"""tests/test_api.py — unit tests for takeoff.api (no live engine, no lifespan)."""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import takeoff.api as api_mod
from takeoff.api import _format_for_frontend, app
from fastapi.testclient import TestClient


# ── _format_for_frontend ──────────────────────────────────────────────────────

class TestFormatForFrontend(unittest.TestCase):
    """Direct unit tests for api._format_for_frontend key remapping."""

    def test_remaps_fixture_table_to_fixture_counts(self):
        raw = {"fixture_table": [{"type_tag": "A", "total": 5}]}
        out = _format_for_frontend(raw)
        self.assertEqual(out["fixture_counts"], [{"type_tag": "A", "total": 5}])
        self.assertNotIn("fixture_table", out)

    def test_remaps_confidence_to_confidence_score(self):
        out = _format_for_frontend({"confidence": 0.85})
        self.assertAlmostEqual(out["confidence_score"], 0.85)
        self.assertNotIn("confidence", out)

    def test_remaps_verdict_to_judge_verdict(self):
        out = _format_for_frontend({"verdict": "PASS"})
        self.assertEqual(out["judge_verdict"], "PASS")
        self.assertNotIn("verdict", out)

    def test_remaps_violations_to_constitutional_violations(self):
        v = [{"rule": "ScheduleTrace", "severity": "FATAL"}]
        out = _format_for_frontend({"violations": v})
        self.assertEqual(out["constitutional_violations"], v)
        self.assertNotIn("violations", out)

    def test_confidence_breakdown_passes_through(self):
        bd = {"schedule_match": 0.9, "area_coverage": 0.8}
        out = _format_for_frontend({"confidence_breakdown": bd})
        self.assertEqual(out["confidence_breakdown"], bd)

    def test_empty_input_all_keys_with_defaults(self):
        out = _format_for_frontend({})
        expected_keys = {
            "job_id", "mode", "grand_total", "fixture_counts", "areas_covered",
            "confidence_band", "confidence_score", "confidence_breakdown",
            "confidence_explanation", "judge_verdict", "constitutional_violations",
            "flags", "ruling_summary", "adversarial_log", "agent_counts",
            "latency_ms", "cost_usd",
        }
        self.assertEqual(set(out.keys()), expected_keys)
        self.assertEqual(out["grand_total"], 0)
        self.assertEqual(out["fixture_counts"], [])
        self.assertEqual(out["judge_verdict"], "UNKNOWN")
        self.assertAlmostEqual(out["confidence_score"], 0.0)

    def test_full_roundtrip_preserves_values(self):
        raw = {
            "job_id": "abc123", "mode": "strict", "grand_total": 42,
            "fixture_table": [{"type_tag": "B"}], "areas_covered": ["Zone 1"],
            "confidence_band": "HIGH", "confidence": 0.88,
            "confidence_breakdown": {"schedule_match": 1.0},
            "confidence_explanation": "HIGH confidence",
            "verdict": "WARN", "violations": [{"rule": "R1"}],
            "flags": ["Check X"], "ruling_summary": "Proceed with caution",
            "adversarial_log": [{"attack_id": "a1"}],
            "agent_counts": {"counter_types": 3},
            "latency_ms": 12000, "cost_usd": 0.055,
        }
        out = _format_for_frontend(raw)
        self.assertEqual(out["job_id"], "abc123")
        self.assertEqual(out["mode"], "strict")
        self.assertEqual(out["grand_total"], 42)
        self.assertEqual(out["fixture_counts"], [{"type_tag": "B"}])
        self.assertEqual(out["areas_covered"], ["Zone 1"])
        self.assertEqual(out["confidence_band"], "HIGH")
        self.assertAlmostEqual(out["confidence_score"], 0.88)
        self.assertEqual(out["judge_verdict"], "WARN")
        self.assertEqual(out["constitutional_violations"], [{"rule": "R1"}])
        self.assertEqual(out["flags"], ["Check X"])
        self.assertEqual(out["latency_ms"], 12000)
        self.assertAlmostEqual(out["cost_usd"], 0.055)


# ── /takeoff/health ───────────────────────────────────────────────────────────

class TestHealthEndpoint(unittest.TestCase):
    """GET /takeoff/health — no lifespan, engine set directly on module."""

    def setUp(self):
        # Reset engine to None before each test (lifespan never runs)
        api_mod.engine = None

    def test_healthy_when_engine_and_key_present(self):
        api_mod.engine = object()  # any truthy non-None value
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            client = TestClient(app)
            r = client.get("/takeoff/health")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["status"], "healthy")
        self.assertTrue(data["takeoff_ready"])
        self.assertTrue(data["api_keys_valid"])

    def test_degraded_when_engine_none(self):
        api_mod.engine = None
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            client = TestClient(app)
            r = client.get("/takeoff/health")
        data = r.json()
        self.assertEqual(data["status"], "degraded")
        self.assertFalse(data["takeoff_ready"])
        self.assertTrue(data["api_keys_valid"])

    def test_degraded_when_no_api_key(self):
        api_mod.engine = None
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        env["ANTHROPIC_API_KEY"] = ""
        with patch.dict(os.environ, env, clear=True):
            client = TestClient(app)
            r = client.get("/takeoff/health")
        data = r.json()
        self.assertEqual(data["status"], "degraded")
        self.assertFalse(data["api_keys_valid"])


# ── /takeoff/run validation ───────────────────────────────────────────────────

class TestRunTakeoffValidation(unittest.TestCase):
    """POST /takeoff/run — input validation before any pipeline execution."""

    def setUp(self):
        api_mod.engine = MagicMock()
        self.client = TestClient(app)

    def _snip(self, label="fixture_schedule", sub="", page=1):
        return {
            "id": f"s-{label}-{page}",
            "label": label,
            "sub_label": sub,
            "page_number": page,
            "image_data": "abc",
        }

    def _post(self, snippets, mode="fast"):
        return self.client.post("/takeoff/run", json={"snippets": snippets, "mode": mode})

    def test_no_engine_returns_503(self):
        api_mod.engine = None
        r = self._post([self._snip()])
        self.assertEqual(r.status_code, 503)

    def test_empty_snippets_returns_400(self):
        r = self._post([])
        self.assertEqual(r.status_code, 400)

    def test_invalid_label_returns_422(self):
        r = self._post([self._snip(label="NOT_VALID")])
        self.assertEqual(r.status_code, 422)
        self.assertIn("NOT_VALID", r.json()["detail"])

    def test_no_fixture_schedule_returns_400(self):
        r = self._post([self._snip("rcp", "Zone A")])
        self.assertEqual(r.status_code, 400)
        self.assertIn("fixture_schedule", r.json()["detail"])

    def test_no_rcp_returns_400(self):
        r = self._post([self._snip("fixture_schedule")])
        self.assertEqual(r.status_code, 400)
        self.assertIn("rcp", r.json()["detail"])

    def test_invalid_mode_returns_400(self):
        snippets = [self._snip("fixture_schedule"), self._snip("rcp", "Zone A", 2)]
        r = self.client.post("/takeoff/run", json={"snippets": snippets, "mode": "turbo"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("turbo", r.json()["detail"])

    def test_valid_labels_not_rejected(self):
        """All 6 valid labels pass the label check (not 422)."""
        for label in ["fixture_schedule", "rcp", "panel_schedule",
                      "plan_notes", "detail", "site_plan"]:
            with self.subTest(label=label):
                snippets = [
                    self._snip("fixture_schedule"),
                    self._snip("rcp", "Zone A", 2),
                    self._snip(label, "X", 3),
                ]
                r = self.client.post("/takeoff/run",
                                     json={"snippets": snippets, "mode": "fast"})
                self.assertNotEqual(r.status_code, 422,
                    f"Label '{label}' incorrectly rejected with 422")


if __name__ == "__main__":
    unittest.main()
