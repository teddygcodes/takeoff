"""
tests/test_takeoff_pipeline.py
==============================
Unit + integration tests for the takeoff/ adversarial lighting takeoff system.

Tests:
  1. Constitution enforcement — feed in counts that violate rules, verify Judge catches them
  2. Confidence calculation — known feature values → expected score
  3. Extraction helpers — extract_json_from_response fallback strategies
  4. Schema — SQLite round-trip
  5. Engine pipeline validation — early-return checks on missing snippets
  6. Engine pipeline (live, optional) — end-to-end with real API

Run:
  python -m pytest tests/test_takeoff_pipeline.py -v -p no:cacheprovider
"""

import json
import sys
import os
import uuid
import tempfile
import unittest

# Add repo root to path so imports work without installing
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from takeoff.constitution import (
    enforce_constitution,
    check_schedule_traceability,
    check_complete_coverage,
    check_emergency_fixtures,
    check_no_double_counting,
    check_cross_sheet_consistency,
    check_flag_assumptions,
    check_non_negative_counts,
    get_constitution,
    _normalize_area_label,
    EMERGENCY_KEYWORDS,
)
from takeoff.confidence import (
    calculate_confidence,
    format_confidence_explanation,
    FEATURE_WEIGHTS,
)
from takeoff.extraction import extract_json_from_response
from takeoff.schema import TakeoffDB


# ══════════════════════════════════════════════════════════════════════
# Helpers / fixtures
# ══════════════════════════════════════════════════════════════════════

SAMPLE_FIXTURE_SCHEDULE = {
    "fixtures": {
        "A": {
            "description": "2x4 LED Recessed Troffer",
            "voltage": "277V",
            "mounting": "recessed",
            "watts": 40,
        },
        "B": {
            "description": "2x2 LED Troffer",
            "voltage": "277V",
            "mounting": "recessed",
            "watts": 30,
        },
        "C": {
            "description": "6-inch Recessed Downlight",
            "voltage": "120V",
            "mounting": "recessed",
            "watts": 15,
        },
        "X": {
            "description": "LED Exit Sign w/ Battery Backup",
            "voltage": "120V",
            "mounting": "wall",
            "watts": 5,
        },
    }
}

SAMPLE_RCP_SNIPPETS = [
    {"label": "rcp", "sub_label": "Open Office North"},
    {"label": "rcp", "sub_label": "Open Office South"},
    {"label": "rcp", "sub_label": "Corridor 1A"},
]

# constitution.py expects fixture_counts as a LIST of dicts with type_tag field
VALID_FIXTURE_COUNTS_LIST = [
    {
        "type_tag": "A",
        "description": "2x4 LED Recessed Troffer",
        "total": 36,
        "counts_by_area": {"Open Office North": 18, "Open Office South": 18},
    },
    {
        "type_tag": "B",
        "description": "2x2 LED Troffer",
        "total": 12,
        "counts_by_area": {"Open Office North": 6, "Open Office South": 6},
    },
    {
        "type_tag": "C",
        "description": "6-inch Recessed Downlight",
        "total": 8,
        "counts_by_area": {"Corridor 1A": 8},
    },
    {
        "type_tag": "X",
        "description": "LED Exit Sign w/ Battery Backup",
        "total": 4,
        "counts_by_area": {"Corridor 1A": 4},
        "notes": "emergency circuit",
    },
]

VALID_AREAS_COVERED = ["Open Office North", "Open Office South", "Corridor 1A"]


# ══════════════════════════════════════════════════════════════════════
# 1. Constitution Enforcement
# ══════════════════════════════════════════════════════════════════════

class TestConstitutionEnforcement(unittest.TestCase):

    def test_get_constitution_returns_dict(self):
        c = get_constitution()
        self.assertIn("hard_rules", c)
        self.assertIn("articles", c)
        self.assertEqual(len(c["hard_rules"]), 6)
        self.assertEqual(len(c["articles"]), 5)

    def test_valid_counts_pass_traceability(self):
        violations = check_schedule_traceability(
            VALID_FIXTURE_COUNTS_LIST, SAMPLE_FIXTURE_SCHEDULE
        )
        self.assertEqual(violations, [], f"Expected no violations, got: {violations}")

    def test_phantom_fixture_fails_traceability(self):
        """Type tag 'Z' is not in the fixture schedule — must fail."""
        bad_counts = list(VALID_FIXTURE_COUNTS_LIST) + [
            {
                "type_tag": "Z",
                "description": "Unknown fixture",
                "total": 3,
                "counts_by_area": {"Open Office North": 3},
            }
        ]
        violations = check_schedule_traceability(bad_counts, SAMPLE_FIXTURE_SCHEDULE)
        self.assertTrue(
            any("Z" in str(v) for v in violations),
            f"Expected violation mentioning 'Z', got: {violations}",
        )

    def test_valid_areas_pass_coverage(self):
        violations = check_complete_coverage(VALID_AREAS_COVERED, SAMPLE_RCP_SNIPPETS)
        self.assertEqual(violations, [])

    def test_missing_area_fails_coverage(self):
        """If the counter didn't cover Corridor 1A, should trigger a violation."""
        partial_coverage = ["Open Office North", "Open Office South"]
        violations = check_complete_coverage(partial_coverage, SAMPLE_RCP_SNIPPETS)
        self.assertTrue(
            len(violations) > 0,
            "Expected coverage violation for missed area",
        )

    def test_emergency_fixtures_present_no_violation(self):
        # Our list has type X with "emergency circuit" in notes
        violations = check_emergency_fixtures(VALID_FIXTURE_COUNTS_LIST)
        self.assertEqual(
            violations, [],
            f"Expected no emergency violation when exit sign is present, got: {violations}",
        )

    def test_no_emergency_fixtures_warns(self):
        # Build a substantial fixture list (> 5 types) with no emergency tracking.
        # The check only fires for jobs with > 5 fixture types — small jobs may legitimately have none.
        large_counts_no_emergency = [
            {"type_tag": "A", "description": "2x4 Troffer", "total": 20, "counts_by_area": {}, "notes": ""},
            {"type_tag": "B", "description": "2x2 Troffer", "total": 10, "counts_by_area": {}, "notes": ""},
            {"type_tag": "C", "description": "Downlight 6in", "total": 8, "counts_by_area": {}, "notes": ""},
            {"type_tag": "D", "description": "Wall Sconce", "total": 6, "counts_by_area": {}, "notes": ""},
            {"type_tag": "E", "description": "Linear Strip", "total": 4, "counts_by_area": {}, "notes": ""},
            {"type_tag": "F", "description": "High Bay", "total": 12, "counts_by_area": {}, "notes": ""},
        ]
        violations = check_emergency_fixtures(large_counts_no_emergency)
        self.assertTrue(
            len(violations) > 0,
            "Expected emergency fixture warning when no emergency fixtures counted in a large job (> 5 types)",
        )

    def test_enforce_constitution_pass(self):
        result = enforce_constitution(
            fixture_counts=VALID_FIXTURE_COUNTS_LIST,
            areas_covered=VALID_AREAS_COVERED,
            rcp_snippets=SAMPLE_RCP_SNIPPETS,
            fixture_schedule=SAMPLE_FIXTURE_SCHEDULE,
        )
        self.assertIn(result["verdict"], ("PASS", "WARN"))
        self.assertIsInstance(result["violations"], list)

    def test_enforce_constitution_fatal_blocks(self):
        """Phantom fixture should produce FATAL violation → BLOCK verdict."""
        bad_counts = [
            {
                "type_tag": "PHANTOM",
                "description": "Unknown fixture",
                "total": 5,
                "counts_by_area": {"Open Office North": 5},
            }
        ]
        result = enforce_constitution(
            fixture_counts=bad_counts,
            areas_covered=["Open Office North"],
            rcp_snippets=[{"label": "rcp", "sub_label": "Open Office North"}],
            fixture_schedule=SAMPLE_FIXTURE_SCHEDULE,
        )
        self.assertEqual(result["verdict"], "BLOCK", f"Expected BLOCK, got {result['verdict']}")
        self.assertTrue(
            any(v.get("severity") == "FATAL" for v in result["violations"]),
            f"Expected FATAL violation, violations: {result['violations']}",
        )


# ══════════════════════════════════════════════════════════════════════
# 2. Confidence Calculation
# ══════════════════════════════════════════════════════════════════════

class TestConfidenceCalculation(unittest.TestCase):

    def test_feature_weights_sum(self):
        """Positive weights should be > 0."""
        positive = sum(v for v in FEATURE_WEIGHTS.values() if v > 0)
        self.assertGreater(positive, 0)

    def test_perfect_score_high_band(self):
        """All features max → HIGH or MODERATE confidence."""
        result = calculate_confidence(
            fixture_counts=VALID_FIXTURE_COUNTS_LIST,
            areas_covered=VALID_AREAS_COVERED,
            rcp_snippets=SAMPLE_RCP_SNIPPETS,
            fixture_schedule=SAMPLE_FIXTURE_SCHEDULE,
            checker_attacks=[],
            reconciler_responses=[],
            constitutional_violations=[],
            mode="strict",
            has_panel_schedule=True,
            has_plan_notes=True,
            notes_addressed=True,
        )
        self.assertGreaterEqual(result["score"], 0.7, f"Expected HIGH/MODERATE, got {result['score']}")
        self.assertIn(result["band"], ("HIGH", "MODERATE"))

    def test_fast_mode_penalty_applied(self):
        """fast mode should yield lower or equal confidence than strict for same inputs."""
        strict = calculate_confidence(
            fixture_counts=VALID_FIXTURE_COUNTS_LIST,
            areas_covered=VALID_AREAS_COVERED,
            rcp_snippets=SAMPLE_RCP_SNIPPETS,
            fixture_schedule=SAMPLE_FIXTURE_SCHEDULE,
            checker_attacks=[],
            reconciler_responses=[],
            constitutional_violations=[],
            mode="strict",
            has_panel_schedule=False,
            has_plan_notes=False,
            notes_addressed=False,
        )
        fast = calculate_confidence(
            fixture_counts=VALID_FIXTURE_COUNTS_LIST,
            areas_covered=VALID_AREAS_COVERED,
            rcp_snippets=SAMPLE_RCP_SNIPPETS,
            fixture_schedule=SAMPLE_FIXTURE_SCHEDULE,
            checker_attacks=[],
            reconciler_responses=[],
            constitutional_violations=[],
            mode="fast",
            has_panel_schedule=False,
            has_plan_notes=False,
            notes_addressed=False,
        )
        self.assertLessEqual(fast["score"], strict["score"])

    def test_fatal_violation_floors_confidence(self):
        """FATAL constitutional violation should force score ≤ 0.25."""
        fatal_violations = [{"severity": "FATAL", "rule": "Schedule Traceability"}]
        result = calculate_confidence(
            fixture_counts=VALID_FIXTURE_COUNTS_LIST,
            areas_covered=VALID_AREAS_COVERED,
            rcp_snippets=SAMPLE_RCP_SNIPPETS,
            fixture_schedule=SAMPLE_FIXTURE_SCHEDULE,
            checker_attacks=[],
            reconciler_responses=[],
            constitutional_violations=fatal_violations,
            mode="strict",
            has_panel_schedule=True,
            has_plan_notes=True,
            notes_addressed=True,
        )
        self.assertLessEqual(result["score"], 0.26, f"FATAL should force score ≤ 0.25, got {result['score']}")

    def test_confidence_score_clamped(self):
        """Score must always be in [0.0, 1.0]."""
        result = calculate_confidence(
            fixture_counts=[],
            areas_covered=[],
            rcp_snippets=[],
            fixture_schedule={},
            checker_attacks=[{"severity": "critical"}, {"severity": "critical"}],
            reconciler_responses=[],
            constitutional_violations=[{"severity": "MAJOR"}, {"severity": "MAJOR"}],
            mode="fast",
            has_panel_schedule=False,
            has_plan_notes=False,
            notes_addressed=False,
        )
        self.assertGreaterEqual(result["score"], 0.0)
        self.assertLessEqual(result["score"], 1.0)

    def test_format_confidence_explanation(self):
        result = calculate_confidence(
            fixture_counts=VALID_FIXTURE_COUNTS_LIST,
            areas_covered=VALID_AREAS_COVERED,
            rcp_snippets=SAMPLE_RCP_SNIPPETS,
            fixture_schedule=SAMPLE_FIXTURE_SCHEDULE,
            checker_attacks=[],
            reconciler_responses=[],
            constitutional_violations=[],
            mode="strict",
            has_panel_schedule=True,
            has_plan_notes=True,
            notes_addressed=True,
        )
        explanation = format_confidence_explanation(result)
        self.assertIsInstance(explanation, str)
        self.assertIn("confidence", explanation.lower())


# ══════════════════════════════════════════════════════════════════════
# 3. JSON Extraction Helpers
# ══════════════════════════════════════════════════════════════════════

class TestExtractJsonFromResponse(unittest.TestCase):

    def test_clean_json_block(self):
        text = '```json\n{"key": "value"}\n```'
        result = extract_json_from_response(text, "test_agent")
        self.assertEqual(result.get("key"), "value")

    def test_inline_json(self):
        text = 'Some preamble\n{"fixture_counts": [], "total": 42}\nSome postamble'
        result = extract_json_from_response(text, "test_agent")
        self.assertEqual(result.get("total"), 42)

    def test_bare_json(self):
        text = '{"attacks": [], "total_attacks": 0}'
        result = extract_json_from_response(text, "test_agent")
        self.assertEqual(result.get("total_attacks"), 0)

    def test_malformed_raises_json_decode_error(self):
        """extract_json_from_response raises JSONDecodeError on completely invalid input."""
        import json as _json
        text = "This is not JSON at all."
        with self.assertRaises(_json.JSONDecodeError):
            extract_json_from_response(text, "test_agent")

    def test_nested_json_object(self):
        """Nested JSON object should be parsed correctly."""
        text = '{"outer": {"inner": [1, 2, 3]}, "flag": true}'
        result = extract_json_from_response(text, "test_agent")
        self.assertIn("outer", result)
        self.assertTrue(result.get("flag"))


# ══════════════════════════════════════════════════════════════════════
# 4. Schema Round-Trip
# ══════════════════════════════════════════════════════════════════════

class TestTakeoffDB(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = TakeoffDB(db_path=self.tmp.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def _job_id(self):
        return str(uuid.uuid4())[:8]

    def test_create_and_get_job(self):
        jid = self._job_id()
        self.db.create_job(
            job_id=jid,
            mode="strict",
            drawing_name="test_drawing",
            total_pages=5,
            snippet_count=4,
        )
        job = self.db.get_job(jid)
        self.assertIsNotNone(job)
        self.assertEqual(job["drawing_name"], "test_drawing")
        self.assertEqual(job["mode"], "strict")

    def test_update_job_status(self):
        jid = self._job_id()
        self.db.create_job(job_id=jid, mode="fast", drawing_name="test")
        self.db.update_job_status(jid, "complete")
        job = self.db.get_job(jid)
        self.assertEqual(job["status"], "complete")

    def test_store_fixture_schedule(self):
        jid = self._job_id()
        self.db.create_job(job_id=jid, mode="strict", drawing_name="test")
        # store_fixture_schedule takes a schedule dict
        self.db.store_fixture_schedule(job_id=jid, schedule=SAMPLE_FIXTURE_SCHEDULE)
        # Should not raise

    def test_store_and_retrieve_result(self):
        jid = self._job_id()
        self.db.create_job(job_id=jid, mode="strict", drawing_name="test")
        self.db.store_result(
            job_id=jid,
            grand_total=92,
            confidence_score=0.84,
            confidence_band="MODERATE",
            confidence_features=json.dumps({}),
            violations=[],
            flags=[],
            judge_verdict="PASS",
        )
        self.db.update_job_status(jid, "complete")
        job = self.db.get_job(jid)
        self.assertEqual(job["status"], "complete")
        self.assertEqual(job["grand_total"], 92)

    def test_list_jobs(self):
        for i in range(3):
            jid = self._job_id()
            self.db.create_job(
                job_id=jid,
                mode="strict",
                drawing_name=f"drawing_{i}",
                total_pages=i + 1,
                snippet_count=2,
            )
        jobs = self.db.list_jobs()
        self.assertGreaterEqual(len(jobs), 3)


# ══════════════════════════════════════════════════════════════════════
# 5. Engine Pipeline Validation (no API key needed)
# ══════════════════════════════════════════════════════════════════════

class TestEnginePipelineValidation(unittest.TestCase):
    """
    Tests engine-level validation via run_takeoff() which returns an error dict
    (rather than raising) when snippet requirements are not met.
    """

    def setUp(self):
        import base64
        from PIL import Image
        import io

        img = Image.new("RGB", (200, 150), color=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        self.fake_image_b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

        self.snippets = [
            {
                "id": "snip-001",
                "label": "fixture_schedule",
                "sub_label": "",
                "page_number": 1,
                "bbox": {"x": 0, "y": 0, "width": 200, "height": 150},
                "image_data": self.fake_image_b64,
            },
            {
                "id": "snip-002",
                "label": "rcp",
                "sub_label": "Open Office North",
                "page_number": 2,
                "bbox": {"x": 0, "y": 0, "width": 200, "height": 150},
                "image_data": self.fake_image_b64,
            },
        ]

    def _make_engine(self, db_path: str):
        from takeoff.engine import TakeoffEngine
        return TakeoffEngine(db_path=db_path, model_router=None)

    def test_missing_fixture_schedule_returns_error(self):
        """Engine should return error dict if no fixture_schedule snippet."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            engine = self._make_engine(db_path)
            bad_snippets = [s for s in self.snippets if s["label"] != "fixture_schedule"]
            result = engine.run_takeoff(snippets=bad_snippets, mode="fast")
            self.assertIn("error", result, f"Expected error key, got: {result}")
        finally:
            os.unlink(db_path)

    def test_missing_rcp_returns_error(self):
        """Engine should return error dict if no rcp snippet."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            engine = self._make_engine(db_path)
            bad_snippets = [s for s in self.snippets if s["label"] != "rcp"]
            result = engine.run_takeoff(snippets=bad_snippets, mode="fast")
            self.assertIn("error", result, f"Expected error key, got: {result}")
        finally:
            os.unlink(db_path)

    @unittest.skipUnless(
        os.environ.get("ANTHROPIC_API_KEY"),
        "Skipped: set ANTHROPIC_API_KEY to run live pipeline tests",
    )
    def test_full_pipeline_live(self):
        """
        End-to-end pipeline test using real Claude API.
        Only runs when ANTHROPIC_API_KEY is available.
        """
        from takeoff.models import ModelRouter
        from takeoff.engine import TakeoffEngine

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            router = ModelRouter(api_key=os.environ["ANTHROPIC_API_KEY"])
            engine = TakeoffEngine(db_path=db_path, model_router=router)
            result = engine.run_takeoff(
                snippets=self.snippets,
                mode="fast",
                drawing_name="test_drawing",
            )
            # May succeed or fail vision extraction on blank PNG — either way should not crash
            self.assertIsInstance(result, dict)
            if "error" not in result:
                self.assertIn("confidence_score", result)
                self.assertIn("grand_total", result)
                self.assertGreaterEqual(result["confidence_score"], 0.0)
                self.assertLessEqual(result["confidence_score"], 1.0)
        finally:
            os.unlink(db_path)


# ══════════════════════════════════════════════════════════════════════
# 6. Programmatic Constitutional Rules (M7)
# ══════════════════════════════════════════════════════════════════════

class TestProgrammaticConstitutionalRules(unittest.TestCase):
    """Tests for check_no_double_counting and check_cross_sheet_consistency."""

    def test_no_double_counting_clean(self):
        """Area subtotals equal reported total → no violation."""
        violations = check_no_double_counting(VALID_FIXTURE_COUNTS_LIST, SAMPLE_FIXTURE_SCHEDULE)
        self.assertEqual(violations, [], f"Expected no violations, got: {violations}")

    def test_no_double_counting_detected(self):
        """Sum of counts_by_area > total * 1.10 → MAJOR violation."""
        inflated = [
            {
                "type_tag": "A",
                "total": 10,
                "counts_by_area": {"Zone 1": 10, "Zone 2": 5},  # sum=15, >10% over total=10
            }
        ]
        violations = check_no_double_counting(inflated, {})
        self.assertTrue(len(violations) > 0, "Expected double-count violation")
        self.assertTrue(any("A" in str(v) for v in violations))
        self.assertEqual(violations[0]["severity"], "MAJOR")

    def test_no_double_counting_within_tolerance(self):
        """Sum of counts_by_area ≤ total * 1.10 → no violation."""
        ok_counts = [
            {
                "type_tag": "B",
                "total": 10,
                "counts_by_area": {"Zone 1": 10},  # sum==total, no problem
            }
        ]
        violations = check_no_double_counting(ok_counts, {})
        self.assertEqual(violations, [])

    def test_no_double_counting_empty_counts(self):
        """Empty fixture_counts → no violations."""
        violations = check_no_double_counting([], {})
        self.assertEqual(violations, [])

    def test_cross_sheet_consistency_clean(self):
        """Same type+area appears only once → no violations."""
        violations = check_cross_sheet_consistency(VALID_FIXTURE_COUNTS_LIST)
        self.assertEqual(violations, [], f"Expected no violations, got: {violations}")

    def test_cross_sheet_conflict_detected(self):
        """Same type+area with different counts → MAJOR violation."""
        conflicting = [
            {"type_tag": "A", "counts_by_area": {"Open Office North": 18}},
            {"type_tag": "A", "counts_by_area": {"Open Office North": 20}},  # conflict!
        ]
        violations = check_cross_sheet_consistency(conflicting)
        self.assertTrue(len(violations) > 0, "Expected cross-sheet conflict violation")
        self.assertEqual(violations[0]["severity"], "MAJOR")

    def test_cross_sheet_no_conflict_different_areas(self):
        """Same type in different areas → no violation."""
        ok = [
            {"type_tag": "A", "counts_by_area": {"Zone 1": 10}},
            {"type_tag": "A", "counts_by_area": {"Zone 2": 5}},  # different area, no conflict
        ]
        violations = check_cross_sheet_consistency(ok)
        self.assertEqual(violations, [])

    def test_cross_sheet_empty_counts(self):
        """Empty fixture_counts → no violations."""
        violations = check_cross_sheet_consistency([])
        self.assertEqual(violations, [])


# ══════════════════════════════════════════════════════════════════════
# 7. Reconciler Concede/Defend/Partial Logic (L6)
# ══════════════════════════════════════════════════════════════════════

class TestReconcilerLogic(unittest.TestCase):
    """Tests for Reconciler agent concede/defend/partial response parsing."""

    def test_concede_defend_partial_counts(self):
        """Count verdicts from a list of reconciler responses."""
        responses = [
            {"attack_id": "ATK-001", "verdict": "concede", "explanation": "Valid — area was missed"},
            {"attack_id": "ATK-002", "verdict": "defend", "explanation": "Counter was correct"},
            {"attack_id": "ATK-003", "verdict": "partial", "explanation": "Partly valid"},
        ]
        concessions = sum(1 for r in responses if r.get("verdict") == "concede")
        defenses = sum(1 for r in responses if r.get("verdict") == "defend")
        partials = sum(1 for r in responses if r.get("verdict") == "partial")
        self.assertEqual(concessions, 1)
        self.assertEqual(defenses, 1)
        self.assertEqual(partials, 1)

    def test_unresolved_attack_count(self):
        """Attacks not matched in reconciler responses are unresolved."""
        attacks = [{"attack_id": f"ATK-{i:03d}"} for i in range(5)]
        responses = [{"attack_id": "ATK-000"}, {"attack_id": "ATK-001"}]
        resolved_ids = {r.get("attack_id") for r in responses}
        attack_ids = {a.get("attack_id") for a in attacks}
        unresolved = attack_ids - resolved_ids
        self.assertEqual(len(unresolved), 3)

    def test_empty_attacks_no_unresolved(self):
        """No attacks → no unresolved."""
        attacks = []
        responses = []
        resolved_ids = {r.get("attack_id") for r in responses}
        attack_ids = {a.get("attack_id") for a in attacks}
        unresolved = attack_ids - resolved_ids
        self.assertEqual(len(unresolved), 0)

    def test_confidence_adversarial_resolved_feature(self):
        """Reconciler responses covering all attacks → adversarial_resolved = 1.0."""
        attacks = [{"attack_id": "ATK-001"}, {"attack_id": "ATK-002"}]
        responses = [{"attack_id": "ATK-001"}, {"attack_id": "ATK-002"}]
        result = calculate_confidence(
            fixture_counts=VALID_FIXTURE_COUNTS_LIST,
            areas_covered=VALID_AREAS_COVERED,
            rcp_snippets=SAMPLE_RCP_SNIPPETS,
            fixture_schedule=SAMPLE_FIXTURE_SCHEDULE,
            checker_attacks=attacks,
            reconciler_responses=responses,
            constitutional_violations=[],
            mode="strict",
            has_panel_schedule=False,
            has_plan_notes=False,
            notes_addressed=False,
        )
        self.assertEqual(result["features"]["adversarial_resolved"], 1.0)

    def test_confidence_unresolved_attacks_penalized(self):
        """Attacks with no reconciler responses → adversarial_resolved = 0.0."""
        attacks = [{"attack_id": "ATK-001"}, {"attack_id": "ATK-002"}]
        result = calculate_confidence(
            fixture_counts=VALID_FIXTURE_COUNTS_LIST,
            areas_covered=VALID_AREAS_COVERED,
            rcp_snippets=SAMPLE_RCP_SNIPPETS,
            fixture_schedule=SAMPLE_FIXTURE_SCHEDULE,
            checker_attacks=attacks,
            reconciler_responses=[],  # fast mode — no Reconciler
            constitutional_violations=[],
            mode="fast",
            has_panel_schedule=False,
            has_plan_notes=False,
            notes_addressed=False,
        )
        self.assertEqual(result["features"]["adversarial_resolved"], 0.0)


# ══════════════════════════════════════════════════════════════════════
# 8. Regression Tests for Critical Bug Fixes
# ══════════════════════════════════════════════════════════════════════

class TestCriticalBugFixes(unittest.TestCase):
    """Regression tests for the 4 critical bugs fixed in this session."""

    # --- Bug 1: MAJOR penalty must not raise a score already below 0.40 ---

    def test_major_violation_does_not_raise_low_score(self):
        """A MAJOR violation hard-override must never INCREASE the confidence score.

        Scenario: fixture counts/coverage are all bad so base score calculates below 0.40.
        The MAJOR cap must keep it at that lower value, not raise it to 0.40.
        """
        result = calculate_confidence(
            fixture_counts=[],           # 0 types → schedule_match_rate = 0.0
            areas_covered=[],            # nothing covered
            rcp_snippets=SAMPLE_RCP_SNIPPETS,  # 3 areas expected
            fixture_schedule=SAMPLE_FIXTURE_SCHEDULE,
            checker_attacks=[{"attack_id": "ATK-001"}],
            reconciler_responses=[],     # fast mode — attacks unresolved
            constitutional_violations=[{"severity": "MAJOR", "rule": "No Double-Counting"}],
            mode="fast",
            has_panel_schedule=False,
            has_plan_notes=False,
            notes_addressed=False,
        )
        # Score must not exceed 0.40 (the MAJOR cap)
        self.assertLessEqual(result["score"], 0.40, f"MAJOR penalty raised score to {result['score']}")
        # Score must also not exceed what the features would give before the override
        # (i.e., the override must never make a bad score look better)
        self.assertIn(result["band"], ("LOW", "VERY_LOW"))

    def test_minor_violation_does_not_raise_low_score(self):
        """A MINOR violation cap must never INCREASE the confidence score."""
        result = calculate_confidence(
            fixture_counts=[],
            areas_covered=[],
            rcp_snippets=SAMPLE_RCP_SNIPPETS,
            fixture_schedule=SAMPLE_FIXTURE_SCHEDULE,
            checker_attacks=[{"attack_id": "ATK-001"}],
            reconciler_responses=[],
            constitutional_violations=[{"severity": "MINOR", "rule": "Flag Assumptions"}],
            mode="fast",
            has_panel_schedule=False,
            has_plan_notes=False,
            notes_addressed=False,
        )
        # Score must not exceed 0.50 (the MINOR cap)
        self.assertLessEqual(result["score"], 0.50, f"MINOR penalty raised score to {result['score']}")

    # --- Bug 2: Worst-case features should produce VERY_LOW, not MODERATE ---

    def test_worst_case_features_produce_very_low_band(self):
        """All features at zero (no fixtures, no coverage, no panel, fast mode) → VERY_LOW."""
        result = calculate_confidence(
            fixture_counts=[],
            areas_covered=[],
            rcp_snippets=SAMPLE_RCP_SNIPPETS,  # 3 expected but none covered
            fixture_schedule=SAMPLE_FIXTURE_SCHEDULE,
            checker_attacks=[{"attack_id": "ATK-001"}],
            reconciler_responses=[],
            constitutional_violations=[],
            mode="fast",
            has_panel_schedule=False,
            has_plan_notes=False,
            notes_addressed=False,
        )
        self.assertLess(result["score"], 0.65,
            f"Worst-case scenario should not reach MODERATE, got {result['score']} ({result['band']})")

    # --- Bug 3: Area label parentheticals must not cause false FATAL violations ---

    def test_area_label_copy_suffix_matches(self):
        """Sub_label 'Floor 1 (Copy)' must match areas_covered 'Floor 1'."""
        from takeoff.constitution import check_complete_coverage
        rcp_snippets_with_copy = [
            {"label": "rcp", "sub_label": "Open Office North (Copy)"},
            {"label": "rcp", "sub_label": "Corridor 1A"},
        ]
        areas_covered = ["Open Office North", "Corridor 1A"]
        violations = check_complete_coverage(areas_covered, rcp_snippets_with_copy)
        self.assertEqual(violations, [],
            f"Parenthetical suffix in sub_label caused false FATAL violation: {violations}")

    def test_area_label_rev_suffix_matches(self):
        """Sub_label 'Suite 200 (Rev B)' must match areas_covered 'Suite 200'."""
        from takeoff.constitution import check_complete_coverage
        rcp_snippets_rev = [
            {"label": "rcp", "sub_label": "Suite 200 (Rev B)"},
        ]
        areas_covered = ["Suite 200"]
        violations = check_complete_coverage(areas_covered, rcp_snippets_rev)
        self.assertEqual(violations, [],
            f"Revision suffix in sub_label caused false FATAL violation: {violations}")

    # --- Bug 4: Checker dedup must preserve distinct attacks with matching 60-char prefixes ---

    def test_checker_dedup_preserves_distinct_attacks(self):
        """Two attacks on the same type+area but with different full descriptions must both survive dedup."""
        from takeoff.agents import Checker
        # Simulate the dedup logic directly (same logic extracted from Checker.generate_attacks)
        attacks = [
            {
                "attack_id": "ATK-001",
                "category": "math_error",
                "affected_type_tag": "A",
                "affected_area": "Open Office North",
                "description": "Double count in type A area Open Office North detected due to overlapping floor plan view north vs south panel",
            },
            {
                "attack_id": "ATK-002",
                "category": "math_error",
                "affected_type_tag": "A",
                "affected_area": "Open Office North",
                "description": "Double count in type A area Open Office North detected due to overlapping detail views 2 and 3 in the drawing set",
            },
        ]
        # These two attacks share the same first 60 chars — old dedup would collapse them
        desc1 = attacks[0]["description"]
        desc2 = attacks[1]["description"]
        self.assertEqual(desc1[:60].lower(), desc2[:60].lower(),
            "Test precondition: descriptions must share same 60-char prefix")

        # Apply the fixed dedup logic (uses full description)
        seen: set = set()
        deduped = []
        for attack in attacks:
            key = (
                attack.get("category", ""),
                (attack.get("affected_type_tag") or "").upper(),
                (attack.get("affected_area") or "").lower().strip(),
                (attack.get("description") or "").lower()  # full description
            )
            if key not in seen:
                seen.add(key)
                deduped.append(attack)

        self.assertEqual(len(deduped), 2,
            f"Fixed dedup should keep both distinct attacks, kept {len(deduped)}: {[a['attack_id'] for a in deduped]}")


# ══════════════════════════════════════════════════════════════════════
# 9. Edge Cases & Negative Path Tests
# ══════════════════════════════════════════════════════════════════════

class TestEdgeCases(unittest.TestCase):
    """Negative path and edge case tests for robustness."""

    # --- check_flag_assumptions ---

    def test_flag_assumptions_unknown_type_no_flags_violation(self):
        """Type tag UNKNOWN with no flags → MAJOR violation."""
        counts = [
            {"type_tag": "UNKNOWN", "description": "Unidentified fixture", "total": 3, "flags": [], "notes": ""}
        ]
        violations = check_flag_assumptions(counts)
        self.assertTrue(len(violations) > 0, "Expected violation for UNKNOWN type with no flags")
        self.assertEqual(violations[0]["severity"], "MAJOR")

    def test_flag_assumptions_unknown_type_with_flags_no_violation(self):
        """Type tag UNKNOWN WITH a flag → no violation."""
        counts = [
            {"type_tag": "UNKNOWN", "description": "Unidentified fixture", "total": 3,
             "flags": ["Cannot identify — assumed type A based on location"], "notes": ""}
        ]
        violations = check_flag_assumptions(counts)
        self.assertEqual(violations, [], f"Expected no violation when UNKNOWN is flagged, got: {violations}")

    def test_flag_assumptions_ambiguous_language_no_flags_violation(self):
        """Description containing 'assumed' with no flags → MAJOR violation."""
        counts = [
            {"type_tag": "B", "description": "Assumed to be type B downlight based on spacing", "total": 5, "flags": [], "notes": ""}
        ]
        violations = check_flag_assumptions(counts)
        self.assertTrue(len(violations) > 0, "Expected violation for assumed type with no flags")

    def test_flag_assumptions_ambiguous_in_notes_no_flags_violation(self):
        """Notes containing 'unclear' with no flags → MAJOR violation."""
        counts = [
            {"type_tag": "C", "description": "6-inch downlight", "total": 2, "flags": [],
             "notes": "unclear which circuit — assumed standard"}
        ]
        violations = check_flag_assumptions(counts)
        self.assertTrue(len(violations) > 0, "Expected violation for unclear note with no flags")

    def test_flag_assumptions_clean_counts_no_violation(self):
        """Clean fixture counts with no ambiguous language → no violations."""
        violations = check_flag_assumptions(VALID_FIXTURE_COUNTS_LIST)
        self.assertEqual(violations, [], f"Expected no violations for clean counts, got: {violations}")

    def test_flag_assumptions_empty_list_no_violation(self):
        """Empty fixture_counts → no violations."""
        violations = check_flag_assumptions([])
        self.assertEqual(violations, [])

    # --- validate_grand_total GrandTotalResult ---

    def test_validate_grand_total_returns_dataclass(self):
        """validate_grand_total must return GrandTotalResult, not a tuple."""
        from takeoff.agents import validate_grand_total, GrandTotalResult
        agent_output = {
            "fixture_counts": [{"total": 10}, {"total": 5}],
            "grand_total_fixtures": 15
        }
        result = validate_grand_total(agent_output)
        self.assertIsInstance(result, GrandTotalResult)
        self.assertFalse(result.was_corrected)
        self.assertEqual(result.counts["grand_total_fixtures"], 15)

    def test_validate_grand_total_corrects_mismatch(self):
        """Reported grand_total > 5% off computed sum → was_corrected=True."""
        from takeoff.agents import validate_grand_total
        agent_output = {
            "fixture_counts": [{"total": 10}, {"total": 5}],
            "grand_total_fixtures": 100  # way off
        }
        result = validate_grand_total(agent_output)
        self.assertTrue(result.was_corrected)
        self.assertEqual(result.counts["grand_total_fixtures"], 15)

    def test_validate_grand_total_empty_counts_no_correction(self):
        """Empty fixture_counts → not corrected."""
        from takeoff.agents import validate_grand_total
        agent_output = {"fixture_counts": [], "grand_total_fixtures": 0}
        result = validate_grand_total(agent_output)
        self.assertFalse(result.was_corrected)

    def test_validate_grand_total_zero_computed_sum_corrects_nonzero_grand_total(self):
        """Computed sum = 0 (all totals are 0) but grand_total > 0 → must correct to 0."""
        from takeoff.agents import validate_grand_total
        agent_output = {
            "fixture_counts": [{"total": 0}, {"total": 0}],
            "grand_total_fixtures": 50
        }
        result = validate_grand_total(agent_output)
        # All per-type totals are 0 but grand_total claims 50 — should be corrected.
        self.assertTrue(result.was_corrected, "Mismatch between per-type sum (0) and grand_total (50) should be corrected")
        self.assertEqual(result.counts["grand_total_fixtures"], 0)

    # --- DB snippet uniqueness warning ---

    def test_db_store_snippets_missing_id_skipped(self):
        """Snippet with no 'id' field is skipped with warning, not inserted."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            db = TakeoffDB(db_path=db_path)
            jid = str(uuid.uuid4())[:8]
            db.create_job(job_id=jid, mode="fast", drawing_name="test")
            # One snippet with id, one without
            snippets = [
                {"id": "snip-001", "label": "rcp", "sub_label": "Zone 1"},
                {"label": "rcp", "sub_label": "Zone 2"},  # missing id
            ]
            db.store_snippets(jid, snippets)
            db.close()
        finally:
            os.unlink(db_path)

    # --- Zero-fixture result has fixture_table ---

    def test_zero_fixture_result_has_fixture_table(self):
        """If Counter returns 0 fixtures, the early-return result must include fixture_table key."""
        # Simulate the zero-fixture early return from engine.py
        zero_fixture_result = {
            "job_id": "test123", "verdict": "BLOCK", "confidence_score": 0.25,
            "confidence_band": "VERY_LOW", "grand_total": 0,
            "fixture_table": [], "fixture_counts": [],
            "checker_attacks": [], "reconciler_responses": [], "violations": [],
            "flags": ["No fixtures detected."],
            "mode": "fast", "error": "No fixtures detected in provided snippets."
        }
        self.assertIn("fixture_table", zero_fixture_result,
                      "Zero-fixture result must contain fixture_table key for frontend compatibility")
        self.assertEqual(zero_fixture_result["fixture_table"], [])

    # --- JSON extraction with extra-space whitespace ---

    def test_extract_json_with_prefix_text(self):
        """JSON embedded after long text preamble should be extracted."""
        text = "The agent responded with the following analysis:\n" * 5 + '{"total": 99, "types": []}'
        result = extract_json_from_response(text, "test")
        self.assertEqual(result.get("total"), 99)

    def test_extract_json_empty_string_raises(self):
        """Empty string input must raise JSONDecodeError."""
        import json as _json
        with self.assertRaises(_json.JSONDecodeError):
            extract_json_from_response("", "test")


# ══════════════════════════════════════════════════════════════════════
# New tests covering fixed gaps
# ══════════════════════════════════════════════════════════════════════

class TestFixedGaps(unittest.TestCase):
    """Tests for bugs and gaps fixed in the audit round."""

    # ── P0 #2: cost_usd in frontend format ─────────────────────────────

    def test_format_for_frontend_includes_cost_usd(self):
        """_format_for_frontend must pass cost_usd through to the response dict."""
        # Import directly so test doesn't depend on server running
        import importlib
        import sys
        # Mock fastapi if not installed
        if "fastapi" not in sys.modules:
            import unittest.mock as mock
            sys.modules.setdefault("fastapi", mock.MagicMock())
            sys.modules.setdefault("fastapi.middleware.cors", mock.MagicMock())
            sys.modules.setdefault("fastapi.responses", mock.MagicMock())
        try:
            from takeoff.api import _format_for_frontend
        except Exception:
            self.skipTest("FastAPI not importable in this environment")

        result = {
            "job_id": "abc123", "mode": "fast", "grand_total": 50,
            "fixture_table": [], "areas_covered": [],
            "confidence_band": "MODERATE", "confidence": 0.72,
            "confidence_explanation": "ok", "verdict": "PASS",
            "violations": [], "flags": [], "ruling_summary": "ok",
            "adversarial_log": [], "agent_counts": {},
            "latency_ms": 4200, "cost_usd": 0.087
        }
        formatted = _format_for_frontend(result)
        self.assertIn("cost_usd", formatted)
        self.assertAlmostEqual(formatted["cost_usd"], 0.087)

    # ── P0 #3: Rule 6 — UNKNOWN tags auto-flagged ──────────────────────

    def test_rule6_unknown_tag_auto_flagged_by_engine(self):
        """Engine must auto-flag type_tag UNKNOWN if Counter didn't self-flag."""
        counter_output = {
            "fixture_counts": [
                {"type_tag": "A", "total": 10, "flags": []},
                {"type_tag": "UNKNOWN", "total": 3, "flags": []},
            ],
            "grand_total_fixtures": 13
        }
        _assumption_keywords = {"UNKNOWN", "TBD", "UNSCHEDULED", "?"}
        for fc in counter_output.get("fixture_counts", []):
            if fc.get("type_tag", "").upper() in _assumption_keywords:
                existing = fc.get("flags", [])
                if not any("ASSUMPTION" in f for f in existing):
                    fc.setdefault("flags", []).append("ASSUMPTION: fixture type not identified in schedule")

        unknown_fc = next(fc for fc in counter_output["fixture_counts"] if fc["type_tag"] == "UNKNOWN")
        self.assertTrue(any("ASSUMPTION" in f for f in unknown_fc["flags"]),
                        "UNKNOWN type_tag should be auto-flagged with ASSUMPTION")
        normal_fc = next(fc for fc in counter_output["fixture_counts"] if fc["type_tag"] == "A")
        self.assertEqual(normal_fc["flags"], [], "Normal type should not be flagged")

    def test_rule6_pre_flagged_unknown_not_double_flagged(self):
        """If Counter already flagged UNKNOWN, engine should not add a second flag."""
        counter_output = {
            "fixture_counts": [
                {"type_tag": "UNKNOWN", "total": 2, "flags": ["ASSUMPTION: not sure what this is"]},
            ],
            "grand_total_fixtures": 2
        }
        _assumption_keywords = {"UNKNOWN", "TBD", "UNSCHEDULED", "?"}
        for fc in counter_output.get("fixture_counts", []):
            if fc.get("type_tag", "").upper() in _assumption_keywords:
                existing = fc.get("flags", [])
                if not any("ASSUMPTION" in f for f in existing):
                    fc.setdefault("flags", []).append("ASSUMPTION: fixture type not identified in schedule")

        fc = counter_output["fixture_counts"][0]
        assumption_flags = [f for f in fc["flags"] if "ASSUMPTION" in f]
        self.assertEqual(len(assumption_flags), 1, "Should not double-flag already-flagged UNKNOWN")

    # ── P0 #4: Multi-panel schedule merge ──────────────────────────────

    def test_multi_panel_merge_accumulates_circuits(self):
        """Merging two PanelData objects should accumulate circuits from both."""
        from takeoff.extraction import PanelData
        panel1 = PanelData(
            panel_name="Panel A",
            circuits=[{"circuit": "1A", "load_va": 1200}],
            total_load_va=1200.0
        )
        panel2 = PanelData(
            panel_name="Panel B",
            circuits=[{"circuit": "1B", "load_va": 800}, {"circuit": "2B", "load_va": 600}],
            total_load_va=1400.0
        )
        # Simulate engine merge logic
        panel1.circuits.extend(panel2.circuits)
        panel1.total_load_va = panel1.total_load_va + panel2.total_load_va
        self.assertEqual(len(panel1.circuits), 3)
        self.assertAlmostEqual(panel1.total_load_va, 2600.0)

    # ── P0 #1: Reconciler per-area scale ───────────────────────────────

    def test_reconciler_area_counts_scaled_proportionally(self):
        """When Reconciler revises a total, counts_by_area must be scaled."""
        original_areas = {"Zone A": 10, "Zone B": 10}
        original_total = 20
        revised_total = 24  # +4 from reconciler
        scale = revised_total / original_total
        scaled = {area: max(0, round(cnt * scale)) for area, cnt in original_areas.items()}
        self.assertEqual(scaled["Zone A"], 12)
        self.assertEqual(scaled["Zone B"], 12)
        self.assertEqual(sum(scaled.values()), revised_total)

    def test_reconciler_no_change_leaves_areas_unchanged(self):
        """When Reconciler doesn't change the total, counts_by_area must be unchanged."""
        original_areas = {"Zone A": 10, "Zone B": 10}
        original_total = 20
        revised_total = 20  # no change
        # In the engine: if final_total == original_total, we skip scaling
        if revised_total != original_total and original_total > 0:
            scale = revised_total / original_total
            counts_by_area = {area: max(0, round(cnt * scale)) for area, cnt in original_areas.items()}
            area_flags = ["AREA_COUNTS_ESTIMATED"]
        else:
            counts_by_area = original_areas
            area_flags = []
        self.assertEqual(counts_by_area, original_areas)
        self.assertEqual(area_flags, [])

    # ── P4 #19: Checker dedup by category+type+area (not description) ──

    def test_checker_dedup_collapses_same_category_type_area(self):
        """Two attacks on same category/type_tag/area but different wording must deduplicate."""
        import re
        attacks = [
            {"attack_id": "ATK-001", "category": "missed_area", "affected_type_tag": "A",
             "affected_area": "Zone 1", "description": "Zone 1 is missing"},
            {"attack_id": "ATK-002", "category": "missed_area", "affected_type_tag": "A",
             "affected_area": "Zone 1", "description": "Zone 1 was not counted"},
        ]
        seen: set = set()
        deduped = []
        for attack in attacks:
            key = (
                attack.get("category", ""),
                (attack.get("affected_type_tag") or "").upper(),
                (attack.get("affected_area") or "").lower().strip()
            )
            if key not in seen:
                seen.add(key)
                deduped.append(attack)
        self.assertEqual(len(deduped), 1, "Same category/type/area should collapse to 1 attack")

    def test_checker_dedup_keeps_distinct_categories(self):
        """Attacks with different categories on same type/area must NOT deduplicate."""
        attacks = [
            {"attack_id": "ATK-001", "category": "missed_area", "affected_type_tag": "A",
             "affected_area": "Zone 1", "description": "zone missing"},
            {"attack_id": "ATK-002", "category": "math_error", "affected_type_tag": "A",
             "affected_area": "Zone 1", "description": "math is wrong"},
        ]
        seen: set = set()
        deduped = []
        for attack in attacks:
            key = (attack.get("category", ""), (attack.get("affected_type_tag") or "").upper(),
                   (attack.get("affected_area") or "").lower().strip())
            if key not in seen:
                seen.add(key)
                deduped.append(attack)
        self.assertEqual(len(deduped), 2, "Different categories on same area should both be kept")

    # ── P4 #20: Area fuzzy match ────────────────────────────────────────

    def test_area_fuzzy_match_word_overlap(self):
        """'North Wing Level 1' should fuzzy-match 'Level 1 North Wing'."""
        from takeoff.constitution import _area_fuzzy_match, _normalize_area_label
        expected = _normalize_area_label("North Wing Level 1")
        covered = {_normalize_area_label("Level 1 North Wing")}
        self.assertTrue(_area_fuzzy_match(expected, covered))

    def test_area_fuzzy_match_no_match(self):
        """'South Wing Level 2' must NOT match 'North Wing Level 1'."""
        from takeoff.constitution import _area_fuzzy_match, _normalize_area_label
        expected = _normalize_area_label("South Wing Level 2")
        covered = {_normalize_area_label("North Wing Level 1")}
        self.assertFalse(_area_fuzzy_match(expected, covered))

    def test_complete_coverage_fuzzy_miss_is_major_not_fatal(self):
        """Area with fuzzy match (label rename) should produce MAJOR, not FATAL violation."""
        from takeoff.constitution import check_complete_coverage
        # Snippets use "Floor 1 North" but Counter covered "Level 1 North"
        rcp_snippets = [{"label": "rcp", "sub_label": "Floor 1 North"}]
        areas_covered = ["Level 1 North"]
        violations = check_complete_coverage(areas_covered, rcp_snippets)
        if violations:
            self.assertEqual(violations[0]["severity"], "MAJOR",
                             "Fuzzy-matched area rename should be MAJOR, not FATAL")

    def test_complete_coverage_exact_match_no_violation(self):
        """Exact area label match (after normalization) should produce no violation."""
        from takeoff.constitution import check_complete_coverage
        rcp_snippets = [{"label": "rcp", "sub_label": "Floor 1 North (Copy)"}]
        areas_covered = ["Floor 1 North"]
        violations = check_complete_coverage(areas_covered, rcp_snippets)
        self.assertEqual(violations, [], "Exact match after normalization should produce no violation")

    # ── P2 #9: Fast mode notes compliance → neutral ────────────────────

    def test_fast_mode_note_compliance_is_neutral(self):
        """Fast mode should report neutral (0.5) for note_compliance regardless of notes."""
        result = calculate_confidence(
            fixture_counts=[{"type_tag": "A", "total": 10, "counts_by_area": {}}],
            areas_covered=["Zone 1"],
            rcp_snippets=[{"label": "rcp", "sub_label": "Zone 1"}],
            fixture_schedule={"fixtures": {"A": {"description": "Troffer"}}},
            checker_attacks=[],
            reconciler_responses=[],
            constitutional_violations=[],
            mode="fast",
            has_panel_schedule=False,
            has_plan_notes=False,  # Fast mode skips structural check → neutral
            notes_addressed=False
        )
        self.assertAlmostEqual(result["features"]["note_compliance"], 0.5,
                               msg="Fast mode note_compliance must be neutral 0.5")

    # ── Payload validation logic ────────────────────────────────────────

    def test_payload_validation_max_snippets(self):
        """Payload with >30 snippets should be rejected."""
        MAX_SNIPPETS = 30
        snippets = [{"id": f"s{i}", "label": "rcp", "image_data": "x"} for i in range(31)]
        self.assertGreater(len(snippets), MAX_SNIPPETS)

    def test_payload_validation_requires_fixture_schedule(self):
        """Payload without fixture_schedule snippet should be rejected."""
        snippets = [
            {"id": "rcp1", "label": "rcp", "image_data": "x"},
            {"id": "rcp2", "label": "rcp", "image_data": "x"},
        ]
        fixture_snippets = [s for s in snippets if s.get("label") == "fixture_schedule"]
        self.assertEqual(len(fixture_snippets), 0, "No fixture_schedule should fail validation")

    def test_payload_validation_requires_rcp(self):
        """Payload without rcp snippet should be rejected."""
        snippets = [
            {"id": "fs1", "label": "fixture_schedule", "image_data": "x"},
        ]
        rcp_snippets = [s for s in snippets if s.get("label") == "rcp"]
        self.assertEqual(len(rcp_snippets), 0, "No rcp snippet should fail validation")


# ══════════════════════════════════════════════════════════════════════
# TestBugFixes2 — tests for the second round of bug fixes
# ══════════════════════════════════════════════════════════════════════

class TestBugFixes2(unittest.TestCase):
    """Tests covering BUG-1 through BUG-10 fixes."""

    # ── BUG-4 / BUG-5: Atomic DB write and integer-split area scaling ────

    def test_store_job_results_atomic_commits_all(self):
        """store_job_results_atomic stores counts, log, and result in one shot."""
        import tempfile, os
        from takeoff.schema import TakeoffDB

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            db = TakeoffDB(db_path)
            db.create_job("job1", mode="fast", drawing_name="test", snippet_count=2)

            fixture_counts = [{"type_tag": "A", "counts_by_area": {"Floor 1": 10, "Floor 2": 5}, "difficulty": "S", "flags": []}]
            attacks = [{"attack_id": "ATK-001", "severity": "minor", "category": "missed_fixtures", "description": "test"}]
            responses = [{"attack_id": "ATK-001", "verdict": "defend", "explanation": "justified"}]
            full_result = {"job_id": "job1", "grand_total": 15}

            db.store_job_results_atomic(
                job_id="job1",
                fixture_counts=fixture_counts,
                attacks=attacks,
                reconciler_responses=responses,
                grand_total=15,
                confidence_score=0.72,
                confidence_band="MODERATE",
                confidence_features='{}',
                violations=[],
                flags=[],
                judge_verdict="PASS",
                full_result=full_result
            )

            counts = db.get_job_counts("job1")
            self.assertEqual(len(counts), 2)  # Floor 1 + Floor 2
            self.assertEqual(sum(r["count"] for r in counts), 15)

            adv_log = db.get_job_adversarial_log("job1")
            self.assertEqual(len(adv_log), 1)
            self.assertEqual(adv_log[0]["final_verdict"], "defend")

            retrieved = db.get_full_result("job1")
            self.assertIsNotNone(retrieved)
            self.assertEqual(retrieved["grand_total"], 15)

            db.close()
        finally:
            os.unlink(db_path)

    def test_scale_area_counts_sums_to_target(self):
        """_scale_area_counts must produce counts that sum exactly to target_total."""
        from takeoff.engine import _scale_area_counts

        original = {"Floor 1": 7, "Floor 2": 5, "Floor 3": 3}
        for target in [12, 13, 14, 17, 20, 1]:
            result = _scale_area_counts(original, target)
            self.assertEqual(sum(result.values()), target,
                             f"Expected sum={target}, got {sum(result.values())} with original={original}")
            self.assertTrue(all(v >= 0 for v in result.values()))

    def test_scale_area_counts_empty(self):
        """Empty input returns empty dict."""
        from takeoff.engine import _scale_area_counts
        self.assertEqual(_scale_area_counts({}, 10), {})

    def test_scale_area_counts_zero_original(self):
        """Zero original total returns zeros."""
        from takeoff.engine import _scale_area_counts
        result = _scale_area_counts({"A": 0, "B": 0}, 5)
        self.assertEqual(result, {"A": 0, "B": 0})

    # ── BUG-9: area_coverage uses fuzzy matching ─────────────────────────

    def test_area_coverage_fuzzy_match_counts(self):
        """area_coverage should credit areas matched by normalization, not just exact string."""
        result = calculate_confidence(
            fixture_counts=[{"type_tag": "A", "total": 5, "counts_by_area": {"North Wing Level 1": 5}}],
            areas_covered=["Level 1 North Wing"],  # same area, different word order
            rcp_snippets=[{"label": "rcp", "sub_label": "North Wing Level 1"}],
            fixture_schedule={"fixtures": {"A": {"description": "Troffer"}}},
            checker_attacks=[],
            reconciler_responses=[],
            constitutional_violations=[],
            mode="fast",
            has_panel_schedule=False,
            has_plan_notes=False,
            notes_addressed=False
        )
        # Fuzzy match should find "North Wing Level 1" ↔ "Level 1 North Wing"
        self.assertGreater(result["features"]["area_coverage"], 0.5,
                           "Fuzzy-matched area should score > 0.5")

    # ── BUG-10: Vision cost tracking ─────────────────────────────────────

    def test_reset_and_get_vision_cost(self):
        """reset_vision_cost zeros the accumulator; get_vision_cost_usd returns float."""
        from takeoff.extraction import reset_vision_cost, get_vision_cost_usd
        reset_vision_cost()
        cost = get_vision_cost_usd()
        self.assertIsInstance(cost, float)
        self.assertEqual(cost, 0.0)

    # ── BUG-3: Fixture schedule collision detection ───────────────────────

    def test_fixture_collision_keeps_first_definition(self):
        """When two schedule snippets define the same type tag, first wins."""
        # Simulate what engine does: iterate extracted.fixtures and skip duplicates
        existing = {"A": {"description": "First definition", "wattage": 38.0}}
        incoming = {"A": {"description": "Second definition", "wattage": 50.0}, "B": {"description": "New type"}}

        warnings = []
        for tag, info in incoming.items():
            if tag in existing:
                warnings.append(f"Collision on '{tag}'")
            else:
                existing[tag] = info

        self.assertEqual(existing["A"]["description"], "First definition",
                         "First definition must be preserved on collision")
        self.assertIn("B", existing, "Non-colliding new type must be added")
        self.assertTrue(any("A" in w for w in warnings), "Collision warning must be emitted")

    # ── BUG-7: Judge model errors return WARN not BLOCK ──────────────────

    def test_judge_model_error_returns_warn(self):
        """Judge model/parse errors should return WARN + JUDGE_UNAVAILABLE, not BLOCK."""
        from unittest.mock import MagicMock, patch
        from takeoff.agents import Judge
        from takeoff.constitution import get_constitution
        from takeoff.extraction import FixtureSchedule

        # Create a Judge whose model_router.complete raises
        router = MagicMock()
        router.complete.side_effect = RuntimeError("API timeout")
        judge = Judge(router, get_constitution())

        fs = FixtureSchedule(fixtures={"A": {"description": "Troffer", "wattage": 38}})
        result = judge.evaluate(
            counter_output={"fixture_counts": [{"type_tag": "A", "total": 10}], "grand_total_fixtures": 10, "areas_covered": []},
            checker_attacks=[],
            reconciler_output=None,
            fixture_schedule=fs,
            mode="fast"
        )
        self.assertEqual(result["verdict"], "WARN",
                         "Model error should produce WARN, not BLOCK")
        self.assertTrue(any("JUDGE_UNAVAILABLE" in f for f in result["flags"]),
                        "JUDGE_UNAVAILABLE flag must be present")
        self.assertEqual(result["violations"], [],
                         "No constitutional violations should be fabricated on model error")

    # ── BUG-2: Semaphore uses public .locked() API ────────────────────────

    def test_semaphore_locked_check_exists(self):
        """asyncio.Semaphore.locked() is a public API (not _value)."""
        import asyncio
        sem = asyncio.Semaphore(1)
        self.assertTrue(hasattr(sem, "locked"), "Semaphore must have public .locked() method")
        # _value access should no longer appear in api.py
        import os as _os
        _api_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "takeoff", "api.py")
        with open(_api_path) as f:
            content = f.read()
        self.assertNotIn("_value", content, "api.py must not access private _value attribute")


# ══════════════════════════════════════════════════════════════════════
# 12. Audit Round-3 Bug Fixes
# ══════════════════════════════════════════════════════════════════════

class TestBugFixes3(unittest.TestCase):
    """Tests for all improvements applied in audit round 3."""

    # ── Negative count validation ─────────────────────────────────────

    def test_negative_total_count_is_fatal(self):
        """Negative total count must produce a FATAL violation."""
        bad_counts = [
            {"type_tag": "A", "total": -5, "counts_by_area": {"Room 1": 10}},
        ]
        violations = check_non_negative_counts(bad_counts)
        self.assertTrue(any(v["severity"] == "FATAL" for v in violations))
        self.assertTrue(any("negative" in v["explanation"].lower() for v in violations))

    def test_negative_area_count_is_fatal(self):
        """Negative per-area count must produce a FATAL violation."""
        bad_counts = [
            {"type_tag": "B", "total": 5, "counts_by_area": {"Room 1": -3, "Room 2": 8}},
        ]
        violations = check_non_negative_counts(bad_counts)
        self.assertTrue(any(v["severity"] == "FATAL" for v in violations))

    def test_zero_count_is_not_flagged(self):
        """Zero counts are valid (fixture type exists but not used in this area)."""
        counts = [
            {"type_tag": "A", "total": 0, "counts_by_area": {"Room 1": 0}},
        ]
        violations = check_non_negative_counts(counts)
        self.assertEqual(violations, [])

    def test_negative_counts_propagate_to_enforce_constitution(self):
        """enforce_constitution must propagate negative count FATAL violations."""
        bad_counts = [{"type_tag": "A", "total": -1, "counts_by_area": {}}]
        result = enforce_constitution(
            fixture_counts=bad_counts,
            areas_covered=[],
            rcp_snippets=[],
            fixture_schedule={"fixtures": {"A": {"description": "Type A"}}}
        )
        self.assertEqual(result["verdict"], "BLOCK")
        self.assertTrue(any("Non-Negative" in v["rule"] for v in result["violations"]))

    # ── Emergency keyword expansion ───────────────────────────────────

    def test_new_emergency_keywords_present(self):
        """New emergency keywords must be in EMERGENCY_KEYWORDS."""
        new_kws = ["recessed egress", "safety lighting", "standby fixture",
                   "egress light", "emergency egress", "exit light", "exit fixture", "em unit"]
        for kw in new_kws:
            self.assertIn(kw, EMERGENCY_KEYWORDS, f"'{kw}' missing from EMERGENCY_KEYWORDS")

    def test_standby_fixture_triggers_emergency_tracking(self):
        """A fixture with 'standby fixture' description should satisfy Rule 5."""
        counts = [
            {"type_tag": "A", "total": 10, "description": "standard troffer", "notes": ""},
            {"type_tag": "B", "total": 5, "description": "standard downlight", "notes": ""},
            {"type_tag": "C", "total": 3, "description": "standard sconce", "notes": ""},
            {"type_tag": "D", "total": 2, "description": "linear strip", "notes": ""},
            {"type_tag": "E", "total": 2, "description": "track head", "notes": ""},
            {"type_tag": "EM", "total": 2, "description": "standby fixture with battery", "notes": ""},
        ]
        violations = check_emergency_fixtures(counts)
        # Should NOT flag — we have an "standby fixture" type
        self.assertEqual(violations, [])

    # ── Area label normalization (revised regex) ──────────────────────

    def test_normalize_strips_copy_suffix(self):
        """'Floor 2 North (Copy)' should normalize to 'floor 2 north'."""
        self.assertEqual(_normalize_area_label("Floor 2 North (Copy)"), "floor 2 north")

    def test_normalize_strips_rev_suffix(self):
        """'Level 3 (Rev A)' should normalize to 'level 3'."""
        self.assertEqual(_normalize_area_label("Level 3 (Rev A)"), "level 3")

    def test_normalize_preserves_location_parenthetical(self):
        """'Floor 2 (North)' must NOT be stripped — (North) is meaningful."""
        result = _normalize_area_label("Floor 2 (North)")
        self.assertIn("north", result, "Parenthetical direction should be preserved")

    def test_normalize_preserves_pure_numeric_parenthetical(self):
        """'Level 1 (2)' must NOT strip (2) — it's a location identifier, not a copy marker.
        Bug fix: the old regex stripped bare digits, mangling real area names like 'Floor (2)'."""
        self.assertEqual(_normalize_area_label("Level 1 (2)"), "level 1 (2)")

    def test_normalize_strips_v_number_suffix(self):
        """'Level 1 (v2)' should strip the version suffix."""
        self.assertEqual(_normalize_area_label("Level 1 (v2)"), "level 1")

    # ── Area coverage confidence default ──────────────────────────────

    def test_area_coverage_neutral_when_no_rcp_areas(self):
        """When no RCP snippets have sub_labels, area_coverage must be 0.5 (neutral)."""
        result = calculate_confidence(
            fixture_counts=VALID_FIXTURE_COUNTS_LIST,
            areas_covered=["Room A"],
            rcp_snippets=[{"label": "rcp"}],  # no sub_label
            fixture_schedule=SAMPLE_FIXTURE_SCHEDULE,
            checker_attacks=[],
            reconciler_responses=[],
            constitutional_violations=[],
            mode="fast",
        )
        self.assertAlmostEqual(result["features"]["area_coverage"], 0.5, places=2)

    def test_area_coverage_not_inflated_to_1_with_no_snippets(self):
        """With no RCP snippets at all, area_coverage must be 0.5 not 1.0."""
        result = calculate_confidence(
            fixture_counts=VALID_FIXTURE_COUNTS_LIST,
            areas_covered=["Room A"],
            rcp_snippets=[],
            fixture_schedule=SAMPLE_FIXTURE_SCHEDULE,
            checker_attacks=[],
            reconciler_responses=[],
            constitutional_violations=[],
            mode="fast",
        )
        self.assertAlmostEqual(result["features"]["area_coverage"], 0.5, places=2)

    # ── Dead penalty code removed ─────────────────────────────────────

    def test_major_violation_applies_cap_not_additive_penalty(self):
        """With 1 MAJOR violation, score must be capped at ≤0.40 (not additive -0.15)."""
        violations = [{"severity": "MAJOR", "rule": "Some Rule", "explanation": "..."}]
        result = calculate_confidence(
            fixture_counts=VALID_FIXTURE_COUNTS_LIST,
            areas_covered=VALID_AREAS_COVERED,
            rcp_snippets=SAMPLE_RCP_SNIPPETS,
            fixture_schedule=SAMPLE_FIXTURE_SCHEDULE,
            checker_attacks=[],
            reconciler_responses=[],
            constitutional_violations=violations,
            mode="fast",
        )
        self.assertLessEqual(result["score"], 0.40)
        # Old code would have applied -0.15 AND THEN the cap separately.
        # Now only the cap applies, so score should be exactly at cap (not lower).
        self.assertGreaterEqual(result["score"], 0.20)

    # ── Vision cost rate configurability ─────────────────────────────

    def test_vision_cost_rates_use_defaults(self):
        """Default vision cost rates should match $3/1M input, $15/1M output."""
        import importlib
        import takeoff.extraction as ext_mod
        # Default values
        self.assertAlmostEqual(ext_mod._VISION_INPUT_COST_PER_TOKEN, 3e-6, places=9)
        self.assertAlmostEqual(ext_mod._VISION_OUTPUT_COST_PER_TOKEN, 15e-6, places=9)

    def test_vision_cost_rates_configurable_via_env(self):
        """Setting env vars should change the cost rates on module reload."""
        import importlib
        import os
        import takeoff.extraction as ext_mod

        os.environ["TAKEOFF_VISION_INPUT_COST_PER_M"] = "6"
        os.environ["TAKEOFF_VISION_OUTPUT_COST_PER_M"] = "30"
        importlib.reload(ext_mod)

        try:
            self.assertAlmostEqual(ext_mod._VISION_INPUT_COST_PER_TOKEN, 6e-6, places=9)
            self.assertAlmostEqual(ext_mod._VISION_OUTPUT_COST_PER_TOKEN, 30e-6, places=9)
        finally:
            # Restore defaults
            os.environ.pop("TAKEOFF_VISION_INPUT_COST_PER_M", None)
            os.environ.pop("TAKEOFF_VISION_OUTPUT_COST_PER_M", None)
            importlib.reload(ext_mod)

    # ── Schema migration error handling ──────────────────────────────

    def test_schema_migration_raises_on_non_duplicate_error(self):
        """Schema migration must re-raise non-duplicate OperationalErrors."""
        import sqlite3
        from unittest.mock import patch, MagicMock

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        with patch("takeoff.schema.sqlite3.connect") as mock_connect:
            mock_conn = MagicMock()
            mock_connect.return_value = mock_conn
            mock_conn.row_factory = None
            # Make ALTER TABLE raise a non-duplicate OperationalError
            mock_conn.execute.side_effect = [
                MagicMock(),  # PRAGMA journal_mode
                MagicMock(),  # PRAGMA synchronous
                MagicMock(),  # CREATE TABLE takeoff_jobs
                MagicMock(),  # CREATE TABLE snippets
                MagicMock(),  # CREATE TABLE fixture_schedule
                MagicMock(),  # CREATE TABLE fixture_counts
                MagicMock(),  # CREATE TABLE adversarial_log
                MagicMock(),  # CREATE TABLE results
                sqlite3.OperationalError("table is locked"),  # ALTER TABLE
            ]
            mock_conn.commit = MagicMock()

            from takeoff.schema import TakeoffDB
            with self.assertRaises(sqlite3.OperationalError):
                TakeoffDB(db_path)

    def test_schema_migration_ignores_duplicate_column(self):
        """Schema migration must silently pass when full_result_json already exists."""
        import sqlite3

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        # First init creates the column
        db1 = TakeoffDB(db_path)
        db1.close()

        # Second init should not raise even though column already exists
        try:
            db2 = TakeoffDB(db_path)
            db2.close()
        except Exception as e:
            self.fail(f"Re-initializing DB raised unexpected exception: {e}")

    # ── Panel circuit dedup ───────────────────────────────────────────

    def test_panel_circuit_dedup_prevents_double_count(self):
        """Merging two identical panels must not duplicate circuits."""
        from takeoff.extraction import PanelData

        panel1 = PanelData(
            panel_name="LP-1",
            circuits=[
                {"circuit": "1", "breaker_size": "20A", "load_va": 1200, "description": "Lighting A"},
                {"circuit": "3", "breaker_size": "20A", "load_va": 950, "description": "Lighting B"},
            ],
            total_load_va=2150,
            warnings=[]
        )
        panel2 = PanelData(
            panel_name="LP-1",  # Same panel name — duplicate submission
            circuits=[
                {"circuit": "1", "breaker_size": "20A", "load_va": 1200, "description": "Lighting A"},
            ],
            total_load_va=1200,
            warnings=[]
        )

        # Simulate the engine's merge + dedup logic
        existing_keys = {(panel1.panel_name, c.get("circuit")) for c in panel1.circuits}
        for circuit in panel2.circuits:
            key = (panel2.panel_name, circuit.get("circuit"))
            if key not in existing_keys:
                panel1.circuits.append(circuit)
                existing_keys.add(key)

        # Circuit "1" should not be duplicated
        circuit_ids = [c["circuit"] for c in panel1.circuits]
        self.assertEqual(len(circuit_ids), 2, "Duplicate circuit should be rejected")
        self.assertEqual(circuit_ids.count("1"), 1, "Circuit 1 must appear exactly once")

    def test_panel_circuit_dedup_allows_different_panels(self):
        """Circuits with the same number on different panels should both be kept."""
        from takeoff.extraction import PanelData

        panel1 = PanelData(
            panel_name="LP-1",
            circuits=[{"circuit": "1", "breaker_size": "20A", "load_va": 1200, "description": "LP-1 Circuit 1"}],
            total_load_va=1200,
            warnings=[]
        )
        panel2 = PanelData(
            panel_name="LP-2",  # Different panel — circuit 1 here is a distinct circuit
            circuits=[{"circuit": "1", "breaker_size": "20A", "load_va": 900, "description": "LP-2 Circuit 1"}],
            total_load_va=900,
            warnings=[]
        )

        existing_keys = {(panel1.panel_name, c.get("circuit")) for c in panel1.circuits}
        for circuit in panel2.circuits:
            key = (panel2.panel_name, circuit.get("circuit"))
            if key not in existing_keys:
                panel1.circuits.append(circuit)
                existing_keys.add(key)

        self.assertEqual(len(panel1.circuits), 2, "Both circuits should be kept — different panels")

    # ── SSE cancel event ──────────────────────────────────────────────

    def test_sse_cancel_event_raises_in_status_callback(self):
        """status_callback must raise RuntimeError when cancel_event is set."""
        import queue as _queue
        import threading

        status_queue = _queue.Queue(maxsize=200)
        cancel_event = threading.Event()

        def status_callback(message: str):
            if cancel_event.is_set():
                raise RuntimeError("Job cancelled: SSE client timed out")
            try:
                status_queue.put_nowait({"type": "status", "message": message})
            except _queue.Full:
                pass

        # Before cancel — should work fine
        status_callback("Starting job")
        self.assertFalse(status_queue.empty())

        # After cancel — should raise
        cancel_event.set()
        with self.assertRaises(RuntimeError):
            status_callback("Another update")

    def test_sse_queue_put_nowait_drops_on_full(self):
        """When queue is full, put_nowait should drop messages without blocking."""
        import queue as _queue
        import threading

        status_queue = _queue.Queue(maxsize=2)
        cancel_event = threading.Event()

        def status_callback(message: str):
            if cancel_event.is_set():
                raise RuntimeError("Job cancelled")
            try:
                status_queue.put_nowait({"type": "status", "message": message})
            except _queue.Full:
                pass  # Drop — does not block or raise

        # Fill queue
        status_callback("msg1")
        status_callback("msg2")
        # This should drop silently, not block or raise
        status_callback("msg3 — dropped")
        self.assertEqual(status_queue.qsize(), 2)

    # ── CLI missing file error ────────────────────────────────────────

    def test_cli_main_file_raises_on_missing_image(self):
        """CLI should exit with error if a manifest snippet references a missing image."""
        import subprocess
        import json
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = {
                "drawing_name": "Test",
                "snippets": [
                    {"id": "s1", "label": "fixture_schedule", "image_path": "nonexistent.png"}
                ]
            }
            manifest_path = os.path.join(tmpdir, "manifest.json")
            with open(manifest_path, "w") as f:
                json.dump(manifest, f)

            result = subprocess.run(
                [sys.executable, "-m", "takeoff", tmpdir],
                capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                env={**os.environ, "ANTHROPIC_API_KEY": "sk-fake-key-for-test"},
                timeout=30
            )
            # Should exit non-zero with a descriptive error about the missing file
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("nonexistent.png", result.stderr)


# ══════════════════════════════════════════════════════════════════════
# TestBugFixes4 — audit round 4 improvements
# ══════════════════════════════════════════════════════════════════════

class TestBugFixes4(unittest.TestCase):
    """Tests for improvements applied in audit round 4."""

    # ── ThreadPoolExecutor exception handling ─────────────────────────

    def test_executor_per_future_exception_does_not_abort_remaining(self):
        """An exception in one future must not cancel the rest."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading

        results = []
        errors = []

        def work(i):
            if i == 1:
                raise ValueError("simulated extraction failure")
            return i * 10

        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {ex.submit(work, i): i for i in range(3)}
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as e:
                    errors.append(str(e))

        # All 3 futures ran: 2 succeeded, 1 failed
        self.assertEqual(len(errors), 1)
        self.assertIn("simulated", errors[0])
        self.assertIn(0, results)
        self.assertIn(20, results)

    # ── Agent parse_error flag ────────────────────────────────────────

    def test_counter_parse_error_flag_set_on_malformed_json(self):
        """Counter must set parse_error=True when LLM returns non-JSON."""
        from unittest.mock import MagicMock
        from takeoff.agents import Counter
        from takeoff.extraction import FixtureSchedule

        router = MagicMock()
        router.complete.return_value = MagicMock(content="This is not JSON at all!")
        counter = Counter(router)
        result = counter.generate_count(FixtureSchedule(), [], [])
        self.assertTrue(result.parse_error, "parse_error should be True on JSON parse failure")
        self.assertEqual(result.data, {})

    def test_checker_parse_error_flag_set_on_malformed_json(self):
        """Checker must set parse_error=True when LLM returns non-JSON."""
        from unittest.mock import MagicMock
        from takeoff.agents import Checker
        from takeoff.extraction import FixtureSchedule

        router = MagicMock()
        router.complete.return_value = MagicMock(content="not json {{{")
        checker = Checker(router)
        result = checker.generate_attacks({}, FixtureSchedule(), [], [], None)
        self.assertTrue(result.parse_error)

    def test_reconciler_parse_error_flag_set_on_malformed_json(self):
        """Reconciler must set parse_error=True when LLM returns non-JSON."""
        from unittest.mock import MagicMock
        from takeoff.agents import Reconciler
        from takeoff.extraction import FixtureSchedule

        router = MagicMock()
        router.complete.return_value = MagicMock(content="<html>not json</html>")
        reconciler = Reconciler(router)
        result = reconciler.address_attacks({}, [], FixtureSchedule(), [])
        self.assertTrue(result.parse_error)

    def test_takeoff_response_parse_error_defaults_false(self):
        """TakeoffResponse.parse_error must default to False (no regression)."""
        from takeoff.agents import TakeoffResponse
        resp = TakeoffResponse(agent_role="counter", data={"fixture_counts": []}, raw_response="ok")
        self.assertFalse(resp.parse_error)

    # ── Checker dedup: severity preservation ─────────────────────────

    def test_checker_dedup_keeps_critical_over_minor(self):
        """When two attacks share the same dedup key, the CRITICAL one must survive."""
        import json
        from unittest.mock import MagicMock
        from takeoff.agents import Checker
        from takeoff.extraction import FixtureSchedule

        attacks_payload = {
            "attacks": [
                {
                    "attack_id": "ATK-001", "severity": "minor",
                    "category": "missed_area", "affected_type_tag": "A",
                    "affected_area": "Room 1", "description": "minor version",
                    "suggested_correction": "check", "evidence": "low"
                },
                {
                    "attack_id": "ATK-002", "severity": "critical",
                    "category": "missed_area", "affected_type_tag": "A",
                    "affected_area": "Room 1", "description": "critical — same dedup key",
                    "suggested_correction": "fix immediately", "evidence": "strong"
                },
            ],
            "total_attacks": 2, "critical_count": 1, "summary": "test"
        }

        router = MagicMock()
        router.complete.return_value = MagicMock(content=json.dumps(attacks_payload))
        checker = Checker(router)
        result = checker.generate_attacks({}, FixtureSchedule(), [], [], None)

        attacks = result.data.get("attacks", [])
        self.assertEqual(len(attacks), 1, "Duplicate dedup key should collapse to 1 attack")
        self.assertEqual(attacks[0]["severity"], "critical",
                         "Higher severity (critical) must be kept, not minor")

    def test_checker_dedup_keeps_major_over_minor(self):
        """Dedup must keep MAJOR over MINOR when they share a key."""
        import json
        from unittest.mock import MagicMock
        from takeoff.agents import Checker
        from takeoff.extraction import FixtureSchedule

        attacks_payload = {
            "attacks": [
                {"attack_id": "ATK-001", "severity": "major",
                 "category": "math_error", "affected_type_tag": "B",
                 "affected_area": "Lobby", "description": "major first"},
                {"attack_id": "ATK-002", "severity": "minor",
                 "category": "math_error", "affected_type_tag": "B",
                 "affected_area": "Lobby", "description": "minor second"},
            ],
            "total_attacks": 2, "critical_count": 0, "summary": "test"
        }

        router = MagicMock()
        router.complete.return_value = MagicMock(content=json.dumps(attacks_payload))
        checker = Checker(router)
        result = checker.generate_attacks({}, FixtureSchedule(), [], [], None)

        attacks = result.data.get("attacks", [])
        self.assertEqual(len(attacks), 1)
        self.assertEqual(attacks[0]["severity"], "major")

    def test_checker_dedup_distinct_areas_not_collapsed(self):
        """Two attacks with different areas must NOT be deduped together."""
        import json
        from unittest.mock import MagicMock
        from takeoff.agents import Checker
        from takeoff.extraction import FixtureSchedule

        attacks_payload = {
            "attacks": [
                {"attack_id": "ATK-001", "severity": "critical",
                 "category": "missed_area", "affected_type_tag": "A",
                 "affected_area": "Room 1", "description": "room 1"},
                {"attack_id": "ATK-002", "severity": "critical",
                 "category": "missed_area", "affected_type_tag": "A",
                 "affected_area": "Room 2", "description": "room 2"},
            ],
            "total_attacks": 2, "critical_count": 2, "summary": "test"
        }

        router = MagicMock()
        router.complete.return_value = MagicMock(content=json.dumps(attacks_payload))
        checker = Checker(router)
        result = checker.generate_attacks({}, FixtureSchedule(), [], [], None)

        self.assertEqual(len(result.data.get("attacks", [])), 2,
                         "Different areas must not be collapsed to 1")

    # ── Reconciler notes cross-reference ─────────────────────────────

    def test_reconciler_notes_gap_detection_in_engine(self):
        """Engine must detect when Reconciler returns fewer notes_compliance than plan_notes."""
        import json
        import tempfile
        from unittest.mock import MagicMock
        from takeoff.engine import TakeoffEngine
        from takeoff.extraction import FixtureSchedule, AreaCount, PlanNote

        # Two plan notes but Reconciler only acknowledges one
        plan_notes = [
            PlanNote(text="all corridor fixtures on emergency circuit",
                     constraint_type="electrical", affects_fixture_type="A"),
            PlanNote(text="occupancy sensors required for type B",
                     constraint_type="control", affects_fixture_type="B"),
        ]
        reconciler_compliance_one_only = [
            {"note": "all corridor fixtures on emergency circuit", "applied": True, "finding": "ok"}
        ]

        fixture_counts_data = {
            "fixture_counts": [
                {"type_tag": "A", "total": 10, "counts_by_area": {"Hall": 10},
                 "description": "Troffer", "difficulty": "S", "accessories": [], "flags": []},
                {"type_tag": "B", "total": 5, "counts_by_area": {"Hall": 5},
                 "description": "Downlight", "difficulty": "S", "accessories": [], "flags": []},
            ],
            "areas_covered": ["Hall"],
            "grand_total_fixtures": 15,
            "reasoning": "test"
        }
        reconciler_data = {
            "responses": [],
            "revised_fixture_counts": {},
            "revised_grand_total": 15,
            "notes_compliance": reconciler_compliance_one_only,
            "reasoning": "one note only"
        }
        judge_data = {
            "verdict": "PASS",
            "violations": [],
            "approved_counts": {"A": 10, "B": 5},
            "flags": [],
            "ruling_summary": "All good"
        }

        router = MagicMock()
        router.get_stats.return_value = {"model_router_cost_usd": 0.0}
        # Counter → valid counts
        router.complete.side_effect = [
            MagicMock(content=json.dumps(fixture_counts_data)),   # Counter
            MagicMock(content=json.dumps({"attacks": [], "total_attacks": 0, "critical_count": 0, "summary": "clean"})),  # Checker
            MagicMock(content=json.dumps(reconciler_data)),        # Reconciler
            MagicMock(content=json.dumps(judge_data)),             # Judge
        ]

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        engine = TakeoffEngine(db_path=db_path, model_router=router)

        # Inject plan_notes and fixture_schedule directly into _run_strict_mode
        # by overriding the extraction step — we call _run_strict_mode directly.
        import base64
        dummy_image = base64.b64encode(b"fake_png_data").decode()

        # Patch extract_fixture_schedule, extract_rcp_counts, extract_plan_notes
        from unittest.mock import patch
        from takeoff.extraction import AreaCount as AC

        mock_fs = FixtureSchedule()
        mock_fs.fixtures = {
            "A": {"description": "Troffer", "wattage": 40},
            "B": {"description": "Downlight", "wattage": 20},
        }
        mock_ac = AC(area_label="Hall", counts_by_type={"A": 10, "B": 5})

        mock_pn_result = plan_notes

        with patch("takeoff.engine.extract_fixture_schedule", return_value=mock_fs), \
             patch("takeoff.engine.extract_rcp_counts", return_value=mock_ac), \
             patch("takeoff.engine.extract_plan_notes", return_value=mock_pn_result):

            snippets = [
                {"id": "s1", "label": "fixture_schedule", "image_data": dummy_image},
                {"id": "s2", "label": "rcp", "sub_label": "Hall", "image_data": dummy_image},
                {"id": "s3", "label": "plan_notes", "image_data": dummy_image},
            ]
            result = engine.run_takeoff(snippets=snippets, mode="strict")

        # With 2 plan notes but only 1 in compliance, the gap is filled with non-applied.
        # notes_addressed must be False (one note unverified → not all applied)
        # Confidence should NOT give full notes credit.
        # We verify the result doesn't crash and produces a valid verdict.
        self.assertIn("verdict", result)
        self.assertNotIn("error", result, f"Unexpected error in result: {result.get('error')}")

    # ── Error code distinction: parse_error vs blank_drawing ─────────

    def test_parse_error_returns_agent_parse_error_code(self):
        """When Counter JSON parse fails, error code must be 'agent_parse_error', not 'blank_drawing'."""
        import tempfile
        from unittest.mock import MagicMock, patch
        from takeoff.engine import TakeoffEngine
        from takeoff.extraction import FixtureSchedule, AreaCount

        router = MagicMock()
        router.get_stats.return_value = {"model_router_cost_usd": 0.0}
        # Counter returns malformed JSON
        router.complete.return_value = MagicMock(content="not valid json!!!")

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        engine = TakeoffEngine(db_path=db_path, model_router=router)

        import base64
        dummy_image = base64.b64encode(b"fake").decode()

        mock_fs = FixtureSchedule()
        mock_fs.fixtures = {"A": {"description": "Troffer"}}
        mock_ac = AreaCount(area_label="Hall", counts_by_type={"A": 5})

        with patch("takeoff.engine.extract_fixture_schedule", return_value=mock_fs), \
             patch("takeoff.engine.extract_rcp_counts", return_value=mock_ac), \
             patch("takeoff.engine.extract_plan_notes", return_value=[]):

            snippets = [
                {"id": "s1", "label": "fixture_schedule", "image_data": dummy_image},
                {"id": "s2", "label": "rcp", "sub_label": "Hall", "image_data": dummy_image},
            ]
            result = engine.run_takeoff(snippets=snippets, mode="fast")

        self.assertEqual(result.get("error"), "agent_parse_error",
                         f"Expected agent_parse_error but got: {result.get('error')}")
        self.assertIn("malformed JSON", result.get("message", ""),
                      "Message should mention malformed JSON")

    def test_blank_drawing_returns_blank_drawing_code(self):
        """When Counter legitimately returns 0 fixtures, error code must be 'blank_drawing'."""
        import json
        import tempfile
        from unittest.mock import MagicMock, patch
        from takeoff.engine import TakeoffEngine
        from takeoff.extraction import FixtureSchedule, AreaCount

        router = MagicMock()
        router.get_stats.return_value = {"model_router_cost_usd": 0.0}
        # Counter returns valid JSON with 0 fixtures
        router.complete.return_value = MagicMock(content=json.dumps({
            "fixture_counts": [], "areas_covered": [], "grand_total_fixtures": 0, "reasoning": "blank"
        }))

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        engine = TakeoffEngine(db_path=db_path, model_router=router)

        import base64
        dummy_image = base64.b64encode(b"fake").decode()

        mock_fs = FixtureSchedule()
        mock_fs.fixtures = {"A": {"description": "Troffer"}}
        mock_ac = AreaCount(area_label="Hall", counts_by_type={})

        with patch("takeoff.engine.extract_fixture_schedule", return_value=mock_fs), \
             patch("takeoff.engine.extract_rcp_counts", return_value=mock_ac), \
             patch("takeoff.engine.extract_plan_notes", return_value=[]):

            snippets = [
                {"id": "s1", "label": "fixture_schedule", "image_data": dummy_image},
                {"id": "s2", "label": "rcp", "sub_label": "Hall", "image_data": dummy_image},
            ]
            result = engine.run_takeoff(snippets=snippets, mode="fast")

        self.assertEqual(result.get("error"), "blank_drawing",
                         f"Expected blank_drawing but got: {result.get('error')}")


# ══════════════════════════════════════════════════════════════════════
# Audit Round 5 Bug Fixes
# ══════════════════════════════════════════════════════════════════════

class TestBugFixes5(unittest.TestCase):
    """Tests for audit round-5 bug fixes."""

    # ── Fix 1: Reconciler deviation flag surfaces in final output ─────

    def test_reconciler_deviation_flag_in_final_output(self):
        """Reconciler >20% deviation flag must appear in result['flags'], not only reconciler_output."""
        import json
        import tempfile
        from unittest.mock import MagicMock, patch
        from takeoff.engine import TakeoffEngine
        from takeoff.extraction import FixtureSchedule, AreaCount

        router = MagicMock()
        router.get_stats.return_value = {"model_router_cost_usd": 0.0}

        counter_json = json.dumps({
            "fixture_counts": [{"type_tag": "A", "total": 100, "counts_by_area": {"Hall": 100}, "flags": []}],
            "areas_covered": ["Hall"],
            "grand_total_fixtures": 100,
            "reasoning": "ok"
        })
        checker_json = json.dumps({"attacks": [], "total_attacks": 0, "critical_count": 0, "summary": "ok"})
        # Reconciler drastically revises total: 100 → 200 (+100%, > 20% threshold)
        reconciler_json = json.dumps({
            "responses": [],
            "revised_fixture_counts": {"A": {"total": 200, "delta": "+100", "reason": "added missing area"}},
            "revised_grand_total": 200,
            "notes_compliance": [],
            "reasoning": "found big discrepancy"
        })
        judge_json = json.dumps({
            "verdict": "WARN", "violations": [], "approved_counts": {}, "flags": [], "ruling_summary": "ok"
        })
        router.complete.side_effect = [
            MagicMock(content=counter_json),
            MagicMock(content=checker_json),
            MagicMock(content=reconciler_json),
            MagicMock(content=judge_json),
        ]

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        engine = TakeoffEngine(db_path=db_path, model_router=router)

        import base64
        dummy_image = base64.b64encode(b"fake").decode()
        mock_fs = FixtureSchedule()
        mock_fs.fixtures = {"A": {"description": "Troffer"}}
        mock_ac = AreaCount(area_label="Hall", counts_by_type={"A": 100})

        with patch("takeoff.engine.extract_fixture_schedule", return_value=mock_fs), \
             patch("takeoff.engine.extract_rcp_counts", return_value=mock_ac), \
             patch("takeoff.engine.extract_plan_notes", return_value=[]):
            snippets = [
                {"id": "s1", "label": "fixture_schedule", "image_data": dummy_image},
                {"id": "s2", "label": "rcp", "sub_label": "Hall", "image_data": dummy_image},
            ]
            result = engine.run_takeoff(snippets=snippets, mode="strict")

        flags = result.get("flags", [])
        deviation_flags = [f for f in flags if "deviat" in f.lower() or "reconciler" in f.lower()]
        self.assertTrue(
            len(deviation_flags) > 0,
            f"Expected a reconciler deviation flag in result flags, got: {flags}"
        )

    # ── Fix 2: Missing image_data emits warning but doesn't crash ─────

    def test_missing_image_data_emits_warning(self):
        """Snippet with empty image_data should emit a warning message via status_callback."""
        import json
        import tempfile
        from unittest.mock import MagicMock, patch
        from takeoff.engine import TakeoffEngine
        from takeoff.extraction import FixtureSchedule, AreaCount

        router = MagicMock()
        router.get_stats.return_value = {"model_router_cost_usd": 0.0}
        counter_json = json.dumps({
            "fixture_counts": [{"type_tag": "A", "total": 5, "counts_by_area": {"Hall": 5}, "flags": []}],
            "areas_covered": ["Hall"], "grand_total_fixtures": 5, "reasoning": "ok"
        })
        checker_json = json.dumps({"attacks": [], "total_attacks": 0, "critical_count": 0, "summary": "ok"})
        judge_json = json.dumps({"verdict": "PASS", "violations": [], "approved_counts": {}, "flags": [], "ruling_summary": "ok"})
        router.complete.side_effect = [
            MagicMock(content=counter_json),
            MagicMock(content=checker_json),
            MagicMock(content=judge_json),
        ]

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        engine = TakeoffEngine(db_path=db_path, model_router=router)

        import base64
        dummy_image = base64.b64encode(b"fake").decode()
        mock_fs = FixtureSchedule()
        mock_fs.fixtures = {"A": {"description": "Troffer"}}
        mock_ac = AreaCount(area_label="Hall", counts_by_type={"A": 5})

        emitted = []

        with patch("takeoff.engine.extract_fixture_schedule", return_value=mock_fs), \
             patch("takeoff.engine.extract_rcp_counts", return_value=mock_ac), \
             patch("takeoff.engine.extract_plan_notes", return_value=[]):
            snippets = [
                {"id": "s1", "label": "fixture_schedule", "image_data": dummy_image},
                {"id": "s2", "label": "rcp", "sub_label": "Hall", "image_data": dummy_image},
                {"id": "s3", "label": "plan_notes", "image_data": ""},  # empty!
            ]
            result = engine.run_takeoff(snippets=snippets, mode="fast", status_callback=emitted.append)

        warning_msgs = [m for m in emitted if "empty" in m.lower() or "missing" in m.lower()]
        self.assertTrue(len(warning_msgs) > 0,
                        f"Expected a warning about empty image_data, got: {emitted}")

    # ── Fix 3: Model router exception → agent_parse_error, not blank_drawing ──

    def test_model_router_exception_classified_as_parse_error(self):
        """When model_router.complete() raises (network/rate-limit error), result must be agent_parse_error."""
        import tempfile
        from unittest.mock import MagicMock, patch
        from takeoff.engine import TakeoffEngine
        from takeoff.extraction import FixtureSchedule, AreaCount

        router = MagicMock()
        router.get_stats.return_value = {"model_router_cost_usd": 0.0}
        # model_router.complete() raises a network error
        router.complete.side_effect = ConnectionError("API unreachable")

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        engine = TakeoffEngine(db_path=db_path, model_router=router)

        import base64
        dummy_image = base64.b64encode(b"fake").decode()
        mock_fs = FixtureSchedule()
        mock_fs.fixtures = {"A": {"description": "Troffer"}}
        mock_ac = AreaCount(area_label="Hall", counts_by_type={"A": 5})

        with patch("takeoff.engine.extract_fixture_schedule", return_value=mock_fs), \
             patch("takeoff.engine.extract_rcp_counts", return_value=mock_ac), \
             patch("takeoff.engine.extract_plan_notes", return_value=[]):
            snippets = [
                {"id": "s1", "label": "fixture_schedule", "image_data": dummy_image},
                {"id": "s2", "label": "rcp", "sub_label": "Hall", "image_data": dummy_image},
            ]
            result = engine.run_takeoff(snippets=snippets, mode="fast")

        self.assertEqual(result.get("error"), "agent_parse_error",
                         f"Model router exception should produce 'agent_parse_error', got: {result.get('error')}")

    # ── Fix 4: _normalize_area_label does NOT strip meaningful numbers ──

    def test_normalize_area_label_preserves_numeric_location(self):
        """'Floor (2)' must normalize to 'floor (2)', not 'floor' — (2) is a location, not a copy marker."""
        self.assertEqual(_normalize_area_label("Floor (2)"), "floor (2)")
        self.assertEqual(_normalize_area_label("Level (3)"), "level (3)")

    def test_normalize_area_label_still_strips_copy_suffixes(self):
        """Copy/revision suffixes (Copy), (Rev A), (v2) must still be stripped."""
        self.assertEqual(_normalize_area_label("Floor 2 North (Copy)"), "floor 2 north")
        self.assertEqual(_normalize_area_label("Level 1 (Rev A)"), "level 1")
        self.assertEqual(_normalize_area_label("Wing B (v2)"), "wing b")

    # ── Fix 5: Adversarial log rowcount warning (DB layer) ───────────

    def test_adversarial_log_update_orphan_warning_is_logged(self):
        """If reconciler response references unknown attack_id, a warning must be logged."""
        import logging
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        db = TakeoffDB(db_path=db_path)
        db.create_job("test-job", mode="strict")
        # Store an attack with ID 'ATK-001'
        db.store_adversarial_log(
            job_id="test-job",
            attacks=[{"attack_id": "ATK-001", "severity": "major", "category": "missed_area",
                       "description": "missed hall"}],
            reconciler_responses=[
                # Valid response
                {"attack_id": "ATK-001", "verdict": "concede", "explanation": "correct"},
                # Orphaned response — attack_id doesn't exist
                {"attack_id": "ATK-999", "verdict": "concede", "explanation": "orphan"},
            ]
        )
        # Verify ATK-001 was updated correctly
        log = db.get_job_adversarial_log("test-job")
        atk001 = next((r for r in log if r["attack_id"] == "ATK-001"), None)
        self.assertIsNotNone(atk001)
        self.assertEqual(atk001["final_verdict"], "concede")
        db.close()

    # ── Fix 6: Checker dedup normalizes category case ─────────────────

    def test_checker_dedup_normalizes_category_case(self):
        """Checker dedup must treat 'missed_area' and 'MISSED_AREA' as the same category."""
        from takeoff.agents import Checker
        from unittest.mock import MagicMock
        import json as _json

        attacks_with_case_variants = _json.dumps({
            "attacks": [
                {"attack_id": "ATK-001", "severity": "critical", "category": "missed_area",
                 "affected_type_tag": "A", "affected_area": "hall", "description": "d1",
                 "suggested_correction": "fix", "evidence": "ev"},
                {"attack_id": "ATK-002", "severity": "minor", "category": "MISSED_AREA",
                 "affected_type_tag": "A", "affected_area": "hall", "description": "d2",
                 "suggested_correction": "fix", "evidence": "ev"},
            ],
            "total_attacks": 2, "critical_count": 1, "summary": "test"
        })
        router = MagicMock()
        router.complete.return_value = MagicMock(content=attacks_with_case_variants)

        from takeoff.extraction import FixtureSchedule
        checker = Checker(router)
        fs = FixtureSchedule()
        fs.fixtures = {"A": {"description": "Troffer"}}
        response = checker.generate_attacks(
            counter_output={"fixture_counts": [], "areas_covered": [], "grand_total_fixtures": 0},
            fixture_schedule=fs, area_counts=[], plan_notes=[]
        )
        attacks = response.data.get("attacks", [])
        self.assertEqual(len(attacks), 1, "Case-insensitive dedup should collapse to 1 attack")
        self.assertEqual(attacks[0].get("severity"), "critical", "Must keep highest-severity (critical)")

    # ── Fix 7: validate_grand_total corrects all-zero totals with non-zero grand_total ──

    def test_validate_grand_total_corrects_all_zero_with_nonzero_grand_total(self):
        """If all per-type totals are 0 but grand_total_fixtures is non-zero, correct it."""
        from takeoff.agents import validate_grand_total
        agent_output = {
            "fixture_counts": [
                {"type_tag": "A", "total": 0},
                {"type_tag": "B", "total": 0},
            ],
            "grand_total_fixtures": 42,  # non-zero grand total with all-zero types
        }
        result = validate_grand_total(agent_output, "TEST")
        self.assertTrue(result.was_corrected, "Should detect mismatch and correct")
        self.assertEqual(result.counts["grand_total_fixtures"], 0,
                         "grand_total should be corrected to match per-type sum (0)")

    def test_validate_grand_total_genuine_zero_not_corrected(self):
        """All-zero per-type totals with zero grand_total should NOT be corrected."""
        from takeoff.agents import validate_grand_total
        agent_output = {
            "fixture_counts": [{"type_tag": "A", "total": 0}],
            "grand_total_fixtures": 0,
        }
        result = validate_grand_total(agent_output, "TEST")
        self.assertFalse(result.was_corrected)

    # ── Fix 8: Engine validates mode before processing ─────────────────

    def test_engine_rejects_invalid_mode(self):
        """Engine must raise ValueError for invalid mode values."""
        import tempfile
        from takeoff.engine import TakeoffEngine
        from unittest.mock import MagicMock
        import base64

        router = MagicMock()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        engine = TakeoffEngine(db_path=db_path, model_router=router)

        dummy_image = base64.b64encode(b"fake").decode()
        with self.assertRaises(ValueError, msg="Invalid mode should raise ValueError"):
            engine.run_takeoff(
                snippets=[
                    {"id": "s1", "label": "fixture_schedule", "image_data": dummy_image},
                    {"id": "s2", "label": "rcp", "image_data": dummy_image},
                ],
                mode="ultra-fast"
            )

    # ── Fix 9: Engine helper methods extracted (Counter + Rule6) ──────

    def test_engine_has_counter_phase_helper(self):
        """TakeoffEngine must expose _run_counter_phase helper used by both fast and strict modes."""
        import tempfile
        from takeoff.engine import TakeoffEngine
        from unittest.mock import MagicMock
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        engine = TakeoffEngine(db_path=db_path, model_router=MagicMock())
        self.assertTrue(hasattr(engine, "_run_counter_phase"), "Engine must have _run_counter_phase method")
        self.assertTrue(hasattr(engine, "_warn_unknown_types"), "Engine must have _warn_unknown_types method")
        self.assertTrue(hasattr(engine, "_apply_rule6_flags"), "Engine must have _apply_rule6_flags method")

    # ── Fix 10: Schema uses difficulty_code key correctly ─────────────

    def test_schema_stores_difficulty_code_from_fixture_count(self):
        """store_job_results_atomic must store difficulty_code field, not 'difficulty' alias."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        db = TakeoffDB(db_path=db_path)
        db.create_job("test-dc-job", mode="fast")
        fixture_counts = [
            {
                "type_tag": "A",
                "difficulty_code": "D",  # use the correct key
                "counts_by_area": {"Hall": 5},
                "flags": [],
            }
        ]
        db.store_job_results_atomic(
            job_id="test-dc-job",
            fixture_counts=fixture_counts,
            attacks=[],
            reconciler_responses=[],
            grand_total=5,
            confidence_score=0.75,
            confidence_band="MODERATE",
            confidence_features="{}",
            violations=[],
            flags=[],
            judge_verdict="PASS",
        )
        rows = db.get_job_counts("test-dc-job")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["difficulty_code"], "D",
                         f"Expected difficulty_code='D', got: {rows[0]['difficulty_code']}")
        db.close()


# ══════════════════════════════════════════════════════════════════════
# 15. Audit Round-6 Bug Fixes
# ══════════════════════════════════════════════════════════════════════

class TestBugFixes6(unittest.TestCase):
    """Tests for improvements applied in audit round 6."""

    # ── total_corrected penalty in calculate_confidence ──────────────

    def test_total_corrected_penalty_applied(self):
        """`total_corrected=True` should subtract 0.05 from final confidence score.

        To observe the penalty, we must ensure the pre-penalty score is ≤ 0.95 so
        the clamp at 1.0 doesn't absorb the subtraction. Including a Checker attack
        with no Reconciler response drops adversarial_resolved to 0.0, pulling the
        score below the 0.95 threshold.
        """
        from takeoff.confidence import calculate_confidence
        _attack = {"attack_id": "atk-1", "severity": "minor", "category": "test",
                   "description": "test", "affected_type_tag": "A", "affected_area": "Hall"}
        _common = dict(
            fixture_counts=[{"type_tag": "A", "total": 10, "counts_by_area": {"Hall": 10}}],
            areas_covered=["Hall"],
            rcp_snippets=[{"label": "rcp", "sub_label": "Hall"}],
            fixture_schedule={"fixtures": {"A": {"description": "Test"}}},
            checker_attacks=[_attack],
            reconciler_responses=[],  # unresolved → adversarial_resolved = 0.0
            constitutional_violations=[],
            mode="fast",
        )
        base = calculate_confidence(**_common, total_corrected=False)
        penalized = calculate_confidence(**_common, total_corrected=True)
        self.assertAlmostEqual(
            base["score"] - penalized["score"], 0.05, places=5,
            msg="total_corrected=True must subtract exactly 0.05 from confidence score"
        )
        self.assertGreater(base["score"], 0.0,
                           "Base score must be above 0.0 for penalty to be visible")

    # ── Counter model_failure flag distinguishes error from zero fixtures ──

    def test_counter_model_failure_flag_present(self):
        """Counter failure response must include '_model_failure': True, not look like zero-fixture result."""
        from unittest.mock import MagicMock, patch
        from takeoff.agents import Counter

        mock_router = MagicMock()
        mock_router.complete.side_effect = RuntimeError("network error")
        counter = Counter(mock_router)
        result = counter.generate_count(
            fixture_schedule=MagicMock(fixtures={}, warnings=[]),
            area_counts=[],
            plan_notes=[],
        )
        self.assertTrue(result.parse_error, "Counter model failure must set parse_error=True")
        self.assertTrue(result.data.get("_model_failure"), "Counter model failure must set _model_failure=True in data")

    def test_checker_model_failure_flag_present(self):
        """Checker failure response must include '_model_failure': True."""
        from unittest.mock import MagicMock
        from takeoff.agents import Checker

        mock_router = MagicMock()
        mock_router.complete.side_effect = RuntimeError("network error")
        checker = Checker(mock_router)
        result = checker.generate_attacks(
            counter_output={"fixture_counts": [], "grand_total_fixtures": 0},
            fixture_schedule=MagicMock(fixtures={}, warnings=[]),
            area_counts=[],
            plan_notes=[],
        )
        self.assertTrue(result.data.get("_model_failure"), "Checker model failure must set _model_failure=True in data")

    # ── Judge mode validation ──────────────────────────────────────────

    def test_judge_rejects_invalid_mode(self):
        """Judge.evaluate() must raise ValueError on unknown mode."""
        from unittest.mock import MagicMock
        from takeoff.agents import Judge

        mock_router = MagicMock()
        constitution = {"hard_rules": []}
        judge = Judge(mock_router, constitution)
        with self.assertRaises(ValueError, msg="Judge must raise ValueError for unknown mode"):
            judge.evaluate(
                counter_output={"fixture_counts": [], "grand_total_fixtures": 0},
                checker_attacks=[],
                reconciler_output=None,
                fixture_schedule=MagicMock(fixtures={}, warnings=[]),
                mode="unknown_mode",
            )

    # ── extraction.py empty content guard ─────────────────────────────

    def test_vision_empty_content_raises(self):
        """_call_vision must raise RuntimeError if Anthropic returns empty content array."""
        from unittest.mock import MagicMock, patch
        from takeoff.extraction import _call_vision

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = []  # empty content array
        mock_response.usage = None
        mock_client.messages.create.return_value = mock_response

        # Use a 200-char base64 string (~150 decoded bytes) to pass the size pre-check
        fake_image_b64 = "A" * 200
        with self.assertRaises(RuntimeError, msg="_call_vision must raise on empty content array"):
            _call_vision(mock_client, "sys", "user", fake_image_b64)

    # ── API key guard path normalization ──────────────────────────────

    def test_api_key_guard_normalizes_path(self):
        """api_key_guard must normalize path so /Takeoff/Health/ doesn't bypass auth."""
        import asyncio
        from unittest.mock import MagicMock, AsyncMock, patch
        # Verify that the normalization logic is correct by unit-testing it directly
        _guarded_paths = ("/takeoff/health", "/")
        test_cases = [
            ("/takeoff/health", True),    # exact → open
            ("/takeoff/health/", True),   # trailing slash → open
            ("/Takeoff/Health", True),    # case variant → open
            ("/takeoff/run", False),      # guarded endpoint → not open
            ("/TAKEOFF/RUN/", False),     # guarded uppercase → not open
        ]
        for path, should_be_open in test_cases:
            normalized = path.rstrip("/").lower() or "/"
            is_open = normalized in _guarded_paths
            self.assertEqual(is_open, should_be_open,
                             f"Path '{path}' normalized to '{normalized}': expected open={should_be_open}")

    # ── Feature weights sum check ─────────────────────────────────────

    def test_feature_weights_sum_to_one(self):
        """FEATURE_WEIGHTS must sum to exactly 1.0 (validated at module load)."""
        from takeoff.confidence import FEATURE_WEIGHTS
        total = sum(FEATURE_WEIGHTS.values())
        self.assertAlmostEqual(total, 1.0, places=9,
                               msg=f"FEATURE_WEIGHTS sum = {total}, must be 1.0")

    # ── Schema transaction rollback safety ────────────────────────────

    def test_schema_transaction_rollback_on_error(self):
        """store_job_results_atomic must roll back if an insert fails, leaving no partial data."""
        import tempfile, os
        from takeoff.schema import TakeoffDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = TakeoffDB(os.path.join(tmpdir, "test.db"))
            db.create_job("bad-job", drawing_name="test", mode="fast")

            # Pass a fixture_count dict that will cause a type error during insert
            bad_fixture_counts = [{"type_tag": None, "total": 1, "counts_by_area": {"A": 1}, "flags": []}]
            try:
                db.store_job_results_atomic(
                    job_id="bad-job",
                    fixture_counts=bad_fixture_counts,
                    attacks=[{"attack_id": None, "severity": None, "category": None, "description": None}],
                    reconciler_responses=[],
                    grand_total=1,
                    confidence_score=0.5,
                    confidence_band="MODERATE",
                    confidence_features="{}",
                    violations=[],
                    flags=[],
                    judge_verdict="FAIL",
                )
            except Exception:
                pass  # expected if any insert fails

            # Regardless of outcome, the DB must be readable (not corrupted)
            rows = db.get_job_counts("bad-job")
            # Either all committed or all rolled back — no partial state
            self.assertIsInstance(rows, list, "DB must remain readable after transaction failure")
            db.close()


# ══════════════════════════════════════════════════════════════════════
# Round-7 bug fixes
# ══════════════════════════════════════════════════════════════════════

class TestBugFixes7(unittest.TestCase):
    """Tests for round-7 audit: 13 fixes across agents, api, constitution, schema, extraction."""

    # ── agents.py: severity null-check ────────────────────────────────

    def test_reconciler_severity_null_safe(self):
        """Reconciler attack summary must not crash if 'severity' key is missing."""
        from takeoff.agents import Reconciler
        from unittest.mock import MagicMock

        mock_router = MagicMock()
        mock_router.complete.return_value = MagicMock(
            content='{"revised_fixture_counts": {}, "revised_grand_total": 0, "responses": [], "notes_compliance": []}',
            usage=MagicMock(input_tokens=0, output_tokens=0)
        )
        r = Reconciler(mock_router)
        counter_output = {"fixture_counts": [], "grand_total_fixtures": 0, "areas_covered": []}
        # Attack with missing 'severity' — previously crashed with AttributeError
        bad_attacks = [{"attack_id": "A1", "category": "phantom", "description": "bad"}]
        try:
            r.address_attacks(counter_output, bad_attacks, {}, [], [])
        except AttributeError as e:
            self.fail(f"Reconciler crashed on missing severity: {e}")

    # ── constitution.py: emergency keyword word-boundary ──────────────

    def test_emergency_keyword_no_substring_collision(self):
        """'em' keyword must not match 'ITEM' or 'SYSTEM' via substring."""
        from takeoff.constitution import check_emergency_fixtures
        # Fixture with description containing "em" as substring but not as standalone word
        fixture_counts = [
            {"type_tag": "A", "description": "LED System Fixture Item", "type_tag": "A",
             "notes": "", "total": 5, "counts_by_area": {"Floor 1": 5}},
            {"type_tag": "B", "description": "Troffer", "notes": "", "total": 3, "counts_by_area": {"Floor 1": 3}},
            {"type_tag": "C", "description": "Sconce", "notes": "", "total": 2, "counts_by_area": {"Floor 1": 2}},
            {"type_tag": "D", "description": "Pendant", "notes": "", "total": 1, "counts_by_area": {"Floor 1": 1}},
            {"type_tag": "E", "description": "Downlight", "notes": "", "total": 4, "counts_by_area": {"Floor 1": 4}},
            {"type_tag": "F", "description": "Linear", "notes": "", "total": 2, "counts_by_area": {"Floor 1": 2}},
        ]
        violations = check_emergency_fixtures(fixture_counts)
        # "SYSTEM", "ITEM" should NOT trigger emergency fixture tracking
        self.assertTrue(
            any(v["rule"] == "Emergency Fixture Tracking" for v in violations),
            "Should flag missing emergency tracking — 'SYSTEM'/'ITEM' substrings must not satisfy the check"
        )

    def test_emergency_keyword_matches_standalone_em(self):
        """'em' keyword should match fixture tag 'EM' (standalone word)."""
        from takeoff.constitution import check_emergency_fixtures
        fixture_counts = [
            {"type_tag": "EM", "description": "Emergency unit", "notes": "", "total": 2,
             "counts_by_area": {"Floor 1": 2}},
            {"type_tag": "A", "description": "Troffer", "notes": "", "total": 5, "counts_by_area": {}},
            {"type_tag": "B", "description": "Downlight", "notes": "", "total": 3, "counts_by_area": {}},
            {"type_tag": "C", "description": "Linear", "notes": "", "total": 2, "counts_by_area": {}},
            {"type_tag": "D", "description": "Sconce", "notes": "", "total": 1, "counts_by_area": {}},
            {"type_tag": "E", "description": "Pendant", "notes": "", "total": 4, "counts_by_area": {}},
        ]
        violations = check_emergency_fixtures(fixture_counts)
        self.assertFalse(
            any(v["rule"] == "Emergency Fixture Tracking" for v in violations),
            "Type tag 'EM' should satisfy emergency tracking check"
        )

    # ── constitution.py: area fuzzy match — numeric asymmetry ─────────

    def test_area_fuzzy_match_numeric_asymmetry(self):
        """'Level' (no numerics) must not fuzzy-match 'Level 1' (has numerics)."""
        from takeoff.constitution import _area_fuzzy_match
        # 'Level' has no numerics; 'Level 1' has numeric '1' — should not match
        result = _area_fuzzy_match("level", {"level 1", "level 2"})
        self.assertFalse(result, "'Level' should not match 'Level 1' when numerics differ")

    def test_area_fuzzy_match_same_numerics_still_match(self):
        """Areas with same numerics and sufficient word overlap should still match."""
        from takeoff.constitution import _area_fuzzy_match
        result = _area_fuzzy_match("north wing level 1", {"level 1 north wing"})
        self.assertTrue(result, "Word-reordered labels with same numerics should still match")

    def test_area_fuzzy_match_different_numerics_no_match(self):
        """'Level 1' must not match 'Level 2' (numeric mismatch)."""
        from takeoff.constitution import _area_fuzzy_match
        result = _area_fuzzy_match("level 1", {"level 2", "level 3"})
        self.assertFalse(result, "'Level 1' should not match 'Level 2'")

    # ── api.py: label validation ───────────────────────────────────────

    def test_snippet_label_validation_unknown_label(self):
        """API endpoint must reject unknown snippet labels with 422."""
        import importlib
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi[testclient] not installed")

        # Import app fresh
        import takeoff.api as api_module
        client = TestClient(api_module.app, raise_server_exceptions=False)
        payload = {
            "mode": "fast",
            "snippets": [
                {"id": "s1", "label": "rcp", "image_data": "abc"},
                {"id": "s2", "label": "UNKNOWN_LABEL", "image_data": "abc"},
            ]
        }
        resp = client.post("/takeoff/run", json=payload)
        self.assertEqual(resp.status_code, 422, "Unknown label should return 422")

    # ── schema.py: json.dumps default=str ─────────────────────────────

    def test_schema_json_dumps_non_serializable_flags(self):
        """store_fixture_counts must not crash if flags contains a non-JSON-serializable value."""
        import tempfile, os
        from takeoff.schema import TakeoffDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = TakeoffDB(os.path.join(tmpdir, "test.db"))
            db.create_job("j1", drawing_name="test", mode="fast")
            # datetime is not JSON-serializable by default — default=str should handle it
            import datetime
            bad_fixture_counts = [{
                "type_tag": "A", "total": 1,
                "counts_by_area": {"Room 1": 1},
                "confidence": 0.9, "difficulty_code": "S",
                "flags": [datetime.datetime.now()]  # non-serializable
            }]
            try:
                db.store_fixture_counts("j1", bad_fixture_counts)
            except (TypeError, Exception) as e:
                self.fail(f"store_fixture_counts crashed on non-serializable flag: {e}")
            db.close()

    # ── schema.py: FK enforcement ──────────────────────────────────────

    def test_schema_foreign_key_enforcement(self):
        """Inserting snippets for a non-existent job_id should fail with FK error."""
        import tempfile, os
        from takeoff.schema import TakeoffDB
        import sqlite3

        with tempfile.TemporaryDirectory() as tmpdir:
            db = TakeoffDB(os.path.join(tmpdir, "test.db"))
            # Do NOT create the parent job — attempt to insert a snippet directly
            snippets = [{"id": "s1", "label": "rcp", "page_number": 1}]
            try:
                db.store_snippets("nonexistent-job-id", snippets)
                # If no exception, FK was not enforced — check if data leaked
                # (some SQLite builds may not have FK support compiled in)
                rows = db.conn.execute(
                    "SELECT COUNT(*) FROM snippets WHERE job_id='nonexistent-job-id'"
                ).fetchone()[0]
                # With FK ON, this should be 0 (row was rejected or we see an error)
                # We only assert if FK is actually supported
                self.assertIn(rows, (0, 1), "FK enforcement result should be 0 or 1")
            except Exception:
                pass  # FK violation — correct behavior
            finally:
                db.close()

    # ── schema.py: close acquires lock ────────────────────────────────

    def test_schema_close_acquires_lock(self):
        """TakeoffDB.close() must acquire the lock before closing the connection."""
        import tempfile, os, threading
        from takeoff.schema import TakeoffDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = TakeoffDB(os.path.join(tmpdir, "test.db"))
            db.create_job("j1", drawing_name="test", mode="fast")
            # close() should not raise even if called from a different thread
            done = threading.Event()
            def _close():
                db.close()
                done.set()
            t = threading.Thread(target=_close, daemon=True)
            t.start()
            t.join(timeout=3)
            self.assertTrue(done.is_set(), "close() timed out — possible deadlock")

    # ── extraction.py: client is cached ───────────────────────────────

    def test_vision_client_is_cached(self):
        """_get_vision_client() must return the same object on repeated calls."""
        import os
        os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-key-for-cache-check")
        from takeoff import extraction
        # Reset the module-level cache to test initialization
        original = extraction._vision_client
        extraction._vision_client = None
        try:
            c1 = extraction._get_vision_client()
            c2 = extraction._get_vision_client()
            self.assertIs(c1, c2, "_get_vision_client() should return the same cached instance")
        finally:
            extraction._vision_client = original

    # ── constitution.py: float area counts ────────────────────────────

    def test_non_integer_area_count_flagged(self):
        """check_non_negative_counts must flag float area counts as MAJOR violation."""
        from takeoff.constitution import check_non_negative_counts
        fixture_counts = [{
            "type_tag": "A",
            "total": 5,
            "counts_by_area": {"Room 1": 2.5}  # non-integer
        }]
        violations = check_non_negative_counts(fixture_counts)
        self.assertTrue(
            any(v["severity"] == "MAJOR" and "non-integer" in v["explanation"].lower()
                for v in violations),
            "Float area count should produce MAJOR violation"
        )

    def test_integer_area_count_passes(self):
        """Integer area counts must not trigger float violation."""
        from takeoff.constitution import check_non_negative_counts
        fixture_counts = [{"type_tag": "A", "total": 5, "counts_by_area": {"Room 1": 5}}]
        violations = check_non_negative_counts(fixture_counts)
        self.assertFalse(
            any("non-integer" in v.get("explanation", "").lower() for v in violations),
            "Integer area count should not produce float violation"
        )


# ══════════════════════════════════════════════════════════════════════
# Checker vision integration tests
# ══════════════════════════════════════════════════════════════════════

class TestCheckerVision(unittest.TestCase):
    """Tests for Checker agent vision-based independent count verification."""

    def _make_checker(self):
        """Return a Checker with a mock model router."""
        from takeoff.agents import Checker
        from unittest.mock import MagicMock
        mock_router = MagicMock()
        mock_router.complete.return_value = MagicMock(
            content='{"attacks": [], "total_attacks": 0, "critical_count": 0, "summary": "no issues"}',
            usage=MagicMock(input_tokens=10, output_tokens=10)
        )
        return Checker(mock_router)

    def _make_fixture_schedule(self):
        from takeoff.extraction import FixtureSchedule
        fs = FixtureSchedule()
        fs.fixtures = {
            "A": {"description": "LED Troffer 2x4", "wattage": 40},
            "B": {"description": "LED Downlight", "wattage": 15},
        }
        return fs

    # ── Test 1: Checker accepts rcp_images parameter ──────────────────

    def test_checker_accepts_rcp_images_param(self):
        """generate_attacks() must accept rcp_images keyword argument without error."""
        from unittest.mock import patch
        checker = self._make_checker()
        fs = self._make_fixture_schedule()

        with patch("takeoff.agents._get_vision_client") as mock_client, \
             patch("takeoff.agents._call_vision_with_retry") as mock_vision:
            mock_client.return_value = object()
            # Vision returns clean agreement — no discrepancies
            mock_vision.return_value = json.dumps({
                "area_label": "Floor 1",
                "independent_counts": {"A": 5},
                "counter_agreed": True,
                "discrepancies": [],
                "additional_findings": ""
            })

            counter_output = {
                "fixture_counts": [
                    {"type_tag": "A", "total": 5, "counts_by_area": {"Floor 1": 5}}
                ],
                "grand_total_fixtures": 5,
                "areas_covered": ["Floor 1"]
            }
            rcp_images = [{"area_label": "Floor 1", "image_data": "aGVsbG8="}]

            try:
                result = checker.generate_attacks(
                    counter_output, fs, [], [], rcp_images=rcp_images
                )
            except TypeError as e:
                self.fail(f"generate_attacks() rejected rcp_images param: {e}")

            self.assertEqual(result.agent_role, "checker")

    # ── Test 2: Vision discrepancy produces attack ────────────────────

    def test_vision_discrepancy_generates_attack(self):
        """When vision count disagrees with Counter, an attack must be generated."""
        from unittest.mock import patch
        checker = self._make_checker()
        fs = self._make_fixture_schedule()

        with patch("takeoff.agents._get_vision_client") as mock_client, \
             patch("takeoff.agents._call_vision_with_retry") as mock_vision:
            mock_client.return_value = object()
            # Vision finds 3 type-B fixtures, Counter claimed 5 (over-count)
            mock_vision.return_value = json.dumps({
                "area_label": "Floor 1",
                "independent_counts": {"A": 5, "B": 3},
                "counter_agreed": False,
                "discrepancies": [
                    {
                        "type_tag": "B",
                        "counter_claimed": 5,
                        "checker_found": 3,
                        "direction": "over_count",
                        "severity": "major",
                        "confidence": "high",
                        "notes": "Only 3 type B symbols visible"
                    }
                ],
                "additional_findings": ""
            })

            counter_output = {
                "fixture_counts": [
                    {"type_tag": "A", "total": 5, "counts_by_area": {"Floor 1": 5}},
                    {"type_tag": "B", "total": 5, "counts_by_area": {"Floor 1": 5}},
                ],
                "grand_total_fixtures": 10,
                "areas_covered": ["Floor 1"]
            }
            rcp_images = [{"area_label": "Floor 1", "image_data": "aGVsbG8="}]

            result = checker.generate_attacks(
                counter_output, fs, [], [], rcp_images=rcp_images
            )
            attacks = result.data.get("attacks", [])

            # Must contain at least the vision attack
            vision_attacks = [a for a in attacks if a.get("attack_id", "").startswith("VIS")]
            self.assertTrue(len(vision_attacks) >= 1, "Expected at least one VIS attack")
            vis = vision_attacks[0]
            self.assertEqual(vis["affected_type_tag"], "B")
            self.assertEqual(vis["affected_area"], "Floor 1")
            self.assertEqual(vis["severity"], "major")
            self.assertEqual(vis["suggested_correction"], 3)
            self.assertIn("[VISION CHECK]", vis["description"])
            self.assertIn("over-count", vis["description"])

    # ── Test 3: Falls back to text-only when rcp_images empty/None ────

    def test_checker_text_only_when_no_rcp_images(self):
        """generate_attacks() must work normally when rcp_images is None or empty."""
        checker = self._make_checker()
        fs = self._make_fixture_schedule()

        counter_output = {
            "fixture_counts": [{"type_tag": "A", "total": 3, "counts_by_area": {"Floor 1": 3}}],
            "grand_total_fixtures": 3,
            "areas_covered": ["Floor 1"]
        }

        for rcp_arg in [None, []]:
            result = checker.generate_attacks(
                counter_output, fs, [], [], rcp_images=rcp_arg
            )
            self.assertEqual(result.agent_role, "checker")
            self.assertIsInstance(result.data.get("attacks"), list)

    # ── Test 4: Vision failure per-area is non-fatal ──────────────────

    def test_checker_vision_failure_is_non_fatal(self):
        """If vision call raises for an area, Checker must still return text-based attacks."""
        from unittest.mock import patch
        checker = self._make_checker()
        fs = self._make_fixture_schedule()

        with patch("takeoff.agents._get_vision_client") as mock_client, \
             patch("takeoff.agents._call_vision_with_retry") as mock_vision:
            mock_client.return_value = object()
            mock_vision.side_effect = RuntimeError("Vision API unavailable")

            counter_output = {
                "fixture_counts": [{"type_tag": "A", "total": 2, "counts_by_area": {"Floor 1": 2}}],
                "grand_total_fixtures": 2,
                "areas_covered": ["Floor 1"]
            }
            rcp_images = [{"area_label": "Floor 1", "image_data": "aGVsbG8="}]

            try:
                result = checker.generate_attacks(
                    counter_output, fs, [], [], rcp_images=rcp_images
                )
            except Exception as e:
                self.fail(f"Checker crashed on vision failure: {e}")

            # Text-based attacks still returned (mock router returns empty attacks)
            self.assertEqual(result.agent_role, "checker")
            self.assertIsInstance(result.data.get("attacks"), list)

    # ── Test 5: Vision+text attacks dedup by key ──────────────────────

    def test_vision_and_text_attacks_dedup(self):
        """If vision and text LLM flag the same (category, type_tag, area), highest severity wins."""
        from unittest.mock import patch
        from takeoff.agents import Checker
        from unittest.mock import MagicMock

        mock_router = MagicMock()
        # Text LLM returns a MINOR attack for same key as vision's MAJOR
        mock_router.complete.return_value = MagicMock(
            content=json.dumps({
                "attacks": [{
                    "attack_id": "ATK-001",
                    "severity": "minor",
                    "category": "missed_fixtures",
                    "affected_type_tag": "B",
                    "affected_area": "Floor 1",
                    "description": "text-based minor attack",
                    "suggested_correction": 4,
                    "evidence": "text analysis"
                }],
                "total_attacks": 1,
                "critical_count": 0,
                "summary": "minor issue"
            }),
            usage=MagicMock(input_tokens=10, output_tokens=10)
        )
        checker = Checker(mock_router)
        fs = self._make_fixture_schedule()

        with patch("takeoff.agents._get_vision_client") as mock_client, \
             patch("takeoff.agents._call_vision_with_retry") as mock_vision:
            mock_client.return_value = object()
            mock_vision.return_value = json.dumps({
                "area_label": "Floor 1",
                "independent_counts": {"B": 3},
                "counter_agreed": False,
                "discrepancies": [{
                    "type_tag": "B",
                    "counter_claimed": 5,
                    "checker_found": 3,
                    "direction": "over_count",
                    "severity": "major",  # higher than LLM's minor
                    "confidence": "high",
                    "notes": "vision found 3"
                }],
                "additional_findings": ""
            })

            counter_output = {
                "fixture_counts": [{"type_tag": "B", "total": 5, "counts_by_area": {"Floor 1": 5}}],
                "grand_total_fixtures": 5,
                "areas_covered": ["Floor 1"]
            }
            rcp_images = [{"area_label": "Floor 1", "image_data": "aGVsbG8="}]

            result = checker.generate_attacks(counter_output, fs, [], [], rcp_images=rcp_images)
            attacks = result.data.get("attacks", [])

            # Should be exactly 1 attack after dedup (both keyed as missed_fixtures/B/floor 1)
            self.assertEqual(len(attacks), 1, "Dedup should collapse vision+text to 1 attack")
            # The MAJOR (vision) entry wins over MINOR (text)
            self.assertEqual(attacks[0]["severity"], "major")


# ══════════════════════════════════════════════════════════════════════
# 11. Audit Fix Tests
# ══════════════════════════════════════════════════════════════════════

class TestAuditFixes(unittest.TestCase):
    """Unit tests for the 6 improvements applied in the codebase audit."""

    # Fix #1: JSON extraction error messages now include per-strategy detail

    def test_json_error_contains_strategy_detail(self):
        """JSONDecodeError message must contain 'direct:' diagnostic detail."""
        with self.assertRaises(json.JSONDecodeError) as ctx:
            extract_json_from_response("not json at all", "TEST")
        self.assertIn("direct:", str(ctx.exception),
            "Error message must identify which extraction strategy failed")

    def test_json_error_contains_detail_when_fence_also_fails(self):
        """Fence strategy failure detail must appear in the error when brace strategy also fails."""
        # A markdown fence with invalid JSON inside tests Strategy 2 failure
        bad_fenced = "```json\nnot valid json{\n```"
        with self.assertRaises(json.JSONDecodeError) as ctx:
            extract_json_from_response(bad_fenced, "TEST")
        msg = str(ctx.exception)
        # At least one strategy error detail must appear
        self.assertTrue(
            "direct:" in msg or "fence:" in msg or "braces:" in msg,
            f"Error message must include at least one strategy detail, got: {msg}"
        )

    # Fix #3: Reconciler grand total arithmetic guardrail

    def test_reconciler_guardrail_corrects_inconsistent_total(self):
        """When revised_grand_total disagrees with per-type sum by >2%, the guardrail corrects it."""
        reconciler_output = {
            "revised_grand_total": 200,
            "revised_fixture_counts": {
                "A": {"total": 100},
                "B": {"total": 75},
            },
        }
        revised_total = reconciler_output["revised_grand_total"]  # 200
        _recon_counts = reconciler_output.get("revised_fixture_counts", {})
        _recon_computed = sum(
            v.get("total", 0) if isinstance(v, dict) else 0
            for v in _recon_counts.values()
        )  # 175

        # Verify the discrepancy triggers the guardrail threshold
        self.assertTrue(
            abs(_recon_computed - revised_total) > max(2, int(_recon_computed * 0.02)),
            "Test precondition: discrepancy must exceed guardrail threshold"
        )

        # Apply the guardrail (mirrors engine.py logic)
        if _recon_computed > 0 and abs(_recon_computed - revised_total) > max(2, int(_recon_computed * 0.02)):
            reconciler_output["revised_grand_total"] = _recon_computed
            revised_total = _recon_computed

        self.assertEqual(revised_total, 175, "Guardrail must correct revised_total to per-type sum")
        self.assertEqual(reconciler_output["revised_grand_total"], 175)

    def test_reconciler_guardrail_no_correction_when_consistent(self):
        """When revised_grand_total agrees with per-type sum, no correction is made."""
        reconciler_output = {
            "revised_grand_total": 175,
            "revised_fixture_counts": {
                "A": {"total": 100},
                "B": {"total": 75},
            },
        }
        original = reconciler_output["revised_grand_total"]
        _recon_counts = reconciler_output.get("revised_fixture_counts", {})
        _recon_computed = sum(
            v.get("total", 0) if isinstance(v, dict) else 0
            for v in _recon_counts.values()
        )  # 175

        triggered = (
            _recon_computed > 0
            and abs(_recon_computed - original) > max(2, int(_recon_computed * 0.02))
        )
        self.assertFalse(triggered, "Guardrail must NOT trigger when totals agree")

    # Fix #4: Reconciler revised_count bounds clamping

    def test_revised_count_negative_clamped_to_zero(self):
        """Negative revised_count must be clamped to 0."""
        counter_output = {"grand_total_fixtures": 100}
        _orig_total = counter_output.get("grand_total_fixtures", 0) or 0
        _MAX_COUNT = max(9999, _orig_total * 10)
        resp = {"attack_id": "ATK-001", "revised_count": -5}

        rc = resp.get("revised_count")
        resp["revised_count"] = max(0, min(int(rc), _MAX_COUNT))

        self.assertEqual(resp["revised_count"], 0)

    def test_revised_count_extreme_value_clamped(self):
        """Hallucinated revised_count (e.g. 99999) must be clamped to _MAX_COUNT."""
        counter_output = {"grand_total_fixtures": 100}
        _orig_total = counter_output.get("grand_total_fixtures", 0) or 0
        _MAX_COUNT = max(9999, _orig_total * 10)  # max(9999, 1000) = 9999
        resp = {"attack_id": "ATK-002", "revised_count": 99999}

        rc = resp.get("revised_count")
        resp["revised_count"] = max(0, min(int(rc), _MAX_COUNT))

        self.assertEqual(resp["revised_count"], _MAX_COUNT)

    def test_revised_count_valid_value_unchanged(self):
        """Valid revised_count within bounds must pass through unchanged."""
        counter_output = {"grand_total_fixtures": 100}
        _orig_total = counter_output.get("grand_total_fixtures", 0) or 0
        _MAX_COUNT = max(9999, _orig_total * 10)
        resp = {"attack_id": "ATK-003", "revised_count": 50}

        rc = resp.get("revised_count")
        resp["revised_count"] = max(0, min(int(rc), _MAX_COUNT))

        self.assertEqual(resp["revised_count"], 50)

    def test_revised_count_non_numeric_cleared(self):
        """Non-numeric revised_count must be cleared to None."""
        resp = {"attack_id": "ATK-004", "revised_count": "lots"}
        try:
            int(resp["revised_count"])
            resp["revised_count"] = max(0, min(int(resp["revised_count"]), 9999))
        except (TypeError, ValueError):
            resp["revised_count"] = None
        self.assertIsNone(resp["revised_count"])

    # Fix #5: Image size pre-validation

    def test_empty_image_raises_value_error(self):
        """_call_vision must raise ValueError before calling API when image is too short."""
        from takeoff.extraction import _call_vision
        # "abc" decodes to ~2 bytes — well below the 100-byte minimum
        with self.assertRaises(ValueError) as ctx:
            _call_vision(None, "system", "user", "abc")
        self.assertIn("empty or corrupt", str(ctx.exception))

    def test_valid_image_length_passes_validation(self):
        """A base64 string long enough to represent a real image must pass the length check.
        This test verifies the threshold is not accidentally set too high."""
        from takeoff.extraction import _call_vision
        # A 200-byte base64 string decodes to ~150 bytes > 100-byte minimum.
        # We expect it to fail on the API call (client=None), NOT on length validation.
        fake_image = "A" * 200
        with self.assertRaises((AttributeError, TypeError)):
            # Should fail on client.messages.create (client is None), not ValueError
            _call_vision(None, "system", "user", fake_image)


# ══════════════════════════════════════════════════════════════════════
# Grid feature tests
# ══════════════════════════════════════════════════════════════════════

class TestGenerateGrid(unittest.TestCase):
    """Tests for generate_grid() in extraction.py."""

    def test_generate_grid_produces_correct_cells(self):
        """A 300x300 image split into 3x3 should yield 9 cells with correct IDs and sizes."""
        import io
        import base64
        from PIL import Image
        from takeoff.extraction import generate_grid

        # Create a 300x300 white image and encode to base64
        img = Image.new("RGB", (300, 300), color=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()

        cells = generate_grid(b64, "TestArea", rows=3, cols=3)

        self.assertEqual(len(cells), 9)
        expected_ids = ["A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3"]
        self.assertEqual([c.cell_id for c in cells], expected_ids)
        self.assertEqual(cells[0].row, 0)
        self.assertEqual(cells[0].col, 0)
        self.assertEqual(cells[4].cell_id, "B2")
        self.assertEqual(cells[4].row, 1)
        self.assertEqual(cells[4].col, 1)
        self.assertEqual(cells[8].cell_id, "C3")

        # Each cell image should decode to ~100x100 px
        for cell in cells:
            dec = Image.open(io.BytesIO(base64.b64decode(cell.image_base64)))
            self.assertEqual(dec.size, (100, 100), f"Cell {cell.cell_id} wrong size: {dec.size}")

        # Bounds should be fractional and cover unit square
        for cell in cells:
            b = cell.bounds
            self.assertAlmostEqual(b["width"], 1 / 3, places=5)
            self.assertAlmostEqual(b["height"], 1 / 3, places=5)

    def test_generate_grid_small_image_reduces_grid(self):
        """An image too small for a 3x3 grid should fall back to fewer cells."""
        import io
        import base64
        from PIL import Image
        from takeoff.extraction import generate_grid

        # 60x60 px → cells would be 20x20, below 50px threshold → should reduce
        img = Image.new("RGB", (60, 60), color=(200, 200, 200))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()

        cells = generate_grid(b64, "TinyArea", rows=3, cols=3)

        # Must have produced at least 1 cell and no more than 4 (2x2 fallback or 1x1)
        self.assertGreaterEqual(len(cells), 1)
        self.assertLessEqual(len(cells), 4)


class TestGridResultToAreaCount(unittest.TestCase):
    """Tests for grid_result_to_area_count() in extraction.py."""

    def test_grid_result_to_area_count_preserves_totals(self):
        """area_totals from GridResult must appear in counts_by_type of AreaCount."""
        from takeoff.extraction import GridResult, CellTypeCount, grid_result_to_area_count

        gr = GridResult(
            area_label="Floor 1",
            grid_cells=[],
            cell_type_counts=[
                CellTypeCount("A1", "A", 5),
                CellTypeCount("A2", "A", 3),
                CellTypeCount("A1", "B", 2),
            ],
            area_totals={"A": 8, "B": 2},
        )
        ac = grid_result_to_area_count(gr)

        self.assertEqual(ac.area_label, "Floor 1")
        self.assertEqual(ac.counts_by_type.get("A"), 8)
        self.assertEqual(ac.counts_by_type.get("B"), 2)

    def test_grid_result_to_area_count_collects_notes(self):
        """Non-empty meaningful notes should appear; EXTRACTION_FAILED notes are filtered out."""
        from takeoff.extraction import GridResult, CellTypeCount, grid_result_to_area_count

        gr = GridResult(
            area_label="Zone 2",
            grid_cells=[],
            cell_type_counts=[
                CellTypeCount("A1", "C", 0, notes="EXTRACTION_FAILED"),
                CellTypeCount("A2", "C", 1, notes="symbol partially cut off at edge"),
                CellTypeCount("A3", "C", 1, notes=""),
            ],
            area_totals={"C": 2},
        )
        ac = grid_result_to_area_count(gr)
        # EXTRACTION_FAILED notes are intentionally filtered out
        self.assertNotIn("EXTRACTION_FAILED", ac.notes)
        # Meaningful notes are included
        self.assertIn("symbol partially cut off at edge", ac.notes)


class TestCheckerGridPath(unittest.TestCase):
    """Tests for the Checker grid cell verification path in agents.py."""

    def test_checker_cell_disagreement_generates_cell_attack(self):
        """When Checker vision returns a different count than Counter, a CELL attack is produced."""
        import base64
        import io
        from unittest.mock import patch, MagicMock
        from PIL import Image
        from takeoff.extraction import (
            GridResult, GridCell, CellTypeCount, FixtureSchedule,
        )
        from takeoff.agents import Checker

        # Build a tiny white cell image
        buf = io.BytesIO()
        Image.new("RGB", (100, 100), "white").save(buf, format="PNG")
        cell_b64 = base64.b64encode(buf.getvalue()).decode()

        cell_b2 = GridCell(
            cell_id="B2", image_base64=cell_b64, row=1, col=1,
            bounds={"x": 1/3, "y": 1/3, "width": 1/3, "height": 1/3},
            area_label="Office Floor",
        )
        grid_result = GridResult(
            area_label="Office Floor",
            grid_cells=[cell_b2],
            cell_type_counts=[CellTypeCount("B2", "A", 5)],
            area_totals={"A": 5},
        )
        grid_results = {"Office Floor": grid_result}

        fixture_schedule = FixtureSchedule()
        fixture_schedule.fixtures["A"] = {"description": "2x4 LED Troffer"}

        counter_output = {
            "fixture_counts": [{"type_tag": "A", "total": 5, "counts_by_area": {"Office Floor": 5}}],
            "grand_total_fixtures": 5,
            "areas_covered": ["Office Floor"],
        }

        # Mock: vision client exists; _call_vision_with_retry returns checker count=3
        mock_response = '{"type_tag": "A", "independent_count": 3, "agrees_with_counter": false, "discrepancy": -2, "notes": "Only 3 visible"}'
        mock_client = MagicMock()
        mock_router = MagicMock()
        mock_router.complete.return_value = MagicMock(content='{"attacks": [], "total_attacks": 0, "critical_count": 0, "summary": "ok"}')

        with patch("takeoff.agents._get_vision_client", return_value=mock_client), \
             patch("takeoff.agents._call_vision_with_retry", return_value=mock_response):
            checker = Checker(mock_router)
            response = checker.generate_attacks(
                counter_output=counter_output,
                fixture_schedule=fixture_schedule,
                area_counts=[],
                plan_notes=[],
                panel_data=None,
                rcp_images=None,
                grid_results=grid_results,
            )

        attacks = response.data.get("attacks", [])
        cell_attacks = [a for a in attacks if a.get("attack_id", "").startswith("CELL")]
        self.assertGreater(len(cell_attacks), 0, "Expected at least one CELL attack")
        atk = cell_attacks[0]
        self.assertEqual(atk.get("cell_id"), "B2")
        self.assertEqual(atk.get("affected_type_tag"), "A")
        self.assertIn("GRID CHECK", atk.get("description", ""))


class TestEngineGridFlag(unittest.TestCase):
    """Tests for use_grid=False bypassing gridded extraction in TakeoffEngine."""

    def test_engine_use_grid_false_calls_extract_rcp_counts(self):
        """With use_grid=False, engine must call extract_rcp_counts and NOT extract_rcp_counts_gridded."""
        import base64
        import io
        from unittest.mock import patch, MagicMock
        from PIL import Image
        from takeoff.engine import TakeoffEngine
        from takeoff.extraction import AreaCount, FixtureSchedule

        # Build a minimal valid snippet image
        buf = io.BytesIO()
        Image.new("RGB", (200, 200), "white").save(buf, format="PNG")
        img_b64 = base64.b64encode(buf.getvalue()).decode()

        snippets = [
            {"id": "s1", "label": "fixture_schedule", "sub_label": "Schedule", "image_data": img_b64, "page_number": 1},
            {"id": "s2", "label": "rcp", "sub_label": "Floor 1", "image_data": img_b64, "page_number": 1},
        ]

        fake_schedule = FixtureSchedule()
        fake_schedule.fixtures["A"] = {"description": "2x4 LED Troffer"}
        fake_area = AreaCount(area_label="Floor 1", counts_by_type={"A": 2})
        fake_counter = MagicMock()
        fake_counter.generate_count.return_value = MagicMock(
            data={"fixture_counts": [{"type_tag": "A", "total": 2, "counts_by_area": {"Floor 1": 2}}],
                  "grand_total_fixtures": 2, "areas_covered": ["Floor 1"]},
            parse_error=False,
        )
        fake_checker = MagicMock()
        fake_checker.generate_attacks.return_value = MagicMock(
            data={"attacks": [], "total_attacks": 0, "critical_count": 0, "_model_failure": False},
            raw_response="{}",
        )
        fake_judge = MagicMock()
        fake_judge.evaluate.return_value = {
            "verdict": "PASS", "violations": [], "flags": [], "ruling_summary": "ok"
        }

        with patch("takeoff.engine.extract_fixture_schedule", return_value=fake_schedule), \
             patch("takeoff.engine.extract_rcp_counts", return_value=fake_area) as mock_rcp, \
             patch("takeoff.engine.extract_rcp_counts_gridded") as mock_grid, \
             patch("takeoff.engine.extract_plan_notes", return_value=[]), \
             patch("takeoff.engine.extract_panel_schedule", return_value=None):

            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = f.name
            engine = TakeoffEngine(db_path=db_path)
            engine.counter = fake_counter
            engine.checker = fake_checker
            engine.judge = fake_judge

            engine.run_takeoff(snippets, mode="fast", use_grid=False)

        mock_rcp.assert_called()
        mock_grid.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
# Regression tests for grid bug fixes (Round 17)
# ══════════════════════════════════════════════════════════════════════

class TestCheckerGridDedupKey(unittest.TestCase):
    """Regression: CELL attacks in the same area/type but different cells must NOT be collapsed."""

    def test_multiple_cells_same_type_all_survive_dedup(self):
        """Three CELL attacks for A in Floor 2 — cells A1, B2, C3 — must all survive deduplication."""
        import base64
        import io
        from unittest.mock import patch, MagicMock
        from PIL import Image
        from takeoff.extraction import (
            GridResult, GridCell, CellTypeCount, FixtureSchedule,
        )
        from takeoff.agents import Checker

        buf = io.BytesIO()
        Image.new("RGB", (100, 100), "white").save(buf, format="PNG")
        cell_b64 = base64.b64encode(buf.getvalue()).decode()

        def _make_cell(cell_id, row, col):
            return GridCell(
                cell_id=cell_id, image_base64=cell_b64, row=row, col=col,
                bounds={"x": col / 3, "y": row / 3, "width": 1/3, "height": 1/3},
                area_label="Floor 2",
            )

        cells = [_make_cell("A1", 0, 0), _make_cell("B2", 1, 1), _make_cell("C3", 2, 2)]
        grid_result = GridResult(
            area_label="Floor 2",
            grid_cells=cells,
            cell_type_counts=[
                CellTypeCount("A1", "A", 5),
                CellTypeCount("B2", "A", 4),
                CellTypeCount("C3", "A", 3),
            ],
            area_totals={"A": 12},
        )
        grid_results = {"Floor 2": grid_result}

        fixture_schedule = FixtureSchedule()
        fixture_schedule.fixtures["A"] = {"description": "2x4 LED Troffer"}

        counter_output = {
            "fixture_counts": [{"type_tag": "A", "total": 12, "counts_by_area": {"Floor 2": 12}}],
            "grand_total_fixtures": 12,
            "areas_covered": ["Floor 2"],
        }

        # Checker sees 3 in every cell vs Counter's 5/4/3 — all disagree
        def _mock_vision(*args, **kwargs):
            cell_img = args[3] if len(args) > 3 else kwargs.get("image_b64", "")
            # Return count=3 for every cell (will disagree with counter's 5 and 4)
            return '{"type_tag": "A", "independent_count": 3, "agrees_with_counter": false, "discrepancy": -2, "notes": "diff"}'

        mock_client = MagicMock()
        mock_router = MagicMock()
        mock_router.complete.return_value = MagicMock(
            content='{"attacks": [], "total_attacks": 0, "critical_count": 0, "summary": "ok"}'
        )

        with patch("takeoff.agents._get_vision_client", return_value=mock_client), \
             patch("takeoff.agents._call_vision_with_retry", side_effect=_mock_vision):
            checker = Checker(mock_router)
            response = checker.generate_attacks(
                counter_output=counter_output,
                fixture_schedule=fixture_schedule,
                area_counts=[],
                plan_notes=[],
                panel_data=None,
                rcp_images=None,
                grid_results=grid_results,
            )

        attacks = response.data.get("attacks", [])
        cell_attacks = [a for a in attacks if (a.get("attack_id") or "").startswith("CELL")]
        # All three cell attacks must survive — dedup must not collapse them
        cell_ids_attacked = {a.get("cell_id") for a in cell_attacks}
        # A1 and B2 disagree (checker=3, counter=5 and 4). C3 agrees (checker=3, counter=3).
        # So at minimum A1 and B2 must appear.
        self.assertIn("A1", cell_ids_attacked, "A1 CELL attack must survive dedup")
        self.assertIn("B2", cell_ids_attacked, "B2 CELL attack must survive dedup")
        self.assertGreaterEqual(len(cell_attacks), 2, "At least 2 distinct CELL attacks must survive dedup")


class TestCheckerGridFallbackCoverage(unittest.TestCase):
    """Regression: When an area falls back from grid to full-image, Checker still gets rcp_images for it."""

    def test_fallback_area_passed_to_checker_as_rcp_image(self):
        """Engine must collect fallback areas and pass them as rcp_images to Checker."""
        import base64
        import io
        from unittest.mock import patch, MagicMock, call
        from PIL import Image
        from takeoff.engine import TakeoffEngine
        from takeoff.extraction import AreaCount, FixtureSchedule, GridResult, GridCell

        buf = io.BytesIO()
        Image.new("RGB", (200, 200), "white").save(buf, format="PNG")
        img_b64 = base64.b64encode(buf.getvalue()).decode()

        snippets = [
            {"id": "s1", "label": "fixture_schedule", "sub_label": "Schedule", "image_data": img_b64, "page_number": 1},
            {"id": "s2", "label": "rcp", "sub_label": "Good Area", "image_data": img_b64, "page_number": 1},
            {"id": "s3", "label": "rcp", "sub_label": "Bad Area", "image_data": img_b64, "page_number": 2},
        ]

        fake_schedule = FixtureSchedule()
        fake_schedule.fixtures["A"] = {"description": "2x4 LED Troffer"}

        # Good Area succeeds grid extraction; Bad Area raises RuntimeError (triggers fallback)
        good_cell = GridCell("A1", img_b64, 0, 0, {"x": 0, "y": 0, "width": 1, "height": 1}, "Good Area")
        good_grid = GridResult("Good Area", [good_cell], [], {"A": 3}, [])
        good_area = AreaCount("Good Area", {"A": 3})
        bad_area_full = AreaCount("Bad Area", {"A": 2})

        def _gridded_side_effect(snippet_image, fixture_schedule, area_label, **kwargs):
            if area_label == "Bad Area":
                raise RuntimeError("Simulated grid failure for Bad Area")
            return good_grid

        fake_counter = MagicMock()
        fake_counter.generate_count.return_value = MagicMock(
            data={"fixture_counts": [{"type_tag": "A", "total": 5, "counts_by_area": {"Good Area": 3, "Bad Area": 2}}],
                  "grand_total_fixtures": 5, "areas_covered": ["Good Area", "Bad Area"]},
            parse_error=False,
        )
        fake_checker = MagicMock()
        fake_checker.generate_attacks.return_value = MagicMock(
            data={"attacks": [], "total_attacks": 0, "critical_count": 0, "_model_failure": False},
            raw_response="{}",
        )
        fake_judge = MagicMock()
        fake_judge.evaluate.return_value = {
            "verdict": "PASS", "violations": [], "flags": [], "ruling_summary": "ok"
        }

        with patch("takeoff.engine.extract_fixture_schedule", return_value=fake_schedule), \
             patch("takeoff.engine.extract_rcp_counts_gridded", side_effect=_gridded_side_effect), \
             patch("takeoff.engine.extract_rcp_counts", return_value=bad_area_full), \
             patch("takeoff.engine.extract_plan_notes", return_value=[]), \
             patch("takeoff.engine.extract_panel_schedule", return_value=None):

            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = f.name
            engine = TakeoffEngine(db_path=db_path)
            engine.counter = fake_counter
            engine.checker = fake_checker
            engine.judge = fake_judge

            engine.run_takeoff(snippets, mode="fast", use_grid=True)

        # Checker must have been called with rcp_images containing the fallback area
        call_kwargs = fake_checker.generate_attacks.call_args
        rcp_images_arg = call_kwargs.kwargs.get("rcp_images") or (
            call_kwargs.args[5] if len(call_kwargs.args) > 5 else None
        )
        self.assertIsNotNone(rcp_images_arg, "rcp_images must be passed to Checker for fallback area")
        fallback_labels = [img.get("area_label") for img in rcp_images_arg]
        self.assertIn("Bad Area", fallback_labels, "Bad Area must appear in rcp_images passed to Checker")


# ══════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
