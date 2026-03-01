"""tests/test_main.py — unit tests for takeoff.__main__._format_text_output."""
import io
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from takeoff.__main__ import _format_text_output


def _capture(result, verbose=False):
    """Run _format_text_output and return the captured stdout as a string."""
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        _format_text_output(result, verbose=verbose)
    return buf.getvalue()


class TestFormatTextOutput(unittest.TestCase):

    def test_error_dict_prints_error_and_message(self):
        out = _capture({"error": True, "message": "Extraction failed"})
        self.assertIn("ERROR", out)
        self.assertIn("Extraction failed", out)

    def test_pass_verdict_shows_pass_and_grand_total(self):
        out = _capture({
            "job_id": "j1", "mode": "fast", "grand_total": 10,
            "fixture_table": [], "areas_covered": [],
            "verdict": "PASS", "violations": [], "flags": [],
            "adversarial_log": [], "agent_counts": {}, "latency_ms": 2000,
        })
        self.assertIn("PASS", out)
        self.assertIn("10 fixtures", out)

    def test_block_verdict_shows_block_and_violation_details(self):
        out = _capture({
            "job_id": "j2", "mode": "strict", "grand_total": 5,
            "fixture_table": [], "areas_covered": [],
            "verdict": "BLOCK",
            "violations": [{
                "severity": "FATAL",
                "rule": "ScheduleTrace",
                "explanation": "Phantom fixture detected",
            }],
            "flags": [], "adversarial_log": [], "agent_counts": {}, "latency_ms": 5000,
        })
        self.assertIn("BLOCK", out)
        self.assertIn("FATAL", out)
        self.assertIn("ScheduleTrace", out)

    def test_warn_verdict_shows_warn_and_flags(self):
        out = _capture({
            "job_id": "j3", "mode": "strict", "grand_total": 0,
            "fixture_table": [], "areas_covered": [],
            "verdict": "WARN", "violations": [],
            "flags": ["Verify area coverage"],
            "adversarial_log": [], "agent_counts": {}, "latency_ms": 1000,
        })
        self.assertIn("WARN", out)
        self.assertIn("Verify area coverage", out)

    def test_verbose_shows_adversarial_log_entries(self):
        out = _capture({
            "job_id": "j5", "mode": "strict", "grand_total": 3,
            "fixture_table": [], "areas_covered": [],
            "verdict": "PASS", "violations": [], "flags": [],
            "adversarial_log": [{
                "attack_id": "atk-001",
                "severity": "critical",
                "category": "double_counting",
                "description": "Counted same area twice",
                "verdict": "resolved",
                "resolution": "Counter confirmed unique areas",
            }],
            "agent_counts": {}, "latency_ms": 4000,
        }, verbose=True)
        self.assertIn("atk-001", out)
        self.assertIn("double_counting", out)
        self.assertIn("Counter confirmed unique areas", out)


if __name__ == "__main__":
    unittest.main()
