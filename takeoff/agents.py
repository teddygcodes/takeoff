"""Takeoff agents: Counter, Checker, Reconciler, Judge.

Mirrors sydyn/agents.py exactly — same patterns, same JSON extraction helpers,
same response dataclasses. Domain logic replaced with lighting takeoff specifics.
"""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

from takeoff.extraction import (
    FixtureSchedule, AreaCount, PlanNote, PanelData, extract_json_from_response,
    _call_vision_with_retry, _get_vision_client,
)


@dataclass
class TakeoffResponse:
    """Output from a takeoff agent."""
    agent_role: str
    data: dict
    raw_response: str
    reasoning: Optional[str] = None
    parse_error: bool = False  # True when JSON parsing of the LLM response failed


# ─── Counter Agent ────────────────────────────────────────────────────────────

class Counter:
    """Produces a complete fixture count from extracted drawing data.

    Equivalent to Researcher in Sydyn — proposes the initial takeoff count.
    """

    def __init__(self, model_router):
        self.model_router = model_router

    def generate_count(
        self,
        fixture_schedule: FixtureSchedule,
        area_counts: List[AreaCount],
        plan_notes: List[PlanNote],
        panel_data: Optional[PanelData] = None
    ) -> TakeoffResponse:
        """Generate a complete fixture count organized by type tag and area.

        Args:
            fixture_schedule: Extracted fixture schedule
            area_counts: Per-area extraction results from RCP snippets
            plan_notes: Constraints from plan notes snippets
            panel_data: Panel schedule data (optional, for cross-reference)

        Returns:
            TakeoffResponse with structured fixture counts
        """
        # Build schedule summary
        schedule_lines = []
        for tag, info in fixture_schedule.fixtures.items():
            desc = info.get("description", "unknown") if isinstance(info, dict) else str(info)
            wattage = info.get("wattage") if isinstance(info, dict) else None
            watt_str = f" ({wattage}W)" if wattage else ""
            schedule_lines.append(f"  Type {tag}: {desc}{watt_str}")
        schedule_text = "\n".join(schedule_lines) if schedule_lines else "  No fixture schedule extracted."

        # Build area count summary
        area_lines = []
        for ac in area_counts:
            counts_str = ", ".join(f"{tag}:{count}" for tag, count in ac.counts_by_type.items())
            area_lines.append(f"  Area '{ac.area_label}': {counts_str}")
            if ac.notes:
                area_lines.append(f"    Notes: {'; '.join(ac.notes)}")
            if ac.warnings:
                area_lines.append(f"    Warnings: {'; '.join(ac.warnings)}")
        areas_text = "\n".join(area_lines) if area_lines else "  No RCP areas extracted."

        # Build plan notes summary
        notes_lines = [f"  - [{n.constraint_type}] {n.text}" for n in plan_notes]
        notes_text = "\n".join(notes_lines) if notes_lines else "  No plan notes."

        # Panel data summary
        panel_text = "No panel schedule provided."
        if panel_data and panel_data.circuits:
            panel_text = f"Panel '{panel_data.panel_name}': total load = {panel_data.total_load_va} VA across {len(panel_data.circuits)} circuits."

        system_prompt = """You are the COUNTER agent in the Takeoff adversarial system — the electrical estimator performing the initial fixture count.

Your role: Produce a complete, organized fixture count from the extracted drawing data.

Rules:
1. Aggregate per-area extraction counts into totals per type tag
2. Every type tag from the fixture schedule MUST appear (even if count is 0)
3. Assign difficulty codes: S=Standard (troffer), M=Moderate (recessed), D=Difficult (needs lift), E=Extreme (custom)
4. List accessories for each fixture type (mounting clips, flex whips, J-boxes, sensors)
5. Flag any type tags that appear in area counts but NOT in the fixture schedule as "UNSCHEDULED"
6. CRITICAL — Flag all assumptions: any fixture type_tag you cannot definitively identify (e.g., "UNKNOWN", "TBD", "?", or any type not clearly described in the schedule) MUST have flags: ["ASSUMPTION: <reason why uncertain>"]. Do not guess silently.
7. Cross-reference with plan notes — add quantities specified in notes if not already in RCP counts

CRITICAL: Respond with ONLY a valid JSON object. No markdown. No explanation before or after.

Output format:
{
  "fixture_counts": [
    {
      "type_tag": "A",
      "description": "2x4 LED Recessed Troffer",
      "counts_by_area": {
        "Floor 2 North Wing": 24,
        "Floor 2 South Wing": 18
      },
      "total": 42,
      "difficulty": "S",
      "notes": "Per fixture schedule sheet E-001",
      "accessories": ["mounting clips", "flex whip"],
      "flags": []
    }
  ],
  "areas_covered": ["Floor 2 North Wing", "Floor 2 South Wing"],
  "grand_total_fixtures": 142,
  "reasoning": "Aggregated counts from 3 RCP areas..."
}"""

        user_prompt = f"""FIXTURE SCHEDULE:
{schedule_text}

RCP AREA COUNTS (extracted from drawing snippets):
{areas_text}

PLAN NOTES:
{notes_text}

PANEL LOAD DATA:
{panel_text}

Produce the complete fixture count. Aggregate all per-area counts, assign difficulty codes, list accessories, and flag any issues."""

        try:
            response = self.model_router.complete(
                task_type="takeoff_counter",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=4000
            )
        except Exception as e:
            print(f"[COUNTER] ERROR: Model router failed: {e}")
            return TakeoffResponse(agent_role="counter", data={"_model_failure": True}, raw_response=f"[MODEL ERROR: {e}]", parse_error=True)

        try:
            data = extract_json_from_response(response.content, "COUNTER")
            if "fixture_counts" not in data:
                logger.warning("[COUNTER] WARNING: Response missing 'fixture_counts' key — got keys: %s", list(data.keys()))
                # Treat missing fixture_counts as a parse failure so the engine returns
                # "agent_parse_error" instead of the misleading "blank_drawing" short-circuit
                return TakeoffResponse(agent_role="counter", data=data, raw_response=response.content, parse_error=True)
            print(f"[COUNTER] {len(data.get('fixture_counts', []))} fixture types, {data.get('grand_total_fixtures', 0)} total fixtures")
            return TakeoffResponse(
                agent_role="counter",
                data=data,
                raw_response=response.content,
                reasoning=data.get("reasoning")
            )
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[COUNTER] ERROR: Failed to parse JSON response: {e}")
            return TakeoffResponse(agent_role="counter", data={}, raw_response=response.content, parse_error=True)


# ─── Checker Agent ────────────────────────────────────────────────────────────

class Checker:
    """Adversarial agent that attacks the Counter's count.

    Equivalent to Adversary in Sydyn — generates attacks on the initial count.
    """

    def __init__(self, model_router):
        self.model_router = model_router

    def generate_attacks(
        self,
        counter_output: dict,
        fixture_schedule: FixtureSchedule,
        area_counts: List[AreaCount],
        plan_notes: List[PlanNote],
        panel_data: Optional[PanelData] = None,
        rcp_images: Optional[List[Dict]] = None,
    ) -> TakeoffResponse:
        """Find errors, omissions, and inconsistencies in the Counter's count.

        Args:
            counter_output: The Counter agent's full output dict
            fixture_schedule: Original fixture schedule for independent verification
            area_counts: Original per-area extraction data
            plan_notes: Plan notes constraints
            panel_data: Panel schedule (optional)
            rcp_images: Optional list of {"area_label": str, "image_data": str} dicts,
                one per RCP snippet. When provided, Checker independently counts each
                area via vision and flags discrepancies with Counter's claimed counts.

        Returns:
            TakeoffResponse with adversarial attacks
        """
        # Summarize counter output
        counter_counts = counter_output.get("fixture_counts", [])
        areas_covered = counter_output.get("areas_covered", [])
        grand_total = counter_output.get("grand_total_fixtures", 0)

        count_summary = []
        for fc in counter_counts:
            count_summary.append(f"  Type {fc.get('type_tag')}: total {fc.get('total', 0)} — areas: {list(fc.get('counts_by_area', {}).keys())}")
        count_text = "\n".join(count_summary) or "  No counts provided."

        # RCP areas available in snippets
        rcp_area_labels = [ac.area_label for ac in area_counts]
        available_areas_text = "\n".join(f"  - {label}" for label in rcp_area_labels)

        # Schedule types available
        schedule_tags = list(fixture_schedule.fixtures.keys())
        schedule_text = "\n".join(f"  - Type {tag}" for tag in schedule_tags)

        # Panel cross-reference
        panel_text = "No panel data."
        if panel_data and panel_data.total_load_va:
            # Estimate counter wattage for cross-reference
            estimated_va = 0
            for fc in counter_counts:
                tag = fc.get("type_tag")
                if tag and tag in fixture_schedule.fixtures:
                    info = fixture_schedule.fixtures[tag]
                    wattage = info.get("wattage", 0) if isinstance(info, dict) else 0
                    if wattage:
                        estimated_va += (fc.get("total", 0) * wattage)
            # Heuristic: small round thousands likely entered as watts instead of VA —
            # surface this in the prompt so the LLM can issue a cross_reference attack.
            unit_warning = ""
            if panel_data.total_load_va % 1000 == 0 and panel_data.total_load_va < 5000:
                unit_warning = f" UNIT CONCERN: {panel_data.total_load_va} is a small round multiple of 1000 — value may be in watts or kVA rather than VA. Flag this as a cross_reference attack."
            panel_text = f"Panel total load: {panel_data.total_load_va} VA.{unit_warning} Estimated Counter wattage: {estimated_va}W. Discrepancy: {abs(panel_data.total_load_va - estimated_va)} VA."

        system_prompt = """You are the CHECKER agent in the Takeoff adversarial system — the second estimator doing an independent review.

Your role: Find every error, omission, and inconsistency in the Counter's takeoff.

Attack categories (use exact string):
- missed_area: Area visible in snippet data but not in Counter's areas_covered
- double_count: Counter may have counted overlapping views twice
- wrong_type: Counter assigned wrong type tag to a fixture
- missed_fixtures: Fixture types in schedule with zero count that likely exist
- math_error: Totals don't add up correctly
- missing_accessory: Required accessories not listed
- cross_reference: Panel load vs. fixture watt mismatch exceeds 15%
- missed_note: Plan note constraints not reflected in counts
- emergency_gap: Emergency fixtures or exit signs not separately tracked

Severity levels:
- critical: Will cause a wrong bid (missed room, major math error)
- major: Significant impact on bid accuracy
- minor: Small issue, easy to verify

CRITICAL: Respond with ONLY a valid JSON object. No markdown. No explanation.

Output format:
{
  "attacks": [
    {
      "attack_id": "ATK-001",
      "severity": "critical|major|minor",
      "category": "missed_area|double_count|wrong_type|missed_fixtures|math_error|missing_accessory|cross_reference|missed_note|emergency_gap",
      "description": "specific description of the issue",
      "affected_type_tag": "type tag or null",
      "affected_area": "area name or null",
      "suggested_correction": "correction text",
      "evidence": "why you believe this is an error"
    }
  ],
  "total_attacks": 0,
  "critical_count": 0,
  "summary": "overall assessment"
}"""

        user_prompt = f"""Counter's fixture count summary:
{count_text}

Counter's areas covered: {areas_covered}
Counter's grand total: {grand_total} fixtures

RCP snippet areas available:
{available_areas_text}

Fixture schedule type tags:
{schedule_text}

Panel cross-reference:
{panel_text}

Check for: missed areas, double-counted overlapping views, wrong fixture type assignments, missing fixture types that likely exist, math errors, missing accessories, emergency fixture gaps, and plan note violations."""

        # ── Vision phase: independent per-area count verification ──────────────
        # For each RCP snippet, call Claude vision to independently count fixtures
        # and compare with Counter's claimed counts. Discrepancies become attacks.
        # Runs BEFORE the text-based LLM call so all attacks dedup together.
        vision_attacks = []
        if rcp_images:
            try:
                vision_client = _get_vision_client()
            except RuntimeError as e:
                logger.warning("[CHECKER] Vision client unavailable — skipping vision phase: %s", e)
                vision_client = None

            if vision_client:
                # Build fixture schedule context once, reuse for all areas
                schedule_lines = []
                for tag, info in fixture_schedule.fixtures.items():
                    desc = info.get("description", "unknown") if isinstance(info, dict) else str(info)
                    schedule_lines.append(f"  Type {tag}: {desc}")
                schedule_context = "\n".join(schedule_lines) or "  (No schedule available)"

                def _check_one_area(rcp: Dict) -> List[Dict]:
                    """Run one vision check for a single RCP area; returns list of attack dicts."""
                    area_label = rcp.get("area_label", "Unknown area")
                    image_data = rcp.get("image_data", "")
                    if not image_data:
                        logger.warning("[CHECKER] No image_data for area '%s' — skipping vision check", area_label)
                        return []

                    # Counter's claimed counts for this specific area
                    claimed_lines = []
                    for fc in counter_output.get("fixture_counts", []):
                        area_count = fc.get("counts_by_area", {}).get(area_label, 0)
                        if area_count > 0:
                            claimed_lines.append(f"  Type {fc.get('type_tag', '?')}: {area_count}")
                    claimed_text = "\n".join(claimed_lines) or "  (Counter claimed 0 fixtures in this area)"

                    vision_system = f"""You are an independent verification agent for electrical fixture counting.

Known fixture types from the schedule:
{schedule_context}

This is the RCP drawing for area: "{area_label}"

The Counter agent claimed these counts for this area:
{claimed_text}

Your task:
1. Count EVERY lighting fixture symbol visible in this RCP drawing independently
2. Match each symbol to its type tag using the fixture schedule above
3. Count any unidentified fixtures as "UNKNOWN"
4. Compare your count with the Counter's claimed count above
5. Report any discrepancies — if your count differs from the Counter's, flag it

CRITICAL rules:
- Count ONLY lighting fixtures — not HVAC diffusers, sprinkler heads, or smoke detectors
- Count every visible symbol, including partially visible ones at edges
- Trust what you SEE in the drawing, not the Counter's claims
- "over_count": Counter claimed MORE than you see (Counter overcounted)
- "under_count": Counter claimed FEWER than you see (Counter missed fixtures)

Severity guide:
- critical: discrepancy ≥3 fixtures or >20% difference
- major: discrepancy of 2 fixtures or 10-20% difference
- minor: discrepancy of 1 fixture or <10% difference

Return JSON ONLY — no explanatory text:
{{
  "area_label": "{area_label}",
  "independent_counts": {{"TYPE_TAG": <int>}},
  "counter_agreed": <bool>,
  "discrepancies": [
    {{
      "type_tag": "<tag>",
      "counter_claimed": <int>,
      "checker_found": <int>,
      "direction": "over_count | under_count",
      "severity": "critical | major | minor",
      "confidence": "high | medium | low",
      "notes": "<brief explanation of what you see>"
    }}
  ],
  "additional_findings": "<other notable issues: obscured symbols, illegible areas, or empty string>"
}}"""

                    try:
                        vision_resp = _call_vision_with_retry(
                            vision_client,
                            system_prompt=vision_system,
                            user_text=f"Verify fixture counts for area '{area_label}'. Return JSON only.",
                            image_base64=image_data,
                            max_tokens=1500,
                            temperature=0.3,
                        )
                        vision_data = extract_json_from_response(vision_resp, "CHECKER_VISION")
                        area_attacks = []
                        for disc in vision_data.get("discrepancies", []):
                            tag = disc.get("type_tag", "UNKNOWN")
                            found = disc.get("checker_found", 0)
                            claimed = disc.get("counter_claimed", 0)
                            direction = disc.get("direction", "unknown")
                            severity = disc.get("severity", "minor")
                            confidence = disc.get("confidence", "medium")
                            notes = disc.get("notes", "")
                            direction_label = "over-count" if direction == "over_count" else "under-count"
                            area_attacks.append({
                                # attack_id assigned after collection to ensure stable ordering
                                "_area_label": area_label,
                                "severity": severity,
                                "category": "missed_fixtures",
                                "affected_type_tag": tag,
                                "affected_area": area_label,
                                "description": (
                                    f"[VISION CHECK] Independent visual count for area '{area_label}' "
                                    f"found {found} × Type {tag}, but Counter claimed {claimed} "
                                    f"({direction_label}). {notes}"
                                ),
                                "suggested_correction": found,
                                "evidence": f"Direct image analysis of RCP for '{area_label}' (confidence: {confidence})",
                            })
                        return area_attacks
                    except Exception as e:
                        logger.warning("[CHECKER] Vision check failed for area '%s': %s — skipping", area_label, e)
                        return []

                # Run all vision checks in parallel (max 4 concurrent to avoid rate limits)
                valid_rcp = [r for r in rcp_images if r.get("image_data")]
                max_vis_workers = min(len(valid_rcp), 4)
                all_area_attacks: List[List[Dict]] = [[] for _ in valid_rcp]
                if max_vis_workers > 0:
                    with ThreadPoolExecutor(max_workers=max_vis_workers) as _vis_ex:
                        _vis_futures = {_vis_ex.submit(_check_one_area, r): i for i, r in enumerate(valid_rcp)}
                        for fut in as_completed(_vis_futures):
                            idx = _vis_futures[fut]
                            try:
                                all_area_attacks[idx] = fut.result()
                            except Exception as _ve:
                                logger.warning("[CHECKER] Vision future raised: %s", _ve)

                # Flatten results in deterministic order and assign VIS IDs
                _vision_attack_counter = 1
                for area_attack_list in all_area_attacks:
                    for atk in area_attack_list:
                        atk.pop("_area_label", None)
                        atk["attack_id"] = f"VIS{_vision_attack_counter:03d}"
                        _vision_attack_counter += 1
                        vision_attacks.append(atk)

        # ── Text-based LLM attack phase ────────────────────────────────────────
        try:
            response = self.model_router.complete(
                task_type="takeoff_checker",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=3000
            )
        except Exception as e:
            print(f"[CHECKER] ERROR: Model router failed: {e}")
            # Still return any vision attacks even if text LLM fails
            if vision_attacks:
                return TakeoffResponse(
                    agent_role="checker",
                    data={"attacks": vision_attacks, "total_attacks": len(vision_attacks),
                          "critical_count": sum(1 for a in vision_attacks if a.get("severity") == "critical"),
                          "_model_failure": True},
                    raw_response=f"[MODEL ERROR: {e}]"
                )
            return TakeoffResponse(agent_role="checker", data={"attacks": [], "_model_failure": True}, raw_response=f"[MODEL ERROR: {e}]")

        try:
            data = extract_json_from_response(response.content, "CHECKER")
            if "attacks" not in data:
                logger.warning("[CHECKER] WARNING: Response missing 'attacks' key — got keys: %s", list(data.keys()))
            # Combine text-based and vision-based attacks before deduplication
            attacks = data.get("attacks", []) + vision_attacks

            # Deduplicate attacks by (category, affected_type_tag, affected_area).
            # When duplicates share a key, keep the highest-severity one so we never
            # silently downgrade a CRITICAL attack to MINOR via dedup.
            _SEVERITY_ORDER = {"critical": 3, "major": 2, "minor": 1}
            seen_attacks: dict = {}
            for attack in attacks:
                key = (
                    (attack.get("category") or "").lower(),
                    (attack.get("affected_type_tag") or "").upper(),
                    (attack.get("affected_area") or "").lower().strip()
                )
                if key not in seen_attacks:
                    seen_attacks[key] = attack
                else:
                    existing_sev = _SEVERITY_ORDER.get(seen_attacks[key].get("severity", "minor"), 1)
                    new_sev = _SEVERITY_ORDER.get(attack.get("severity", "minor"), 1)
                    if new_sev > existing_sev:
                        seen_attacks[key] = attack  # keep higher-severity entry
            deduped = list(seen_attacks.values())
            if len(deduped) < len(attacks):
                print(f"[CHECKER] Deduplicated {len(attacks) - len(deduped)} duplicate attacks")
            data["attacks"] = deduped
            data["total_attacks"] = len(deduped)

            critical = data.get("critical_count", 0)
            print(f"[CHECKER] {len(deduped)} attacks ({critical} critical)")
            return TakeoffResponse(
                agent_role="checker",
                data=data,
                raw_response=response.content,
                reasoning=data.get("summary")
            )
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[CHECKER] ERROR: Failed to parse JSON response: {e}")
            return TakeoffResponse(agent_role="checker", data={"attacks": []}, raw_response=response.content, parse_error=True)


# ─── Reconciler Agent ─────────────────────────────────────────────────────────

class Reconciler:
    """Addresses Checker attacks and produces revised counts.

    Equivalent to Critic in Sydyn — either defends or concedes each attack.
    Skipped in fast mode (same as Critic in Sydyn).
    """

    def __init__(self, model_router):
        self.model_router = model_router

    def address_attacks(
        self,
        counter_output: dict,
        checker_attacks: List[Dict],
        fixture_schedule: FixtureSchedule,
        area_counts: List[AreaCount],
        plan_notes: Optional[List] = None
    ) -> TakeoffResponse:
        """Address each Checker attack — defend or concede.

        Args:
            counter_output: Original Counter counts
            checker_attacks: Attack list from Checker
            fixture_schedule: Fixture schedule for reference
            area_counts: Original RCP extraction data
            plan_notes: Plan notes constraints — Reconciler verifies these were applied

        Returns:
            TakeoffResponse with responses and revised counts
        """
        # Build attacks summary
        attack_lines = []
        for a in checker_attacks:
            attack_lines.append(
                f"  [{a.get('attack_id')}] {(a.get('severity') or 'minor').upper()} — {a.get('category')}: {a.get('description')}"
            )
            if a.get("suggested_correction"):
                attack_lines.append(f"    Suggested: {a.get('suggested_correction')}")
        attacks_text = "\n".join(attack_lines) or "  No attacks."

        # Counter's original counts
        orig_counts = {}
        for fc in counter_output.get("fixture_counts", []):
            orig_counts[fc.get("type_tag")] = fc.get("total", 0)
        orig_text = "\n".join(f"  Type {tag}: {cnt}" for tag, cnt in orig_counts.items())

        system_prompt = """You are the RECONCILER agent in the Takeoff adversarial system — the senior estimator reviewing the dispute.

Your role: Address each Checker attack with a verdict: concede, defend, or partial.

For each attack:
- CONCEDE: Accept the error. Provide corrected count.
- DEFEND: Reject the attack. Explain why Counter was correct.
- PARTIAL: Accept part of the attack. Provide partially corrected count.

After addressing all attacks, provide REVISED fixture counts incorporating all concessions.

CRITICAL: Respond with ONLY a valid JSON object. No markdown. No explanation.

Output format:
{
  "responses": [
    {
      "attack_id": "ATK-001",
      "verdict": "concede|defend|partial",
      "explanation": "why you agree or disagree",
      "revised_count": 6,
      "revised_area": "Floor 2 Break Room"
    }
  ],
  "revised_fixture_counts": {
    "A": {"total": 54, "delta": "+2", "reason": "Conceded ATK-003"},
    "B": {"total": 18, "delta": "0", "reason": "No changes"},
    "C": {"total": 6, "delta": "+6", "reason": "Conceded ATK-001 — missed break room"}
  },
  "revised_grand_total": 148,
  "notes_compliance": [
    {"note": "all corridor fixtures on emergency circuit", "applied": true, "finding": "Type B correctly flagged as emergency circuit in Counter output"},
    {"note": "provide occupancy sensor for type A", "applied": false, "finding": "Counter listed type A accessories as 'mounting clips' only — no occupancy sensor"}
  ],
  "reasoning": "3 of 4 attacks valid. Grand total increased from 142 to 148."
}"""

        # Build plan notes context for semantic compliance verification
        notes_text = "No plan notes provided."
        if plan_notes:
            notes_lines = [f"  - [{n.constraint_type}] {n.text}" for n in plan_notes]
            notes_text = "\n".join(notes_lines)

        user_prompt = f"""Counter's original counts:
{orig_text}
Counter's original grand total: {counter_output.get('grand_total_fixtures', 0)}

Checker's attacks:
{attacks_text}

PLAN NOTE CONSTRAINTS (verify each was applied in the count):
{notes_text}

Address each attack and provide revised counts. For each plan note constraint, explicitly state in your reasoning whether the Counter applied it correctly (e.g. correct circuit assignment, correct accessory, correct quantity). If a constraint was not applied, treat it as a concede."""

        try:
            response = self.model_router.complete(
                task_type="takeoff_reconciler",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=3000
            )
        except Exception as e:
            print(f"[RECONCILER] ERROR: Model router failed: {e}")
            return TakeoffResponse(agent_role="reconciler", data={}, raw_response=f"[MODEL ERROR: {e}]")

        try:
            data = extract_json_from_response(response.content, "RECONCILER")
            if "responses" not in data:
                logger.warning("[RECONCILER] WARNING: Response missing 'responses' key — got keys: %s", list(data.keys()))
            concessions = sum(1 for r in data.get("responses", []) if r.get("verdict") == "concede")
            print(f"[RECONCILER] {len(data.get('responses', []))} responses ({concessions} concessions), revised total: {data.get('revised_grand_total', 'unknown')}")
            return TakeoffResponse(
                agent_role="reconciler",
                data=data,
                raw_response=response.content,
                reasoning=data.get("reasoning")
            )
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[RECONCILER] ERROR: Failed to parse JSON response: {e}")
            return TakeoffResponse(agent_role="reconciler", data={}, raw_response=response.content, parse_error=True)


# ─── Judge Agent ──────────────────────────────────────────────────────────────

class Judge:
    """Evaluates the final takeoff against constitutional rules.

    Equivalent to Judge in Sydyn — constitutional quality gate.
    """

    def __init__(self, model_router, constitution: dict):
        self.model_router = model_router
        self.constitution = constitution

    def evaluate(
        self,
        counter_output: dict,
        checker_attacks: List[Dict],
        reconciler_output: Optional[dict],
        fixture_schedule: FixtureSchedule,
        mode: str = "fast"
    ) -> Dict:
        """Evaluate the complete takeoff against constitutional rules.

        Args:
            counter_output: Counter's fixture counts
            checker_attacks: Checker's attack list
            reconciler_output: Reconciler's responses (None in fast mode)
            fixture_schedule: Fixture schedule for traceability check
            mode: "fast" | "strict" | "liability"

        Returns:
            Dict with verdict, violations, flags, and ruling summary
        """
        if mode not in ("fast", "strict", "liability"):
            raise ValueError(f"[JUDGE] Unknown mode '{mode}'. Must be 'fast', 'strict', or 'liability'.")

        # Build hard rules text
        hard_rules_text = "\n".join([
            f"{i+1}. {rule['name']}: {rule['description']}"
            for i, rule in enumerate(self.constitution["hard_rules"])
        ])

        # Final counts (reconciler's revised if available, else counter's)
        if reconciler_output and reconciler_output.get("revised_fixture_counts"):
            final_counts = reconciler_output["revised_fixture_counts"]
            grand_total = reconciler_output.get("revised_grand_total", 0)
            source = "Reconciler (post-adversarial)"
        else:
            final_counts_list = counter_output.get("fixture_counts", [])
            # Guard: skip entries missing type_tag to prevent a {None: ...} phantom dict key
            final_counts = {
                fc.get("type_tag"): {"total": fc.get("total")}
                for fc in final_counts_list
                if fc.get("type_tag")
            }
            grand_total = counter_output.get("grand_total_fixtures", 0)
            source = "Counter (pre-adversarial)"

        # Areas
        areas = counter_output.get("areas_covered", [])

        # Schedule tags
        schedule_tags = list(fixture_schedule.fixtures.keys())

        # Attack summary
        critical_attacks = [a for a in checker_attacks if a.get("severity") == "critical"]
        unresolved = []
        if reconciler_output:
            resolved_ids = {r.get("attack_id") for r in reconciler_output.get("responses", [])}
            unresolved = [a for a in checker_attacks if a.get("attack_id") not in resolved_ids]

        system_prompt = f"""You are the JUDGE agent in the Takeoff adversarial system — the final constitutional authority.

Your role: Evaluate the takeoff against all 6 hard rules and issue a final ruling.

MODE: {mode.upper()}

HARD RULES (must enforce):
{hard_rules_text}

CRITICAL: Respond with ONLY a valid JSON object. No markdown. No explanation before or after.

Output format:
{{
  "verdict": "PASS|WARN|BLOCK",
  "violations": [
    {{
      "rule": "rule name",
      "severity": "FATAL|MAJOR|MINOR",
      "explanation": "specific reason for violation"
    }}
  ],
  "approved_counts": {{
    "A": 54,
    "B": 18
  }},
  "flags": ["list of warnings or items to verify"],
  "ruling_summary": "one paragraph ruling"
}}

PASS: No major violations — takeoff approved
WARN: Minor violations — takeoff acceptable with noted caveats
BLOCK: Fatal violations — takeoff must be redone"""

        user_prompt = f"""Final fixture counts (source: {source}):
{json.dumps(final_counts, indent=2)}

Grand total: {grand_total} fixtures
Areas covered: {areas}
Fixture schedule type tags: {schedule_tags}
Critical Checker attacks: {len(critical_attacks)}
Unresolved attacks: {len(unresolved)}

Evaluate against all 6 constitutional hard rules and issue your ruling."""

        try:
            response = self.model_router.complete(
                task_type="takeoff_judge",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=2000
            )
        except Exception as e:
            # Model/network error — return WARN not BLOCK so users can distinguish
            # "pipeline blocked my count" from "API was temporarily unavailable"
            logger.warning("[JUDGE] Model call failed: %s — returning WARN verdict", e)
            return {
                "verdict": "WARN",
                "violations": [],
                "approved_counts": {},
                "flags": [f"JUDGE_UNAVAILABLE: Judge model call failed ({e}). Manual review required."],
                "ruling_summary": f"Judge unavailable due to model error: {e}. Takeoff not evaluated — manual review required.",
                "raw_response": f"[MODEL ERROR: {e}]"
            }

        try:
            data = extract_json_from_response(response.content, "JUDGE")
            if "verdict" not in data:
                logger.warning("[JUDGE] WARNING: Response missing 'verdict' key — got keys: %s", list(data.keys()))
            verdict = data.get("verdict", "WARN")
            return {
                "verdict": verdict,
                "violations": data.get("violations", []),
                "approved_counts": data.get("approved_counts", {}),
                "flags": data.get("flags", []),
                "ruling_summary": data.get("ruling_summary", ""),
                "raw_response": response.content
            }
        except (json.JSONDecodeError, ValueError) as e:
            # Parse error — also WARN not BLOCK; the takeoff data is intact even if
            # the Judge response was malformed
            logger.warning("[JUDGE] Response parse failed: %s — returning WARN verdict", e)
            return {
                "verdict": "WARN",
                "violations": [],
                "approved_counts": {},
                "flags": [f"JUDGE_UNAVAILABLE: Judge response could not be parsed ({e}). Manual review required."],
                "ruling_summary": "Judge response parse error. Takeoff data is intact but constitutional evaluation could not complete — review manually.",
                "raw_response": response.content
            }


# ─── Grand Total Validation ───────────────────────────────────────────────────

@dataclass
class GrandTotalResult:
    """Result of validate_grand_total — avoids fragile tuple unpacking."""
    counts: dict
    was_corrected: bool


def validate_grand_total(agent_output: dict, agent_name: str = "Agent") -> GrandTotalResult:
    """Validate that grand_total_fixtures matches the sum of per-type totals.

    If the reported grand_total differs from the computed sum by more than 5%,
    log a warning and correct it.

    Returns:
        GrandTotalResult with corrected counts dict and was_corrected flag
    """
    fixture_counts = agent_output.get("fixture_counts", [])
    reported_total = agent_output.get("grand_total_fixtures", 0) or 0

    if not fixture_counts:
        return GrandTotalResult(counts=agent_output, was_corrected=False)

    computed_total = sum(fc.get("total", 0) for fc in fixture_counts)
    if computed_total == 0:
        if reported_total > 0:
            # All per-type totals are zero but grand_total claims non-zero — correct it.
            print(
                f"[{agent_name}] WARNING: grand_total_fixtures={reported_total} "
                f"but all per-type totals are 0. Correcting grand_total to 0."
            )
            corrected = dict(agent_output)
            corrected["grand_total_fixtures"] = 0
            return GrandTotalResult(counts=corrected, was_corrected=True)
        return GrandTotalResult(counts=agent_output, was_corrected=False)

    discrepancy = abs(reported_total - computed_total) / computed_total
    if discrepancy > 0.05:
        print(
            f"[{agent_name}] WARNING: grand_total_fixtures={reported_total} "
            f"differs from computed sum={computed_total} "
            f"({discrepancy * 100:.1f}% discrepancy). Using computed sum."
        )
        corrected = dict(agent_output)
        corrected["grand_total_fixtures"] = computed_total
        return GrandTotalResult(counts=corrected, was_corrected=True)

    return GrandTotalResult(counts=agent_output, was_corrected=False)
