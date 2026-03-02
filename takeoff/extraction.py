"""Takeoff extraction: vision model calls to turn snippet images into structured data.

This module is the equivalent of sydyn/evidence.py — it converts raw input
(base64 snippet images) into structured domain objects the agents can reason over.

Vision calls use the Anthropic SDK directly since ModelRouter.complete() handles
text-only prompts. All extraction functions send base64 images to Claude Sonnet.
"""

import base64
import io
import json
import logging
import os
import random
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List, Optional, Dict

try:
    from PIL import Image as _PilImage
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

logger = logging.getLogger(__name__)

from takeoff.settings import API_CONFIG

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False
    logger.warning(
        "'anthropic' package not installed. "
        "Vision extraction will fail at runtime. Run: pip install anthropic"
    )

# Base exception types caught in all extraction functions.
# Includes anthropic.APIError (covers 4xx/5xx/network failures) when available.
_EXTRACTION_ERRORS = (
    RuntimeError, json.JSONDecodeError, ValueError, OSError, TimeoutError
) + ((anthropic.APIError,) if HAS_ANTHROPIC else ())


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


# ─── Grid Counting Data Classes ───────────────────────────────────────────────

@dataclass
class GridCell:
    """A single cell from a gridded RCP snippet."""
    cell_id: str          # "A1", "B2", etc. (row letter + col number)
    image_base64: str     # base64 PNG of this cell
    row: int              # 0-indexed
    col: int              # 0-indexed
    bounds: dict          # {x, y, width, height} as fraction of full image (0.0–1.0)
    area_label: str       # parent area label, e.g. "Floor 2 North Wing"


@dataclass
class CellTypeCount:
    """Count of a single fixture type in a single grid cell."""
    cell_id: str
    type_tag: str
    count: int
    confidence: str = "medium"  # low | medium | high
    notes: str = ""


@dataclass
class GridResult:
    """Complete gridded count results for one RCP area."""
    area_label: str
    grid_cells: List[GridCell]
    cell_type_counts: List[CellTypeCount]   # every (cell, type) pair counted
    area_totals: Dict[str, int]             # type_tag → total across all cells
    warnings: List[str] = field(default_factory=list)


# ─── JSON Extraction Helper ───────────────────────────────────────────────────

def extract_json_from_response(response_text: str, agent_name: str = "Extractor") -> dict:
    """Extract and parse JSON from vision model response.

    Mirrors sydyn/agents.py extract_json_from_response exactly.
    """
    logger.debug("[%s] Raw response preview: %s...", agent_name, response_text[:500])
    _errors = []

    # Strategy 1: Direct parse
    try:
        return json.loads(response_text)
    except json.JSONDecodeError as _e1:
        _errors.append(f"direct: {_e1}")

    # Strategy 2: Extract from markdown code fences
    fence_match = re.search(r'```(?:json)?\s*(.*?)\s*```', response_text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError as _e2:
            _errors.append(f"fence: {_e2}")

    # Strategy 3: Extract from first { to its matching } using a depth counter.
    # rfind('}') was incorrect — it would grab trailing text or a second JSON object.
    first_brace = response_text.find('{')
    if first_brace != -1:
        depth = 0
        in_string = False
        escape_next = False
        matching_close = -1
        for i, ch in enumerate(response_text[first_brace:], start=first_brace):
            if escape_next:
                escape_next = False
                continue
            if ch == '\\' and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if not in_string:
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        matching_close = i
                        break
        if matching_close != -1:
            try:
                return json.loads(response_text[first_brace:matching_close + 1])
            except json.JSONDecodeError as _e3:
                _errors.append(f"braces: {_e3}")

    detail = "; ".join(_errors) or "no JSON structure found"
    logger.warning("[%s] ERROR: Failed to extract valid JSON. Strategies: %s", agent_name, detail)
    raise json.JSONDecodeError(f"Could not extract valid JSON ({detail})", response_text, 0)


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
                api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
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

    # Validate image has plausible content before sending to API.
    # An empty or near-empty base64 string indicates a capture failure.
    _estimated_bytes = len(image_base64) * 3 // 4
    if _estimated_bytes < 100:
        raise ValueError(
            f"[EXTRACTION] image_base64 appears empty or corrupt "
            f"(~{_estimated_bytes} decoded bytes) — check that the snippet image was captured correctly"
        )

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
        except _EXTRACTION_ERRORS as e:
            last_error = e
            if attempt < max_retries:
                delay = (2 ** (attempt + 1)) + random.uniform(0, 1)
                logger.warning("[EXTRACTION] Vision call failed (attempt %d/%d): %s. Retrying in %.1fs...",
                               attempt + 1, max_retries + 1, e, delay)
                time.sleep(delay)
            else:
                logger.error("[EXTRACTION] Vision call failed after %d attempts: %s", max_retries + 1, e)
    raise last_error


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

    except _EXTRACTION_ERRORS as e:
        logger.error("[EXTRACTION] ERROR extracting fixture schedule: %s", e, exc_info=True)
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

        bad_vals = {k: v for k, v in area_count.counts_by_type.items() if not isinstance(v, (int, float))}
        if bad_vals:
            logger.warning("[EXTRACTION] RCP '%s': non-numeric counts_by_type values removed: %s", area_label, bad_vals)
            for k in bad_vals:
                del area_count.counts_by_type[k]
        total = sum(area_count.counts_by_type.values())
        logger.info("[EXTRACTION] RCP '%s': %d fixtures across %d types", area_label, total, len(area_count.counts_by_type))
        return area_count

    except _EXTRACTION_ERRORS as e:
        logger.error("[EXTRACTION] ERROR extracting RCP counts for '%s': %s", area_label, e, exc_info=True)
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

    except _EXTRACTION_ERRORS as e:
        logger.error("[EXTRACTION] ERROR extracting plan notes: %s", e, exc_info=True)
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

    except _EXTRACTION_ERRORS as e:
        logger.error("[EXTRACTION] ERROR extracting panel schedule: %s", e, exc_info=True)
        return PanelData(warnings=[f"Extraction failed: {str(e)}"])


# ─── Grid-Based RCP Counting ──────────────────────────────────────────────────

_MIN_CELL_PX = 50  # Minimum cell dimension in pixels — smaller cells are not useful


def generate_grid(
    image_base64: str,
    area_label: str,
    rows: int = 3,
    cols: int = 3
) -> List[GridCell]:
    """Split an RCP snippet image into a grid of cells.

    Args:
        image_base64: Base64-encoded PNG/JPEG of the RCP area
        area_label: Human-readable area name, carried into each cell
        rows: Number of grid rows (default 3)
        cols: Number of grid columns (default 3)

    Returns:
        List of GridCell objects, ordered row-major (A1, A2, ..., C3)

    Raises:
        RuntimeError: If PIL is not available
        ValueError: If the image cannot be decoded
    """
    if not HAS_PIL:
        raise RuntimeError("Pillow is required for grid extraction. Run: pip install Pillow")

    # Strip data URI prefix if present
    raw_b64 = image_base64
    if raw_b64.startswith("data:"):
        raw_b64 = raw_b64.split(",", 1)[1]

    img = _PilImage.open(io.BytesIO(base64.b64decode(raw_b64))).convert("RGB")
    w, h = img.size

    # Auto-reduce grid size if cells would be too small
    while rows > 1 and h // rows < _MIN_CELL_PX:
        rows -= 1
    while cols > 1 and w // cols < _MIN_CELL_PX:
        cols -= 1

    cell_w = w // cols
    cell_h = h // rows

    cells: List[GridCell] = []
    for r in range(rows):
        for c in range(cols):
            x0 = c * cell_w
            y0 = r * cell_h
            # Last column/row captures remainder pixels to avoid off-by-one gaps
            x1 = w if c == cols - 1 else (c + 1) * cell_w
            y1 = h if r == rows - 1 else (r + 1) * cell_h

            crop = img.crop((x0, y0, x1, y1))
            buf = io.BytesIO()
            crop.save(buf, format="PNG")
            cell_b64 = base64.b64encode(buf.getvalue()).decode()

            cell_id = chr(ord("A") + r) + str(c + 1)
            bounds = {
                "x": x0 / w,
                "y": y0 / h,
                "width": (x1 - x0) / w,
                "height": (y1 - y0) / h,
            }
            cells.append(GridCell(
                cell_id=cell_id,
                image_base64=cell_b64,
                row=r,
                col=c,
                bounds=bounds,
                area_label=area_label,
            ))

    logger.debug("[GRID] Generated %d cells (%dx%d) from %dx%d image for '%s'",
                 len(cells), rows, cols, w, h, area_label)
    return cells


def count_fixture_type_in_cell(
    client,
    cell: GridCell,
    type_tag: str,
    type_description: str,
    schedule_context: str,
    grid_dimensions: str,
) -> CellTypeCount:
    """Count a single fixture type within a single grid cell via vision.

    Args:
        client: Anthropic vision client
        cell: The grid cell to examine
        type_tag: Fixture type identifier (e.g. "A")
        type_description: Human description (e.g. "2x4 LED Troffer")
        schedule_context: Full schedule as text reference (other types listed)
        grid_dimensions: e.g. "3x3" for display in the prompt

    Returns:
        CellTypeCount with count, confidence, and any notes
    """
    system_prompt = (
        f"You are an expert electrical estimator counting fixtures in a section of a "
        f"Reflected Ceiling Plan.\n\n"
        f"FIXTURE TO COUNT: {type_tag} — {type_description}\n"
        f"CELL POSITION: {cell.cell_id} in a {grid_dimensions} grid of area \"{cell.area_label}\"\n\n"
        f"RULES:\n"
        f"1. Count ONLY fixture type {type_tag}. Ignore all other types completely.\n"
        f"2. Count a fixture if its center point or more than 50% of its symbol is within this cell image.\n"
        f"3. If a fixture symbol appears cut off at the edge and you cannot determine if >50% is "
        f"in this cell, note it but do NOT count it.\n"
        f"4. If you cannot identify a symbol as type {type_tag}, do NOT count it — describe it in notes.\n"
        f"5. Be precise. Count individually. Do not estimate or round.\n\n"
        f"FIXTURE SCHEDULE FOR REFERENCE (so you know what other types look like — do NOT count them):\n"
        f"{schedule_context}\n\n"
        f'Respond with ONLY valid JSON:\n'
        f'{{"type_tag": "{type_tag}", "count": <integer>, "confidence": "low|medium|high", "notes": "any observations"}}'
    )
    user_text = (
        f"Count all Type {type_tag} ({type_description}) fixtures in this cell. "
        f"This is cell {cell.cell_id} of area \"{cell.area_label}\"."
    )

    try:
        raw = _call_vision_with_retry(
            client, system_prompt, user_text, cell.image_base64,
            max_tokens=200, temperature=0.0,
        )
        parsed = extract_json_from_response(raw, f"GRID_CELL_{cell.cell_id}_{type_tag}")
        count = parsed.get("count", 0)
        if not isinstance(count, (int, float)) or count < 0:
            count = 0
        return CellTypeCount(
            cell_id=cell.cell_id,
            type_tag=type_tag,
            count=int(count),
            confidence=parsed.get("confidence", "medium"),
            notes=parsed.get("notes", ""),
        )
    except _EXTRACTION_ERRORS as e:
        logger.warning("[GRID] Cell %s type %s extraction failed: %s", cell.cell_id, type_tag, e)
        return CellTypeCount(
            cell_id=cell.cell_id,
            type_tag=type_tag,
            count=0,
            confidence="low",
            notes="EXTRACTION_FAILED",
        )


def extract_rcp_counts_gridded(
    snippet_image: str,
    fixture_schedule: FixtureSchedule,
    area_label: str,
    grid_rows: int = 3,
    grid_cols: int = 3,
) -> GridResult:
    """Extract fixture counts from an RCP snippet using a grid-based approach.

    Splits the image into a grid, then counts each fixture type separately
    in each cell, running all cell×type calls in parallel.

    Args:
        snippet_image: Base64-encoded PNG of the RCP area
        fixture_schedule: Previously extracted fixture schedule for context
        area_label: Human-readable area name
        grid_rows: Number of grid rows (default 3)
        grid_cols: Number of grid columns (default 3)

    Returns:
        GridResult with per-cell counts and area totals

    Raises:
        RuntimeError: If >30% of cell calls fail (caller should fall back)
    """
    cells = generate_grid(snippet_image, area_label, grid_rows, grid_cols)

    # Build fixture type list (skip empty descriptions)
    type_items = [
        (tag, info.get("description", "") if isinstance(info, dict) else "")
        for tag, info in fixture_schedule.fixtures.items()
        if info and (info.get("description") if isinstance(info, dict) else True)
    ]

    if not type_items:
        return GridResult(
            area_label=area_label,
            grid_cells=cells,
            cell_type_counts=[],
            area_totals={},
            warnings=["No fixture types in schedule — cannot count"],
        )

    # Build schedule context string for the prompt
    schedule_context = "\n".join(
        f"  {tag}: {(info.get('description', '') if isinstance(info, dict) else '')}"
        for tag, info in fixture_schedule.fixtures.items()
    )

    actual_rows = (max(c.row for c in cells) + 1) if cells else grid_rows
    actual_cols = (max(c.col for c in cells) + 1) if cells else grid_cols
    grid_dimensions = f"{actual_rows}x{actual_cols}"

    client = _get_vision_client()

    # Build all (cell, type) tasks
    tasks = [
        (cell, type_tag, type_desc)
        for cell in cells
        for type_tag, type_desc in type_items
    ]

    max_workers = max(1, min(10, len(tasks), API_CONFIG.get("vision_max_workers", 4) * 2))
    all_cell_counts: List[CellTypeCount] = [None] * len(tasks)  # type: ignore

    with ThreadPoolExecutor(max_workers=max_workers) as _ex:
        fut_map = {
            _ex.submit(count_fixture_type_in_cell, client, cell, tag, desc, schedule_context, grid_dimensions): i
            for i, (cell, tag, desc) in enumerate(tasks)
        }
        for fut in as_completed(fut_map):
            idx = fut_map[fut]
            try:
                all_cell_counts[idx] = fut.result()
            except Exception as _e:
                cell, tag, _ = tasks[idx]
                logger.warning("[GRID] Future failed for cell %s type %s: %s", cell.cell_id, tag, _e)
                all_cell_counts[idx] = CellTypeCount(cell.cell_id, tag, 0, "low", "EXTRACTION_FAILED")

    # Check failure rate
    failures = sum(1 for ctc in all_cell_counts if ctc and ctc.notes == "EXTRACTION_FAILED")
    if tasks and failures / len(tasks) > 0.30:
        raise RuntimeError(
            f"[GRID] Too many cell failures ({failures}/{len(tasks)}) for '{area_label}' — falling back"
        )

    # Aggregate totals
    area_totals: Dict[str, int] = defaultdict(int)
    for ctc in all_cell_counts:
        if ctc:
            area_totals[ctc.type_tag] += ctc.count

    # Collect warnings
    warnings: List[str] = []
    for ctc in all_cell_counts:
        if ctc and ctc.confidence == "low" and ctc.count > 0:
            warnings.append(f"Low confidence on {ctc.type_tag} in cell {ctc.cell_id}")
        if ctc and ctc.notes == "EXTRACTION_FAILED":
            warnings.append(f"Cell {ctc.cell_id} type {ctc.type_tag} extraction failed — count set to 0")

    logger.info(
        "[GRID] '%s': %d cells × %d types = %d tasks; totals: %s",
        area_label, len(cells), len(type_items), len(tasks), dict(area_totals)
    )

    return GridResult(
        area_label=area_label,
        grid_cells=cells,
        cell_type_counts=[ctc for ctc in all_cell_counts if ctc is not None],
        area_totals=dict(area_totals),
        warnings=warnings,
    )


def grid_result_to_area_count(grid_result: GridResult) -> AreaCount:
    """Convert a GridResult to an AreaCount for backward compatibility with Counter agent."""
    notes = [ctc.notes for ctc in grid_result.cell_type_counts if ctc.notes and ctc.notes != "EXTRACTION_FAILED"]
    return AreaCount(
        area_label=grid_result.area_label,
        counts_by_type=grid_result.area_totals,
        notes=notes[:20],
        warnings=grid_result.warnings,
    )
