"""tests/test_llm.py — unit tests for takeoff.llm.LLMProvider (all API calls mocked)."""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic
from takeoff.llm import LLMProvider, LLMTimeoutException, COST_PER_1K


def _make_response(text="OK", input_tokens=100, output_tokens=50):
    """Build a mock Anthropic API response."""
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    resp.usage.input_tokens = input_tokens
    resp.usage.output_tokens = output_tokens
    return resp


def _provider(cache=True):
    """Build an LLMProvider with a mock client and rate limiting disabled."""
    p = LLMProvider(api_key="sk-fake", cache_enabled=cache)
    p._rate_limit_enabled = False
    p.client = MagicMock()
    return p


class TestLLMProvider(unittest.TestCase):

    # ── Cost calculation ──────────────────────────────────────────────────────

    def test_calculate_cost_known_model(self):
        p = LLMProvider.__new__(LLMProvider)
        # claude-sonnet-4-6: $0.003/1K input + $0.015/1K output
        cost = p._calculate_cost("claude-sonnet-4-6", 1000, 1000)
        self.assertAlmostEqual(cost, 0.018, places=6)

    def test_calculate_cost_unknown_model_uses_fallback(self):
        p = LLMProvider.__new__(LLMProvider)
        # Fallback is {"input": 0.003, "output": 0.015} — same as sonnet
        cost_known = p._calculate_cost("claude-sonnet-4-6", 1000, 1000)
        cost_unknown = p._calculate_cost("nonexistent-model-xyz", 1000, 1000)
        self.assertAlmostEqual(cost_known, cost_unknown, places=6)

    # ── Cache behaviour ───────────────────────────────────────────────────────

    def test_cache_hit_skips_api_call(self):
        p = _provider(cache=True)
        p.client.messages.create.return_value = _make_response("First")
        resp1 = p.complete("sys", "user", model="claude-sonnet-4-6", temperature=0.5)
        resp2 = p.complete("sys", "user", model="claude-sonnet-4-6", temperature=0.5)
        self.assertFalse(resp1.cached)
        self.assertTrue(resp2.cached)
        self.assertEqual(p.client.messages.create.call_count, 1)

    def test_cache_miss_on_different_prompt(self):
        p = _provider(cache=True)
        p.client.messages.create.return_value = _make_response()
        p.complete("sys", "user A", model="claude-sonnet-4-6", temperature=0.5)
        p.complete("sys", "user B", model="claude-sonnet-4-6", temperature=0.5)
        self.assertEqual(p.client.messages.create.call_count, 2)

    # ── No API key ────────────────────────────────────────────────────────────

    def test_no_api_key_client_is_none_and_returns_error(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            p = LLMProvider(api_key="")
        self.assertIsNone(p.client)
        resp = p.complete("sys", "user")
        self.assertIn("No API key", resp.content)

    # ── Retry behaviour ───────────────────────────────────────────────────────

    def test_rate_limit_retries_and_succeeds(self):
        p = _provider(cache=False)
        p.client.messages.create.side_effect = [
            anthropic.RateLimitError(
                message="Rate limited",
                response=MagicMock(status_code=429),
                body=None,
            ),
            _make_response("Success"),
        ]
        with patch("time.sleep"):
            resp = p.complete("sys", "user")
        self.assertEqual(resp.content, "Success")
        self.assertEqual(p.client.messages.create.call_count, 2)

    def test_timeout_exhausted_raises_llm_timeout_exception(self):
        p = _provider(cache=False)
        p.client.messages.create.side_effect = [
            anthropic.APITimeoutError(request=MagicMock())
        ] * 3  # 3 attempts all time out
        with self.assertRaises(LLMTimeoutException):
            p.complete("sys", "user")
        self.assertEqual(p.client.messages.create.call_count, 3)

    def test_api_error_exhausted_returns_error_response(self):
        p = _provider(cache=False)
        p.client.messages.create.side_effect = [
            anthropic.APIError(message="Server error", request=MagicMock(), body=None)
        ] * 3
        with patch("time.sleep"):
            resp = p.complete("sys", "user")
        self.assertIn("[LLM ERROR", resp.content)
        self.assertEqual(p.client.messages.create.call_count, 3)

    # ── Token + cost tracking ─────────────────────────────────────────────────

    def test_successful_call_tracks_cost_and_tokens(self):
        p = _provider(cache=False)
        p.client.messages.create.return_value = _make_response(
            "Done", input_tokens=500, output_tokens=200
        )
        p.complete("sys", "user", model="claude-sonnet-4-6")
        self.assertEqual(p.total_input_tokens, 500)
        self.assertEqual(p.total_output_tokens, 200)
        self.assertGreater(p.total_cost_usd, 0.0)
        self.assertEqual(p.call_count, 1)


if __name__ == "__main__":
    unittest.main()
