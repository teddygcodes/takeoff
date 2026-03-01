"""tests/test_settings.py — unit tests for takeoff.settings env helpers."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from takeoff.settings import _env_int, _env_float


class TestEnvInt(unittest.TestCase):
    """Tests for _env_int: os.getenv(key, str(default)) then int()."""

    def test_returns_default_when_not_set(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_env_int("MISSING_VAR", 99), 99)

    def test_parses_valid_integer(self):
        with patch.dict(os.environ, {"TEST_INT": "42"}):
            self.assertEqual(_env_int("TEST_INT", 0), 42)

    def test_parses_negative_integer(self):
        with patch.dict(os.environ, {"TEST_INT": "-7"}):
            self.assertEqual(_env_int("TEST_INT", 0), -7)

    def test_parses_zero(self):
        with patch.dict(os.environ, {"TEST_INT": "0"}):
            self.assertEqual(_env_int("TEST_INT", 5), 0)

    def test_float_string_raises_runtime_error(self):
        with patch.dict(os.environ, {"TEST_INT": "3.14"}):
            with self.assertRaises(RuntimeError) as ctx:
                _env_int("TEST_INT", 0)
            self.assertIn("TEST_INT", str(ctx.exception))
            self.assertIn("must be an integer", str(ctx.exception))

    def test_word_string_raises_runtime_error(self):
        with patch.dict(os.environ, {"TEST_INT": "banana"}):
            with self.assertRaises(RuntimeError):
                _env_int("TEST_INT", 0)

    def test_empty_string_raises_runtime_error(self):
        # Empty string is not the default — os.getenv returns "" and int("") fails
        with patch.dict(os.environ, {"TEST_INT": ""}):
            with self.assertRaises(RuntimeError):
                _env_int("TEST_INT", 5)


class TestEnvFloat(unittest.TestCase):
    """Tests for _env_float: os.getenv(key, str(default)) then float()."""

    def test_returns_default_when_not_set(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertAlmostEqual(_env_float("MISSING_VAR", 3.14), 3.14)

    def test_parses_valid_float(self):
        with patch.dict(os.environ, {"TEST_FLOAT": "2.718"}):
            self.assertAlmostEqual(_env_float("TEST_FLOAT", 0.0), 2.718)

    def test_parses_integer_string_as_float(self):
        with patch.dict(os.environ, {"TEST_FLOAT": "5"}):
            self.assertAlmostEqual(_env_float("TEST_FLOAT", 0.0), 5.0)

    def test_word_string_raises_runtime_error(self):
        with patch.dict(os.environ, {"TEST_FLOAT": "not-a-float"}):
            with self.assertRaises(RuntimeError) as ctx:
                _env_float("TEST_FLOAT", 0.0)
            self.assertIn("TEST_FLOAT", str(ctx.exception))
            self.assertIn("must be a float", str(ctx.exception))

    def test_empty_string_raises_runtime_error(self):
        with patch.dict(os.environ, {"TEST_FLOAT": ""}):
            with self.assertRaises(RuntimeError):
                _env_float("TEST_FLOAT", 1.0)


if __name__ == "__main__":
    unittest.main()
