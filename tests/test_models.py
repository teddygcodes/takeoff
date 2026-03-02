"""Unit tests for takeoff/models.py — ModelRouter and verify_api_key."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from takeoff.models import ModelRouter, TASK_TEMPERATURES
from takeoff.settings import MODEL_ALLOCATION, MODEL_IDS, API_CONFIG


class TestModelRouterInit(unittest.TestCase):
    """ModelRouter initialization and API key handling."""

    def test_init_strips_whitespace_from_api_key(self):
        router = ModelRouter(api_key="  sk-test-key  ")
        self.assertEqual(router.api_key, "sk-test-key")

    def test_init_falls_back_to_env_var(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-from-env"}):
            router = ModelRouter(api_key=None)
        self.assertEqual(router.api_key, "sk-from-env")

    def test_init_empty_when_no_key(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            router = ModelRouter(api_key=None)
        self.assertEqual(router.api_key, "")

    def test_is_mock_is_false(self):
        router = ModelRouter(api_key="sk-test")
        self.assertFalse(router.is_mock)

    def test_initial_stats_are_zero(self):
        router = ModelRouter(api_key="sk-test")
        stats = router.get_stats()
        self.assertEqual(stats["model_router_calls"], 0)
        self.assertAlmostEqual(stats["model_router_cost_usd"], 0.0)


class TestModelRouterTaskRouting(unittest.TestCase):
    """Model and temperature selection by task type."""

    def setUp(self):
        self.router = ModelRouter(api_key="sk-test")

    def test_known_tasks_resolve_to_sonnet(self):
        for task in ("takeoff_counter", "takeoff_checker", "takeoff_reconciler", "takeoff_judge"):
            model = self.router._get_model_for_task(task)
            self.assertEqual(model, MODEL_IDS["sonnet"], f"Expected Sonnet for {task}")

    def test_unknown_task_falls_back_to_sonnet(self):
        model = self.router._get_model_for_task("nonexistent_task")
        self.assertEqual(model, MODEL_IDS["sonnet"])

    def test_test_task_resolves_to_haiku(self):
        model = self.router._get_model_for_task("takeoff_test")
        self.assertEqual(model, MODEL_IDS["haiku"])

    def test_task_temperatures_are_set(self):
        self.assertAlmostEqual(self.router._get_temperature_for_task("takeoff_counter"), 0.3)
        self.assertAlmostEqual(self.router._get_temperature_for_task("takeoff_checker"), 0.5)
        self.assertAlmostEqual(self.router._get_temperature_for_task("takeoff_reconciler"), 0.3)
        self.assertAlmostEqual(self.router._get_temperature_for_task("takeoff_judge"), 0.2)
        self.assertAlmostEqual(self.router._get_temperature_for_task("takeoff_test"), 0.0)

    def test_unknown_task_temperature_falls_back_to_api_config(self):
        temp = self.router._get_temperature_for_task("unknown_task")
        self.assertAlmostEqual(temp, API_CONFIG.get("temperature", 0.4))


class TestModelRouterComplete(unittest.TestCase):
    """complete() routes correctly and tracks stats."""

    def test_complete_increments_call_count_and_cost(self):
        router = ModelRouter(api_key="sk-test")
        mock_response = MagicMock()
        mock_response.cost_usd = 0.005
        mock_response.content = "result"

        with patch.object(router._provider, "complete", return_value=mock_response) as mock_complete:
            response = router.complete(
                task_type="takeoff_counter",
                system_prompt="sys",
                user_prompt="user",
            )

        self.assertEqual(router._total_calls, 1)
        self.assertAlmostEqual(router._total_cost_usd, 0.005)
        self.assertEqual(response, mock_response)

        # Verify correct model and temperature were passed
        call_kwargs = mock_complete.call_args[1]
        self.assertEqual(call_kwargs["model"], MODEL_IDS["sonnet"])
        self.assertAlmostEqual(call_kwargs["temperature"], TASK_TEMPERATURES["takeoff_counter"])

    def test_temperature_override_is_respected(self):
        router = ModelRouter(api_key="sk-test")
        mock_response = MagicMock()
        mock_response.cost_usd = 0.0

        with patch.object(router._provider, "complete", return_value=mock_response) as mock_complete:
            router.complete(
                task_type="takeoff_counter",
                system_prompt="sys",
                user_prompt="user",
                temperature=0.99,
            )

        call_kwargs = mock_complete.call_args[1]
        self.assertAlmostEqual(call_kwargs["temperature"], 0.99)

    def test_get_stats_reflects_multiple_calls(self):
        router = ModelRouter(api_key="sk-test")
        mock_response = MagicMock()
        mock_response.cost_usd = 0.01

        with patch.object(router._provider, "complete", return_value=mock_response):
            router.complete("takeoff_counter", "sys", "user")
            router.complete("takeoff_checker", "sys", "user")

        stats = router.get_stats()
        self.assertEqual(stats["model_router_calls"], 2)
        self.assertAlmostEqual(stats["model_router_cost_usd"], 0.02)


if __name__ == "__main__":
    unittest.main()
