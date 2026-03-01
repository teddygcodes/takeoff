"""Takeoff extraction: vision model calls to turn snippet images into structured data.

This module is the equivalent of sydyn/evidence.py — it converts raw input
(base64 snippet images) into structured domain objects the agents can reason over.

Vision calls use the Anthropic SDK directly since ModelRouter.complete() handles
text-only prompts. All extraction functions send base64 images to Claude Sonnet.
"""

import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Dict

logger = logging.getLogger(__name__)

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False
    logger.warning(
        "'anthropic' package not installed. "
        "Vision extraction will fail at runtime. Run: pip install anthropic"
    )


# ─── Vision Cost Tracking ─────────────────────────────────────────────────────

# Accumulated token counts across all vision calls in the current process.
# Thread-safe via _cost_lock; reset per-job by calling reset_vision_cost().
_vision_cost_lock = threading.Lock()
_vision_input_tokens: int = 0
_vision_output_tokens: int = 0

# Approximate cost per token for vision model (configurable via env vars).
# Defaults match claude-sonnet-4-6 pricing: $3/1M input, $15/1M output.
# Override with: TAKEOFF_VISION_INPUT_COST_PER_M, TAKEOFF_VISION_OUTPUT_COST_PER_M
def _parse_cost_per_million(env_var: str, default_str: str) -> float:
    """Parse a cost-per-million-tokens env var, falling back to default on bad input."""
    raw = os.getenv(env_var, default_str)
    try:
        return float(raw) / 1_000_000
    except (ValueError, TypeError):
        logger.warning(
            "[EXTRACTION] Invalid value for %s: %r — using default %s per M tokens",
            env_var, raw, default_str
        )
        return float(default_str) / 1_000_000

_VISION_INPUT_COST_PER_TOKEN = _parse_cost_per_million("TAKEOFF_VISION_INPUT_COST_PER_M", "3")
_VISION_OUTPUT_COST_PER_TOKEN = _parse_cost_per_million("TAKEOFF_VISION_OUTPUT_COST_PER_M", "15")


def reset_vision_cost() -> None:
    """Reset accumulated vision token counts. Call at the start of each job."""
    global _vision_input_tokens, _vision_output_tokens
    with _vision_cost_lock:
        _vision_input_tokens = 0
        _vision_output_tokens = 0


def get_vision_cost_usd() -> float:
    """Return accumulated vision extraction cost in USD since last reset."""
    with _vision_cost_lock:
        return (
            _vision_input_tokens * _VISION_INPUT_COST_PER_TOKEN
            + _vision_output_tokens * _VISION_OUTPUT_COST_PER_TOKEN
        )


# ─── Data Classes ───────────────────────────────────────────────────────────

@dataclass
class FixtureEntry:
    """Single row from the fixture schedule."""
    type_tag: str
    description: str
    manufacturer: Optional[str] = None
    catalog_number: Optional[str] = None
    voltage: Optional[str] = None
    mounting: Optional[str] = None
    dimming: Optional[str] = None
    wattage: Optional[float] = None
    notes: Optional[str] = None


@dataclass
class FixtureSchedule:
    """Structured fixture schedule extracted from a drawing snippet."""
    fixtures: Dict[str, dict] = field(default_factory=dict)   # tag -> FixtureEntry dict
    raw_notes: Optional[str] = None
    extraction_confidence: str = "low"  # low | medium | high
    warnings: List[str] = field(default_factory=list)


@dataclass
class AreaCount:
    """Fixture counts for a single RCP area."""
    area_label: str
    counts_by_type: Dict[str, int] = field(default_factory=dict)  # type_tag -> count
    notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class PlanNote:
    """Single parsed constraint from plan notes."""
    text: str
    affects_fixture_type: Optional[str] = None
    constraint_type: str = "general"  # circuit | quantity | placement | mounting | general


@dataclass
class PanelData:
    """Extracted panel schedule data for cross-reference."""
    panel_name: Optional[str] = None
    circuits: List[dict] = field(default_factory=list)  # [{circuit, breaker_size, load_va, description}]
    total_load_va: Optional[float] = None
    warnings: List[str] = field(default_factory=list)


# ─── JSON Extraction Helper ───────────────────────────────────────────────────

def extract_json_from_response(response_text: str, agent_name: str = "Extractor") -> dict:
    """Extract and parse JSON from vision model response.

    Mirrors sydyn/agents.py extract_json_from_response exactly.
    """
    logger.debug("[%s] Raw response preview: %s...", agent_name, response_text[:500])

    # Strategy 1: Direct parse
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        pass

    # Strategy 2: Extract from markdown code fences
    fence_match = re.search(r'```(?:json)?\s*(.*?)\s*```', response_text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # Strategy 3: Extract from first { to last }
    first_brace = response_text.find('{')
    last_brace = response_text.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(response_text[first_brace:last_brace + 1])
        except json.JSONDecodeError:
            pass

    logger.warning("[%s] ERROR: Failed to extract valid JSON", agent_name)
    raise json.JSONDecodeError("Could not extract valid JSON", response_text, 0)


# ─── Vision Client ───────────────────────────────────────────────────────────

# Lazy-initialized module-level client — created once, reused across all extraction
# calls within a process. Avoids redundant TLS handshakes and connection-pool churn
# when multiple futures call extraction concurrently via ThreadPoolExecutor.
_vision_client: 'Optional[anthropic.Anthropic]' = None
_vision_client_lock = threading.Lock()


def _get_vision_client() -> 'anthropic.Anthropic':
    """Return the shared Anthropic client for vision calls (created once, reused).

    Raises:
        RuntimeError: If anthropic package is missing or API key is not set.
    """
    global _vision_client
    if not HAS_ANTHROPIC:
        raise RuntimeError(
            "[TAKEOFF] FATAL: 'anthropic' package not installed. "
            "Run: pip install anthropic"
        )
    if _vision_client is None:
        with _vision_client_lock:
            if _vision_client is None:
                api_key = os.getenv("ANTHROPIC_API_KEY", "")
                if not api_key:
                    raise RuntimeError(
                        "[TAKEOFF] FATAL: ANTHROPIC_API_KEY not set. "
                        "Cannot run vision extraction. Set the key and retry."
                    )
                try:
                    timeout = float(os.getenv("TAKEOFF_VISION_TIMEOUT", "180"))
                    if timeout <= 0:
                        raise ValueError("timeout must be positive")
                except (ValueError, TypeError):
                    logger.warning("[EXTRACTION] Invalid TAKEOFF_VISION_TIMEOUT — using 180s default")
                    timeout = 180.0
                _vision_client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
    return _vision_client


def _call_vision(
    client,
    system_prompt: str,
    user_text: str,
    image_base64: str,
    max_tokens: int = 3000,
    temperature: float = 0.0,
    model: Optional[str] = None
) -> str:
    """Send a vision request to Claude Sonnet.

    Args:
        client: Anthropic client
        system_prompt: System-level instructions
        user_text: User text prompt
        image_base64: Base64-encoded PNG image
        max_tokens: Max output tokens
        temperature: Sampling temperature
        model: Model to use (must support vision)

    Returns:
        Response text content
    """
    if model is None:
        model = os.getenv("TAKEOFF_VISION_MODEL", "claude-sonnet-4-6")

    # Detect media type and strip data URI prefix if present
    detected_media_type = "image/png"
    if image_base64.startswith("data:"):
        header, image_base64 = image_base64.split(",", 1)
        # header looks like "data:image/jpeg;base64" — extract the exact MIME type
        # between "data:" and ";" (or end of string) to avoid partial-match false positives
        _mime = header.split(":")[1].split(";")[0].strip().lower() if ":" in header else ""
        if _mime in ("image/jpeg", "image/jpg"):
            detected_media_type = "image/jpeg"
        elif _mime == "image/webp":
            detected_media_type = "image/webp"
        elif _mime == "image/gif":
            detected_media_type = "image/gif"
        # else: falls through to default "image/png"

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": detected_media_type,
                        "data": image_base64
                    }
                },
                {
                    "type": "text",
                    "text": user_text
                }
            ]
        }]
    )

    # Accumulate token usage for cost tracking
    global _vision_input_tokens, _vision_output_tokens
    if hasattr(response, "usage") and response.usage:
        with _vision_cost_lock:
            _vision_input_tokens += getattr(response.usage, "input_tokens", 0)
            _vision_output_tokens += getattr(response.usage, "output_tokens", 0)

    if not response.content:
        raise RuntimeError("[EXTRACTION] Vision API returned empty content array")
    return response.content[0].text


def _call_vision_with_retry(
    client,
    system_prompt: str,
    user_text: str,
    image_base64: str,
    max_tokens: int = 3000,
    temperature: float = 0.0,
    model: Optional[str] = None,
    max_retries: int = 2
) -> str:
    """Call vision API with exponential backoff retry on transient failures."""
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return _call_vision(client, system_prompt, user_text, image_base64, max_tokens, temperature, model)
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                delay = (2 ** (attempt + 1)) + random.uniform(0, 1)
                logger.warning("[EXTRACTION] Vision call failed (attempt %d/%d): %s. Retrying in %.1fs...",
                               attempt + 1, max_retries + 1, e, delay)
                time.sleep(delay)
            else:
                logger.error("[EXTRACTION] Vision call failed after %d attempts: %s", max_retries + 1, e)
    raise last_error


def _simulate_vision_response(system_prompt: str, user_text: str) -> str:
    """TEST ONLY: Simulated vision response for unit tests.
    Never called automatically — only usable via explicit --simulate flag in CLI.
    Do NOT call from production code paths."""
    system_lower = system_prompt.lower()
    user_lower = user_text.lower()

    if "fixture schedule" in system_lower or "fixture schedule" in user_lower:
        return json.dumps({
            "fixtures": {
                "A": {
                    "description": "2x4 LED Recessed Troffer, 4000K, 3500 lumens",
                    "manufacturer": "Acuity Lithonia",
                    "catalog_number": "BLT4 40L ADP LP840",
                    "voltage": "120-277V",
                    "mounting": "Recessed",
                    "dimming": "0-10V",
                    "wattage": 38.0,
                    "notes": "Standard office fixture"
                },
                "B": {
                    "description": "4\" LED Recessed Downlight, 3000K",
                    "manufacturer": "Lithonia Lighting",
                    "catalog_number": "WF4 LED 30K 120",
                    "voltage": "120V",
                    "mounting": "Recessed",
                    "dimming": "None",
                    "wattage": 11.0,
                    "notes": "Corridor and breakroom"
                },
                "C": {
                    "description": "LED Exit Sign, green letters, battery backup",
                    "manufacturer": "Sure-Lites",
                    "catalog_number": "LP6N",
                    "voltage": "120/277V",
                    "mounting": "Wall or Ceiling",
                    "dimming": "N/A",
                    "wattage": 3.0,
                    "notes": "Emergency egress — battery backup required"
                }
            },
            "warnings": [],
            "extraction_confidence": "high",
            "raw_notes": "Mock schedule extraction"
        })

    if "reflected ceiling plan" in system_lower or "rcp" in system_lower or "count" in user_lower:
        return json.dumps({
            "area_label": "Mock Area",
            "counts_by_type": {"A": 18, "B": 6},
            "notes": ["All type A on standard circuit"],
            "warnings": []
        })

    if "plan note" in system_lower or "specification" in system_lower:
        return json.dumps({
            "notes": [
                {
                    "text": "All corridor fixtures on emergency circuit",
                    "affects_fixture_type": None,
                    "constraint_type": "circuit"
                },
                {
                    "text": "Provide occupancy sensor in each private office",
                    "affects_fixture_type": "A",
                    "constraint_type": "mounting"
                }
            ]
        })

    if "panel schedule" in system_lower or "circuit" in system_lower:
        return json.dumps({
            "panel_name": "LP-1",
            "circuits": [
                {"circuit": "1", "breaker_size": "20A", "load_va": 1200, "description": "Lighting - Floor 2 North"},
                {"circuit": "3", "breaker_size": "20A", "load_va": 950, "description": "Lighting - Floor 2 South"}
            ],
            "total_load_va": 2150,
            "warnings": []
        })

    return json.dumps({"error": "Unrecognized extraction request", "raw": ""})


# ─── Extraction Functions ─────────────────────────────────────────────────────

def extract_fixture_schedule(snippet_image: str) -> FixtureSchedule:
    """Extract structured fixture schedule from a snippet image.

    Args:
        snippet_image: Base64-encoded PNG of the fixture schedule table

    Returns:
        FixtureSchedule with all type tags and descriptions
    """
    client = _get_vision_client()

    system_prompt = """You are an expert electrical estimator reading a fixture schedule from construction drawings.

Your task: Extract every row from the fixture schedule table and return structured JSON.

The fixture schedule is a table with columns like:
- Type Tag (the letter/code like A, B, C1, D2)
- Description (fixture name and specs)
- Manufacturer
- Catalog Number / Model
- Lamp Type / Color Temperature
- Voltage
- Mounting Type
- Dimming (if any)
- Wattage
- Notes / Remarks

CRITICAL instructions:
1. Extract EVERY row — do not skip any fixture type
2. Preserve the exact type tag as shown (A, B, C1, D2, etc.)
3. If a column is blank or illegible, use null
4. Estimate wattage as a number (watts) if shown
5. Flag anything that looks like an exit sign, emergency fixture, or battery pack

Return ONLY valid JSON (no markdown fences):
{
  "fixtures": {
    "A": {
      "description": "full description",
      "manufacturer": "manufacturer name or null",
      "catalog_number": "catalog/model or null",
      "voltage": "voltage string or null",
      "mounting": "recessed/surface/pendant/etc or null",
      "dimming": "0-10V/DALI/none or null",
      "wattage": numeric_watts_or_null,
      "notes": "any notes or null"
    }
  },
  "warnings": ["list of any issues or ambiguities"],
  "extraction_confidence": "high|medium|low",
  "raw_notes": "any general notes at bottom of schedule"
}"""

    user_text = "Extract the complete fixture schedule from this drawing. Return all fixture types in the JSON format specified."

    try:
        response_text = _call_vision_with_retry(client, system_prompt, user_text, snippet_image, max_tokens=3000)
        data = extract_json_from_response(response_text, "FIXTURE_SCHEDULE")

        fixtures_raw = data.get("fixtures", {})
        fixture_schedule = FixtureSchedule(
            fixtures=fixtures_raw,
            raw_notes=data.get("raw_notes"),
            extraction_confidence=data.get("extraction_confidence", "medium"),
            warnings=data.get("warnings", [])
        )
        logger.info("[EXTRACTION] Fixture schedule: %d types extracted", len(fixtures_raw))
        return fixture_schedule

    except Exception as e:
        logger.error("[EXTRACTION] ERROR extracting fixture schedule: %s", e)
        return FixtureSchedule(warnings=[f"Extraction failed: {str(e)}"])


def extract_rcp_counts(
    snippet_image: str,
    fixture_schedule: FixtureSchedule,
    area_label: str
) -> AreaCount:
    """Extract fixture counts from an RCP snippet image.

    Args:
        snippet_image: Base64-encoded PNG of the RCP area
        fixture_schedule: Previously extracted fixture schedule for context
        area_label: Human-readable label for this area (e.g. "Floor 2 North Wing")

    Returns:
        AreaCount with per-type counts for this area
    """
    client = _get_vision_client()

    # Build schedule context for the prompt
    schedule_context = ""
    if fixture_schedule.fixtures:
        lines = ["Known fixture types from the schedule:"]
        for tag, info in fixture_schedule.fixtures.items():
            if not isinstance(info, dict):
                logger.warning("[EXTRACTION] Fixture '%s' info is not a dict (got %s) — Counter may receive garbled description", tag, type(info).__name__)
            desc = info.get("description", "unknown") if isinstance(info, dict) else str(info)
            lines.append(f"  Type {tag}: {desc}")
        schedule_context = "\n".join(lines)

    system_prompt = f"""You are an expert electrical estimator counting lighting fixtures on a Reflected Ceiling Plan (RCP).

{schedule_context}

Your task: Count every lighting fixture symbol visible in this RCP area and return a count by fixture type tag.

CRITICAL counting rules:
1. Count EVERY fixture symbol — do not estimate or assume
2. Match each symbol to its type tag using the fixture schedule above
3. Count by type tag (A, B, C, etc.) — not by description
4. If you can see a fixture symbol but cannot identify its type tag, count it as "UNKNOWN"
5. Do NOT count HVAC diffusers, sprinkler heads, or smoke detectors
6. If a room has no fixtures, do not include it in the counts
7. List any notes that appear on the RCP that affect fixture counts (e.g. "TYP 4 PLACES")

Return ONLY valid JSON:
{{
  "area_label": "{area_label}",
  "counts_by_type": {{
    "A": 18,
    "B": 4,
    "C": 2
  }},
  "notes": ["list of relevant RCP notes that affect counts"],
  "warnings": ["any ambiguities, illegible symbols, or assumptions made"]
}}"""

    user_text = f"Count all lighting fixture symbols in this RCP area: '{area_label}'. Return counts by type tag."

    try:
        response_text = _call_vision_with_retry(client, system_prompt, user_text, snippet_image, max_tokens=2000)
        data = extract_json_from_response(response_text, "RCP_COUNTS")

        area_count = AreaCount(
            area_label=data.get("area_label", area_label),
            counts_by_type=data.get("counts_by_type", {}),
            notes=data.get("notes", []),
            warnings=data.get("warnings", [])
        )

        total = sum(area_count.counts_by_type.values())
        logger.info("[EXTRACTION] RCP '%s': %d fixtures across %d types", area_label, total, len(area_count.counts_by_type))
        return area_count

    except Exception as e:
        logger.error("[EXTRACTION] ERROR extracting RCP counts for '%s': %s", area_label, e)
        return AreaCount(area_label=area_label, warnings=[f"Extraction failed: {str(e)}"])


def extract_plan_notes(snippet_image: str) -> List[PlanNote]:
    """Extract relevant constraints from plan notes snippet.

    Args:
        snippet_image: Base64-encoded PNG of plan notes or specifications

    Returns:
        List of PlanNote constraint objects
    """
    client = _get_vision_client()

    system_prompt = """You are an expert electrical estimator reading plan notes and specifications from construction drawings.

Your task: Extract all notes that affect lighting fixture counts, locations, or installation requirements.

Focus on notes about:
- Emergency circuits or emergency fixture requirements
- Occupancy sensors or control devices
- Fixture counts specified in notes (e.g. "provide 2 type B in each restroom")
- Mounting height or location constraints
- Fixture accessories required (e.g. "all type A to have emergency battery backup")
- Exceptions or substitutions
- Reference to addenda or RFIs

Return ONLY valid JSON:
{
  "notes": [
    {
      "text": "full note text",
      "affects_fixture_type": "type tag or null",
      "constraint_type": "circuit|quantity|placement|mounting|accessory|general"
    }
  ]
}"""

    user_text = "Extract all notes from this drawing that affect lighting fixture counts or installation requirements."

    try:
        response_text = _call_vision_with_retry(client, system_prompt, user_text, snippet_image, max_tokens=2000)
        data = extract_json_from_response(response_text, "PLAN_NOTES")

        notes = []
        for n in data.get("notes", []):
            notes.append(PlanNote(
                text=n.get("text", ""),
                affects_fixture_type=n.get("affects_fixture_type"),
                constraint_type=n.get("constraint_type", "general")
            ))

        logger.info("[EXTRACTION] Plan notes: %d constraints extracted", len(notes))
        return notes

    except Exception as e:
        logger.error("[EXTRACTION] ERROR extracting plan notes: %s", e)
        return []


def extract_panel_schedule(snippet_image: str) -> PanelData:
    """Extract panel schedule data for cross-reference wattage verification.

    Args:
        snippet_image: Base64-encoded PNG of the panel schedule

    Returns:
        PanelData with circuit loads and totals
    """
    client = _get_vision_client()

    system_prompt = """You are an expert electrical estimator reading a panel schedule from construction drawings.

Your task: Extract circuit data from this panel schedule, focusing on lighting circuits.

For each circuit, capture:
- Circuit number
- Breaker size (amps)
- Load (VA or watts)
- Circuit description/label

Sum up total lighting load in VA.

Return ONLY valid JSON:
{
  "panel_name": "panel designation (e.g. LP-1) or null",
  "circuits": [
    {
      "circuit": "circuit number",
      "breaker_size": "20A",
      "load_va": 1200,
      "description": "Lighting - Office Level 2"
    }
  ],
  "total_load_va": 4800,
  "warnings": ["any illegible entries or assumptions"]
}"""

    user_text = "Extract all circuit data from this panel schedule. Focus on lighting circuits for load cross-reference."

    try:
        response_text = _call_vision_with_retry(client, system_prompt, user_text, snippet_image, max_tokens=2000)
        data = extract_json_from_response(response_text, "PANEL_SCHEDULE")

        panel = PanelData(
            panel_name=data.get("panel_name"),
            circuits=data.get("circuits", []),
            total_load_va=data.get("total_load_va"),
            warnings=data.get("warnings", [])
        )

        logger.info("[EXTRACTION] Panel '%s': %d circuits, %s VA total", panel.panel_name, len(panel.circuits), panel.total_load_va)
        return panel

    except Exception as e:
        logger.error("[EXTRACTION] ERROR extracting panel schedule: %s", e)
        return PanelData(warnings=[f"Extraction failed: {str(e)}"])
