"""Takeoff main engine — orchestrates the full adversarial lighting takeoff pipeline.

Mirrors sydyn/engine.py exactly:
- Same phase structure (extract → counter → checker → reconciler → judge → confidence)
- Same fast/strict/liability modes
- Same status callback pattern for SSE streaming
- Same result dict structure
"""

import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

from takeoff.models import ModelRouter
from takeoff.schema import TakeoffDB
from takeoff.constitution import get_constitution, enforce_constitution
from takeoff.confidence import calculate_confidence, format_confidence_explanation
from takeoff.extraction import (
    FixtureSchedule, AreaCount, PlanNote, PanelData,
    extract_fixture_schedule, extract_rcp_counts, extract_plan_notes, extract_panel_schedule
)
from takeoff.agents import Counter, Checker, Reconciler, Judge, validate_grand_total
from takeoff.extraction import reset_vision_cost, get_vision_cost_usd


def _scale_area_counts(original_areas: dict, target_total: int) -> dict:
    """Scale per-area counts to a new total using the largest-remainder algorithm.

    Guarantees sum(result.values()) == target_total without off-by-one rounding errors.
    """
    if not original_areas:
        return {}
    original_total = sum(original_areas.values())
    if original_total == 0:
        return {area: 0 for area in original_areas}

    scale = target_total / original_total
    floored = {area: int(cnt * scale) for area, cnt in original_areas.items()}
    remainder = target_total - sum(floored.values())

    # Distribute leftover units to the areas with the largest fractional parts
    sorted_by_frac = sorted(
        original_areas.keys(),
        key=lambda a: (original_areas[a] * scale) - int(original_areas[a] * scale),
        reverse=True
    )
    for i in range(remainder):
        floored[sorted_by_frac[i % len(sorted_by_frac)]] += 1

    return {area: max(0, cnt) for area, cnt in floored.items()}



class TakeoffEngine:
    """Main orchestrator for adversarial lighting takeoff jobs."""

    def __init__(
        self,
        db_path: str = "takeoff.db",
        model_router: Optional[ModelRouter] = None
    ):
        """Initialize the Takeoff engine.

        Args:
            db_path: Path to SQLite database
            model_router: Optional ModelRouter (will create if None)
        """
        self.db = TakeoffDB(db_path)
        self.constitution = get_constitution()

        if model_router:
            self.model_router = model_router
        else:
            self.model_router = ModelRouter()

        # Initialize agents
        self.counter = Counter(self.model_router)
        self.checker = Checker(self.model_router)
        self.reconciler = Reconciler(self.model_router)
        self.judge = Judge(self.model_router, self.constitution)

    def run_takeoff(
        self,
        snippets: List[Dict],
        mode: Optional[str] = "strict",
        drawing_name: Optional[str] = None,
        status_callback=None
    ) -> Dict:
        """Execute the full adversarial takeoff pipeline.

        Args:
            snippets: List of snippet dicts with id, label, sub_label, image_data (base64), page_number
            mode: "fast" | "strict" | "liability"
            drawing_name: Optional display name for the drawing set
            status_callback: Optional callback for SSE status updates

        Returns:
            Dict with fixture counts, confidence, adversarial log, and metadata
        """
        self._status_callback = status_callback

        def emit(message: str):
            if status_callback:
                try:
                    status_callback(message)
                except Exception as _cb_err:
                    print(f"[TAKEOFF] WARNING: status_callback raised: {_cb_err}")
            print(f"[TAKEOFF] {message}")

        # Generate job ID
        job_id = str(uuid.uuid4())[:8]
        start_time = time.time()

        # Validate mode immediately — before any expensive extraction calls
        if mode not in ("fast", "strict", "liability"):
            raise ValueError(f"Unknown mode '{mode}'. Must be 'fast', 'strict', or 'liability'.")

        emit(f"Starting takeoff job {job_id}...")

        # Reset vision token cost accumulator for this job
        reset_vision_cost()

        # ─── Step 1: Validate snippets ───────────────────────────────────────
        emit("Validating snippet set...")

        # Validate snippet labels and warn on unrecognized values
        valid_labels = {"fixture_schedule", "rcp", "plan_notes", "panel_schedule", "detail", "site_plan"}
        for s in snippets:
            lbl = s.get("label", "")
            if lbl and lbl not in valid_labels:
                emit(f"WARNING: Snippet '{s.get('id', '?')}' has unrecognized label '{lbl}' — expected one of: {', '.join(sorted(valid_labels))}")
            if not s.get("image_data", "").strip():
                emit(f"WARNING: Snippet '{s.get('id', '?')}' (label: '{lbl}') has empty or missing 'image_data' — will be skipped during extraction")

        fixture_snippets = [s for s in snippets if s.get("label") == "fixture_schedule"]
        rcp_snippets = [s for s in snippets if s.get("label") == "rcp"]
        notes_snippets = [s for s in snippets if s.get("label") == "plan_notes"]
        panel_snippets = [s for s in snippets if s.get("label") == "panel_schedule"]

        if not fixture_snippets:
            return {
                "job_id": job_id,
                "error": "insufficient_snippets",
                "message": "At least 1 fixture_schedule snippet is required."
            }

        if not rcp_snippets:
            return {
                "job_id": job_id,
                "error": "insufficient_snippets",
                "message": "At least 1 rcp snippet is required."
            }

        emit(f"Snippet set valid: {len(fixture_snippets)} schedule(s), {len(rcp_snippets)} RCP(s), {len(notes_snippets)} note(s), {len(panel_snippets)} panel(s)")

        # Create job record
        self.db.create_job(
            job_id=job_id,
            mode=mode,
            drawing_name=drawing_name,
            snippet_count=len(snippets)
        )
        # Strip base64 image data before persisting — images can be multi-MB each
        snippets_meta = [{k: v for k, v in s.items() if k != "image_data"} for s in snippets]
        self.db.store_snippets(job_id, snippets_meta)

        # ─── Step 2: Extract fixture schedule (parallel if multiple snippets) ────
        emit("Extracting fixture schedule...")

        fixture_schedule = FixtureSchedule()
        fs_images = [(s, s.get("image_data", "")) for s in fixture_snippets if s.get("image_data", "")]
        if fs_images:
            with ThreadPoolExecutor(max_workers=min(len(fs_images), 6)) as _ex:
                fs_futures = {_ex.submit(extract_fixture_schedule, img): s for s, img in fs_images}
                for fut in as_completed(fs_futures):
                    try:
                        extracted = fut.result()
                    except Exception as _e:
                        emit(f"WARNING: Fixture schedule extraction failed for one snippet: {_e}")
                        continue
                    for tag, info in extracted.fixtures.items():
                        if tag in fixture_schedule.fixtures:
                            emit(f"WARNING: Fixture type '{tag}' defined in multiple schedule snippets — keeping first definition")
                        else:
                            fixture_schedule.fixtures[tag] = info
                    fixture_schedule.warnings.extend(extracted.warnings)
                    if not fixture_schedule.raw_notes:
                        fixture_schedule.raw_notes = extracted.raw_notes

        emit(f"Fixture schedule: {len(fixture_schedule.fixtures)} type tags extracted")

        # H2: Fail with NEEDS_REVIEW if extraction produced no fixtures (API error, blank image, etc.)
        if not fixture_schedule.fixtures:
            warning_text = "; ".join(fixture_schedule.warnings) if fixture_schedule.warnings else "unknown reason"
            emit(f"WARNING: Fixture schedule extraction yielded 0 fixtures — {warning_text}")
            self.db.update_job_status(job_id, "needs_review")
            return {
                "job_id": job_id,
                "status": "needs_review",
                "error": "extraction_failed",
                "message": (
                    f"Fixture schedule extraction failed — no fixture types found. {warning_text}. "
                    "Please verify the fixture_schedule snippet shows the full schedule table clearly, "
                    "then resubmit."
                ),
                "confidence": 0.0,
                "confidence_band": "VERY_LOW",
                "verdict": "BLOCK",
                "flags": ["Manual review required — fixture schedule extraction failed"]
            }

        self.db.store_fixture_schedule(job_id, {"fixtures": fixture_schedule.fixtures})

        # ─── Steps 3-5: Extract RCP, notes, and panel in parallel ─────────────
        # RCP needs the fixture schedule for context, so these run after Step 2.
        # Within this group they are independent and can run concurrently.

        # Pre-compute area labels (avoids index-ordering issues with threads)
        rcp_jobs = [
            (rcp_snippet.get("sub_label") or f"Area {i + 1}", rcp_snippet)
            for i, rcp_snippet in enumerate(rcp_snippets)
            if rcp_snippet.get("image_data", "")
        ]

        emit(f"Extracting {len(rcp_jobs)} RCP area(s), {len(notes_snippets)} note(s), {len(panel_snippets)} panel(s) in parallel...")

        # Build work units
        _parallel_work = []
        _rcp_indices: List[int] = []
        _notes_indices: List[int] = []
        _panel_indices: List[int] = []

        for area_label, rcp_snippet in rcp_jobs:
            _rcp_indices.append(len(_parallel_work))
            _parallel_work.append(("rcp", area_label, rcp_snippet.get("image_data", "")))

        for notes_snippet in notes_snippets:
            if notes_snippet.get("image_data", ""):
                _notes_indices.append(len(_parallel_work))
                _parallel_work.append(("notes", None, notes_snippet.get("image_data", "")))

        for panel_snippet in panel_snippets:
            if panel_snippet.get("image_data", ""):
                _panel_indices.append(len(_parallel_work))
                _parallel_work.append(("panel", None, panel_snippet.get("image_data", "")))

        _results: List = [None] * len(_parallel_work)

        if _parallel_work:
            max_workers = min(len(_parallel_work), 8)
            with ThreadPoolExecutor(max_workers=max_workers) as _ex:
                idx_futures = {}
                for i, (kind, label, img) in enumerate(_parallel_work):
                    if kind == "rcp":
                        idx_futures[_ex.submit(extract_rcp_counts, img, fixture_schedule, label)] = i
                    elif kind == "notes":
                        idx_futures[_ex.submit(extract_plan_notes, img)] = i
                    else:
                        idx_futures[_ex.submit(extract_panel_schedule, img)] = i
                for fut in as_completed(idx_futures):
                    idx = idx_futures[fut]
                    try:
                        _results[idx] = fut.result()
                    except Exception as _e:
                        emit(f"WARNING: Parallel extraction failed for one snippet: {_e}")
                        # _results[idx] stays None — downstream code handles None gracefully

        # Collect ordered results
        area_counts: List[AreaCount] = [_results[i] for i in _rcp_indices if _results[i] is not None]
        for ac in area_counts:
            emit(f"Counted fixtures in '{ac.area_label}'")

        plan_notes: List[PlanNote] = []
        for i in _notes_indices:
            if _results[i]:
                plan_notes.extend(_results[i])

        panel_data: Optional[PanelData] = None
        for i in _panel_indices:
            extracted = _results[i]
            if extracted is None:
                continue
            if panel_data is None:
                panel_data = extracted
            else:
                # Warn when panel names disagree — may be two distinct panels being merged
                if (
                    extracted.panel_name
                    and panel_data.panel_name
                    and extracted.panel_name != panel_data.panel_name
                ):
                    emit(
                        f"WARNING: Panel names differ across snippets "
                        f"('{panel_data.panel_name}' vs '{extracted.panel_name}') — "
                        "merging circuits; verify these are the same panel"
                    )
                # Merge circuits from additional panels.
                # Deduplicate by (panel_name, circuit) to prevent double-counting
                # when the same panel snippet is submitted twice.
                existing_keys = {
                    (panel_data.panel_name, c.get("circuit"))
                    for c in panel_data.circuits
                }
                for circuit in extracted.circuits:
                    key = (extracted.panel_name, circuit.get("circuit"))
                    if key not in existing_keys:
                        panel_data.circuits.append(circuit)
                        existing_keys.add(key)
                if extracted.total_load_va and panel_data.total_load_va:
                    panel_data.total_load_va = panel_data.total_load_va + extracted.total_load_va
                panel_data.warnings.extend(extracted.warnings)

        # ─── Step 6: Run agent pipeline ───────────────────────────────────────
        if mode == "fast":
            result = self._run_fast_mode(
                job_id, fixture_schedule, area_counts, plan_notes, panel_data,
                rcp_snippets, emit, start_time
            )
        else:  # strict | liability
            result = self._run_strict_mode(
                job_id, fixture_schedule, area_counts, plan_notes, panel_data,
                rcp_snippets, emit, start_time, mode
            )

        # Update job status — combine agent LLM costs and vision extraction costs
        elapsed_ms = int((time.time() - start_time) * 1000)
        agent_cost_usd = self.model_router.get_stats().get("model_router_cost_usd") or 0.0
        vision_cost_usd = get_vision_cost_usd()
        cost_usd = round(agent_cost_usd + vision_cost_usd, 6)
        self.db.update_job_status(job_id, "complete", latency_ms=elapsed_ms, cost_usd=cost_usd)
        result["latency_ms"] = elapsed_ms
        result["cost_usd"] = cost_usd

        return result

    def _run_counter_phase(
        self,
        fixture_schedule: FixtureSchedule,
        area_counts: List[AreaCount],
        plan_notes: List[PlanNote],
        panel_data: Optional[PanelData],
        emit
    ):
        """Run Counter agent + grand total validation. Returns (counter_output, total_corrected, counter_response)."""
        emit("Counter analyzing drawings and building fixture count...")
        counter_response = self.counter.generate_count(fixture_schedule, area_counts, plan_notes, panel_data)
        _gtr = validate_grand_total(counter_response.data, "COUNTER")
        counter_output, total_corrected = _gtr.counts, _gtr.was_corrected
        emit(f"Counter produced count: {counter_output.get('grand_total_fixtures', 0)} total fixtures")
        if total_corrected:
            emit("WARNING: Counter's grand_total_fixtures was auto-corrected to match per-type sum")
        return counter_output, total_corrected, counter_response

    def _warn_unknown_types(self, area_counts: List[AreaCount], fixture_schedule: FixtureSchedule, emit):
        """Warn about fixture types referenced in RCP areas that aren't in the fixture schedule."""
        known_tags = {t.upper() for t in fixture_schedule.fixtures.keys()}
        unknown_types = {
            tag for ac in area_counts
            for tag in ac.counts_by_type.keys()
            if tag.upper() not in known_tags and tag.upper() != "UNKNOWN"
        }
        if unknown_types:
            emit(f"WARNING: RCP areas reference types not in schedule: {', '.join(sorted(unknown_types))} — may be extraction errors")

    def _apply_rule6_flags(self, counter_output: dict, emit):
        """Rule 6 enforcement: auto-flag any UNKNOWN/TBD type tags Counter didn't self-flag."""
        _assumption_keywords = {"UNKNOWN", "TBD", "UNSCHEDULED", "?"}
        for fc in counter_output.get("fixture_counts", []):
            if fc.get("type_tag", "").upper() in _assumption_keywords:
                existing_flags = fc.get("flags", [])
                if not any("ASSUMPTION" in f for f in existing_flags):
                    fc.setdefault("flags", []).append("ASSUMPTION: fixture type not identified in schedule")
                    emit(f"WARNING: Auto-flagged assumption on type_tag '{fc.get('type_tag')}' per Rule 6")

    def _run_fast_mode(
        self,
        job_id: str,
        fixture_schedule: FixtureSchedule,
        area_counts: List[AreaCount],
        plan_notes: List[PlanNote],
        panel_data: Optional[PanelData],
        rcp_snippets: List[Dict],
        emit,
        start_time: float
    ) -> Dict:
        """Run fast mode: Counter + Checker + Judge (no Reconciler)."""
        emit("Running FAST mode pipeline (Counter + Checker + Judge)")

        # Counter
        counter_output, total_corrected, counter_response = self._run_counter_phase(
            fixture_schedule, area_counts, plan_notes, panel_data, emit
        )

        # Short-circuit: zero fixtures — either blank drawing or Counter JSON parse failure
        if counter_output.get("grand_total_fixtures", 0) == 0:
            if counter_response.parse_error:
                emit("ERROR: Counter agent failed to parse its JSON response — LLM output was malformed")
                _error_code = "agent_parse_error"
                _error_msg = ("Counter agent returned malformed JSON. The drawing data may be valid — "
                              "please try resubmitting. If the issue persists, check the raw agent output.")
            else:
                emit("WARNING: Counter found 0 fixtures — blank or unreadable drawing")
                _error_code = "blank_drawing"
                _error_msg = "No fixtures detected in provided snippets."
            return {
                "job_id": job_id, "verdict": "BLOCK", "confidence_score": 0.25,
                "confidence_band": "VERY_LOW", "grand_total": 0,
                "fixture_table": [], "fixture_counts": [],
                "checker_attacks": [], "reconciler_responses": [], "violations": [],
                "flags": ["No fixtures detected. Check that snippets contain a fixture schedule and RCP data."],
                "mode": "fast", "error": _error_code, "message": _error_msg
            }

        # I7: Warn about unknown types; Rule 6: auto-flag assumptions
        self._warn_unknown_types(area_counts, fixture_schedule, emit)
        self._apply_rule6_flags(counter_output, emit)

        # Checker — text-based structural review + independent vision count per RCP area
        _rcp_images = [
            {"area_label": s.get("sub_label") or f"RCP-{i+1}", "image_data": s.get("image_data", "")}
            for i, s in enumerate(rcp_snippets)
            if s.get("image_data")
        ]
        emit(f"Checker reviewing count for errors and omissions ({len(_rcp_images)} RCP area(s) via vision)...")
        for _rcp in _rcp_images:
            emit(f"Checker independently reviewing {_rcp['area_label']}...")
        checker_response = self.checker.generate_attacks(
            counter_output, fixture_schedule, area_counts, plan_notes, panel_data,
            rcp_images=_rcp_images,
        )
        checker_attacks = checker_response.data.get("attacks", [])
        if not checker_response.data and checker_response.raw_response:
            emit("WARNING: Checker agent returned empty output — attacks may be missing")
        emit(f"Checker found {len(checker_attacks)} issues ({checker_response.data.get('critical_count', 0)} critical)")

        # No Reconciler in fast mode
        reconciler_output = None

        # Judge
        emit("Judge evaluating against constitutional rules...")
        judge_result = self.judge.evaluate(
            counter_output, checker_attacks, reconciler_output, fixture_schedule, mode="fast"
        )
        emit(f"Judge verdict: {judge_result.get('verdict')}")

        # Confidence
        fixture_counts_list = counter_output.get("fixture_counts", [])
        areas_covered = counter_output.get("areas_covered", [])

        # Fast mode: skip structural notes check (it's unreliable — only checks type presence,
        # not constraint application). Pass has_plan_notes=False so confidence uses neutral 0.5.
        confidence_result = calculate_confidence(
            fixture_counts=fixture_counts_list,
            areas_covered=areas_covered,
            rcp_snippets=rcp_snippets,
            fixture_schedule={"fixtures": fixture_schedule.fixtures},
            checker_attacks=checker_attacks,
            reconciler_responses=[],
            constitutional_violations=judge_result.get("violations", []),
            mode="fast",
            has_panel_schedule=panel_data is not None,
            has_plan_notes=False,  # Neutral — structural check is not semantically meaningful
            notes_addressed=False,
            total_corrected=total_corrected
        )

        # Persist all writes atomically — if any fails, all roll back
        grand_total = counter_output.get("grand_total_fixtures", 0)
        result = self._build_result(
            job_id, counter_output, checker_attacks, reconciler_output,
            judge_result, confidence_result, fixture_schedule, "fast"
        )
        self.db.store_job_results_atomic(
            job_id=job_id,
            fixture_counts=fixture_counts_list,
            attacks=checker_attacks,
            reconciler_responses=[],
            grand_total=grand_total,
            confidence_score=confidence_result["score"],
            confidence_band=confidence_result["band"],
            confidence_features=confidence_result["features_json"],
            violations=judge_result.get("violations", []),
            flags=judge_result.get("flags", []),
            judge_verdict=judge_result.get("verdict", "WARN"),
            full_result=result
        )
        return result

    def _run_strict_mode(
        self,
        job_id: str,
        fixture_schedule: FixtureSchedule,
        area_counts: List[AreaCount],
        plan_notes: List[PlanNote],
        panel_data: Optional[PanelData],
        rcp_snippets: List[Dict],
        emit,
        start_time: float,
        mode: str
    ) -> Dict:
        """Run strict/liability mode: Counter + Checker + Reconciler + Judge."""
        emit(f"Running {mode.upper()} mode pipeline (Counter + Checker + Reconciler + Judge)")

        # Counter
        counter_output, total_corrected, counter_response = self._run_counter_phase(
            fixture_schedule, area_counts, plan_notes, panel_data, emit
        )

        # Short-circuit: zero fixtures — either blank drawing or Counter JSON parse failure
        if counter_output.get("grand_total_fixtures", 0) == 0:
            if counter_response.parse_error:
                emit("ERROR: Counter agent failed to parse its JSON response — LLM output was malformed")
                _error_code = "agent_parse_error"
                _error_msg = ("Counter agent returned malformed JSON. The drawing data may be valid — "
                              "please try resubmitting. If the issue persists, check the raw agent output.")
            else:
                emit("WARNING: Counter found 0 fixtures — blank or unreadable drawing")
                _error_code = "blank_drawing"
                _error_msg = "No fixtures detected in provided snippets."
            return {
                "job_id": job_id, "verdict": "BLOCK", "confidence_score": 0.25,
                "confidence_band": "VERY_LOW", "grand_total": 0,
                "fixture_table": [], "fixture_counts": [],
                "checker_attacks": [], "reconciler_responses": [], "violations": [],
                "flags": ["No fixtures detected. Check that snippets contain a fixture schedule and RCP data."],
                "mode": mode, "error": _error_code, "message": _error_msg
            }

        # I7: Warn about unknown types; Rule 6: auto-flag assumptions
        self._warn_unknown_types(area_counts, fixture_schedule, emit)
        self._apply_rule6_flags(counter_output, emit)

        # Checker — text-based structural review + independent vision count per RCP area
        _rcp_images = [
            {"area_label": s.get("sub_label") or f"RCP-{i+1}", "image_data": s.get("image_data", "")}
            for i, s in enumerate(rcp_snippets)
            if s.get("image_data")
        ]
        emit(f"Checker independently reviewing count ({len(_rcp_images)} RCP area(s) via vision)...")
        for _rcp in _rcp_images:
            emit(f"Checker independently reviewing {_rcp['area_label']}...")
        checker_response = self.checker.generate_attacks(
            counter_output, fixture_schedule, area_counts, plan_notes, panel_data,
            rcp_images=_rcp_images,
        )
        checker_attacks = checker_response.data.get("attacks", [])
        if not checker_response.data and checker_response.raw_response:
            emit("WARNING: Checker agent returned empty output — attacks may be missing")
        emit(f"Checker found {len(checker_attacks)} issues ({checker_response.data.get('critical_count', 0)} critical)")

        # Reconciler
        emit(f"Reconciler addressing {len(checker_attacks)} attacks...")
        reconciler_response = self.reconciler.address_attacks(
            counter_output, checker_attacks, fixture_schedule, area_counts, plan_notes
        )
        reconciler_output = reconciler_response.data
        if not reconciler_output and reconciler_response.raw_response:
            emit("WARNING: Reconciler agent returned empty output — using Counter counts as final")
            reconciler_output = {}
        if reconciler_output and "revised_fixture_counts" not in reconciler_output:
            logger.warning("[ENGINE] WARNING: Reconciler output missing 'revised_fixture_counts' — falling back to Counter counts")
        if reconciler_output and "revised_grand_total" not in reconciler_output:
            logger.warning("[ENGINE] WARNING: Reconciler output missing 'revised_grand_total' — falling back to Counter total")
        reconciler_responses_list = reconciler_output.get("responses", [])
        original_total = counter_output.get("grand_total_fixtures", 0)
        revised_total = reconciler_output.get("revised_grand_total", original_total)
        emit(f"Reconciler: revised total = {revised_total}")

        # I6: Guardrail — flag if Reconciler's revised total deviates >20% from Counter's original
        if original_total > 0 and revised_total > 0:
            deviation = abs(revised_total - original_total) / original_total
            if deviation > 0.20:
                emit(f"WARNING: Reconciler revised total {revised_total} deviates {deviation*100:.0f}% from Counter's {original_total} — flagging as suspicious")
                if isinstance(reconciler_output, dict):
                    reconciler_output = dict(reconciler_output)
                    reconciler_output.setdefault("flags", []).append(
                        f"Reconciler total {revised_total} deviates {deviation*100:.0f}% from Counter ({original_total}) — verify manually"
                    )

        # Judge
        emit("Judge evaluating final takeoff against constitutional rules...")
        judge_result = self.judge.evaluate(
            counter_output, checker_attacks, reconciler_output, fixture_schedule, mode=mode
        )
        emit(f"Judge verdict: {judge_result.get('verdict')}")

        # Confidence
        fixture_counts_list = counter_output.get("fixture_counts", [])
        areas_covered = counter_output.get("areas_covered", [])

        # Determine notes compliance from Reconciler's semantic check.
        # If Reconciler produced no notes_compliance, treat as unverified (conservative).
        # The old structural _check_notes_addressed() was semantically meaningless and removed.
        reconciler_notes = list(reconciler_output.get("notes_compliance", [])) if reconciler_output else []

        # Cross-reference: if Reconciler returned fewer entries than plan_notes, it silently
        # omitted some notes. Inject synthetic non-applied entries for the gap.
        if plan_notes and reconciler_notes and len(reconciler_notes) < len(plan_notes):
            _gap = len(plan_notes) - len(reconciler_notes)
            emit(f"WARNING: Reconciler notes_compliance has {len(reconciler_notes)} entries but "
                 f"{len(plan_notes)} plan notes exist — {_gap} note(s) unverified, treating as not applied")
            for _ in range(_gap):
                reconciler_notes.append({
                    "note": "(unverified plan note)",
                    "applied": False,
                    "finding": "Note absent from Reconciler compliance list — treated as not applied"
                })

        if reconciler_notes:
            notes_addressed = all(n.get("applied", False) for n in reconciler_notes)
        else:
            notes_addressed = False  # unverified — no credit

        confidence_result = calculate_confidence(
            fixture_counts=fixture_counts_list,
            areas_covered=areas_covered,
            rcp_snippets=rcp_snippets,
            fixture_schedule={"fixtures": fixture_schedule.fixtures},
            checker_attacks=checker_attacks,
            reconciler_responses=reconciler_responses_list,
            constitutional_violations=judge_result.get("violations", []),
            mode=mode,
            has_panel_schedule=panel_data is not None,
            has_plan_notes=len(plan_notes) > 0,
            notes_addressed=notes_addressed,
            total_corrected=total_corrected
        )

        # Persist all writes atomically — if any fails, all roll back
        grand_total = reconciler_output.get("revised_grand_total", counter_output.get("grand_total_fixtures", 0))
        result = self._build_result(
            job_id, counter_output, checker_attacks, reconciler_output,
            judge_result, confidence_result, fixture_schedule, mode
        )
        self.db.store_job_results_atomic(
            job_id=job_id,
            fixture_counts=fixture_counts_list,
            attacks=checker_attacks,
            reconciler_responses=reconciler_responses_list,
            grand_total=grand_total,
            confidence_score=confidence_result["score"],
            confidence_band=confidence_result["band"],
            confidence_features=confidence_result["features_json"],
            violations=judge_result.get("violations", []),
            flags=judge_result.get("flags", []),
            judge_verdict=judge_result.get("verdict", "WARN"),
            full_result=result
        )
        return result

    def _build_result(
        self,
        job_id: str,
        counter_output: dict,
        checker_attacks: List[Dict],
        reconciler_output: Optional[dict],
        judge_result: dict,
        confidence_result: dict,
        fixture_schedule: FixtureSchedule,
        mode: str
    ) -> Dict:
        """Build the final result dict for API/CLI consumers."""
        # Use reconciler's revised counts if available
        if reconciler_output and reconciler_output.get("revised_grand_total"):
            grand_total = reconciler_output["revised_grand_total"]
            revised_counts = reconciler_output.get("revised_fixture_counts", {})
        else:
            grand_total = counter_output.get("grand_total_fixtures", 0)
            revised_counts = {}

        # Build final fixture count table for display
        fixture_table = []
        for fc in counter_output.get("fixture_counts", []):
            tag = fc.get("type_tag", "")
            revised = revised_counts.get(tag, {})
            original_total = fc.get("total", 0)
            final_total = revised.get("total", original_total) if revised else original_total
            delta = revised.get("delta", "0") if revised else "0"

            schedule_entry = fixture_schedule.fixtures.get(tag, {})
            desc = schedule_entry.get("description", fc.get("description", "")) if isinstance(schedule_entry, dict) else fc.get("description", "")

            # If Reconciler revised the total, proportionally scale per-area counts.
            # Reconciler only returns type-level totals, so area splits are estimated.
            # Uses largest-remainder algorithm to guarantee sum == final_total.
            original_areas = fc.get("counts_by_area", {})
            if revised and final_total != original_total and original_total > 0:
                counts_by_area = _scale_area_counts(original_areas, final_total)
                area_flags = ["AREA_COUNTS_ESTIMATED: proportionally scaled after reconciliation"]
            else:
                counts_by_area = original_areas
                area_flags = []

            fixture_table.append({
                "type_tag": tag,
                "description": desc,
                "total": final_total,
                "delta": delta,
                "difficulty": fc.get("difficulty", "S"),
                "accessories": fc.get("accessories", []),
                "flags": fc.get("flags", []) + area_flags,
                "counts_by_area": counts_by_area
            })

        # Adversarial log summary
        adv_log = []
        for attack in checker_attacks:
            resolution = None
            verdict_text = None
            if reconciler_output:
                for resp in reconciler_output.get("responses", []):
                    if resp.get("attack_id") == attack.get("attack_id"):
                        resolution = resp.get("explanation")
                        verdict_text = resp.get("verdict")
                        break
            adv_log.append({
                "attack_id": attack.get("attack_id"),
                "severity": attack.get("severity"),
                "category": attack.get("category"),
                "description": attack.get("description"),
                "suggested_correction": attack.get("suggested_correction"),
                "resolution": resolution,
                "verdict": verdict_text
            })

        return {
            "job_id": job_id,
            "mode": mode,
            "grand_total": grand_total,
            "fixture_table": fixture_table,
            "areas_covered": counter_output.get("areas_covered", []),
            "confidence": confidence_result["score"],
            "confidence_band": confidence_result["band"],
            "confidence_explanation": format_confidence_explanation(confidence_result),
            "verdict": judge_result.get("verdict"),
            "violations": judge_result.get("violations", []),
            "flags": judge_result.get("flags", []) + (reconciler_output.get("flags", []) if reconciler_output else []),
            "ruling_summary": judge_result.get("ruling_summary", ""),
            "adversarial_log": adv_log,
            "agent_counts": {
                "counter_types": len(counter_output.get("fixture_counts", [])),
                "checker_attacks": len(checker_attacks),
                "reconciler_responses": len(reconciler_output.get("responses", [])) if reconciler_output else 0
            }
        }
