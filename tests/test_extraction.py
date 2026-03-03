"""tests/test_extraction.py — unit tests for takeoff.extraction (all vision calls mocked).

Covers:
  - generate_grid() edge cases (auto-reduction, small images, area_label propagation)
  - count_fixture_type_in_cell() parse paths (happy path, non-numeric, negative)
  - _call_vision_with_retry() retry exhaustion and success-on-second-attempt
  - extract_rcp_counts_gridded() schedule_context/type_items consistency (regression #1)
  - extract_rcp_counts_gridded() skips empty-description fixture types

Run:
  python -m pytest tests/test_extraction.py -v
"""

import base64
import io
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── helpers ───────────────────────────────────────────────────────────────────

def _skip_no_pil(test_fn):
    """Decorator: skip test if Pillow is not installed."""
    def wrapper(self, *a, **kw):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not available")
        return test_fn(self, *a, **kw)
    wrapper.__name__ = test_fn.__name__
    return wrapper


def _make_png_b64(width: int = 300, height: int = 300, color=(255, 255, 255)) -> str:
    """Return a base64-encoded PNG of the given dimensions."""
    from PIL import Image
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _make_fixture_schedule(fixtures: dict):
    """Build a minimal FixtureSchedule-like object for tests."""
    fs = MagicMock()
    fs.fixtures = fixtures
    return fs


def _make_cell(width=100, height=100):
    """Build a minimal GridCell for testing."""
    from takeoff.extraction import GridCell
    b64 = _make_png_b64(width, height)
    return GridCell(
        cell_id="A1", image_base64=b64, row=0, col=0,
        bounds={"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
        area_label="TestArea",
    )


# ══════════════════════════════════════════════════════════════════════
# 1. generate_grid() — edge cases
# ══════════════════════════════════════════════════════════════════════

class TestGenerateGridEdgeCases(unittest.TestCase):
    """Edge cases for generate_grid() not covered by the existing TestGenerateGrid."""

    @_skip_no_pil
    def test_area_label_propagated_to_all_cells(self):
        """Each GridCell must carry the area_label passed to generate_grid."""
        from takeoff.extraction import generate_grid
        b64 = _make_png_b64(300, 300)
        cells = generate_grid(b64, "Office Wing", rows=2, cols=2)
        for cell in cells:
            self.assertEqual(cell.area_label, "Office Wing",
                             f"Cell {cell.cell_id} has wrong area_label: {cell.area_label!r}")

    @_skip_no_pil
    def test_1x1_grid_returns_one_cell(self):
        """Requesting 1x1 grid returns exactly one cell covering the whole image."""
        from takeoff.extraction import generate_grid
        b64 = _make_png_b64(100, 100)
        cells = generate_grid(b64, "SingleCell", rows=1, cols=1)
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0].cell_id, "A1")
        self.assertAlmostEqual(cells[0].bounds["width"], 1.0, places=5)
        self.assertAlmostEqual(cells[0].bounds["height"], 1.0, places=5)

    @_skip_no_pil
    def test_very_small_image_reduces_to_1x1(self):
        """An image < 50px in both dimensions should reduce to a 1x1 grid."""
        from takeoff.extraction import generate_grid
        # 40x40 px → 40//3 ≈ 13 < 50 → reduces rows; 40//3 < 50 → reduces cols → 1x1
        b64 = _make_png_b64(40, 40)
        cells = generate_grid(b64, "Tiny", rows=3, cols=3)
        self.assertEqual(len(cells), 1, "40x40 image should reduce to a 1x1 grid")

    @_skip_no_pil
    def test_data_uri_prefix_stripped(self):
        """generate_grid must handle a data: URI prefix correctly."""
        from takeoff.extraction import generate_grid
        raw_b64 = _make_png_b64(150, 150)
        data_uri = f"data:image/png;base64,{raw_b64}"
        cells = generate_grid(data_uri, "DataUriArea", rows=2, cols=2)
        self.assertEqual(len(cells), 4)

    @_skip_no_pil
    def test_bounds_sum_to_unit_square(self):
        """Cell bounds should tile perfectly along each axis."""
        from takeoff.extraction import generate_grid
        b64 = _make_png_b64(300, 300)
        cells = generate_grid(b64, "GridArea", rows=3, cols=3)
        row0_cells = [c for c in cells if c.row == 0]
        total_w = sum(c.bounds["width"] for c in row0_cells)
        self.assertAlmostEqual(total_w, 1.0, places=3)
        col0_cells = [c for c in cells if c.col == 0]
        total_h = sum(c.bounds["height"] for c in col0_cells)
        self.assertAlmostEqual(total_h, 1.0, places=3)


# ══════════════════════════════════════════════════════════════════════
# 2. count_fixture_type_in_cell() — response parsing
# ══════════════════════════════════════════════════════════════════════

class TestCountFixtureTypeInCell(unittest.TestCase):
    """Unit tests for count_fixture_type_in_cell() with mocked vision calls."""

    @_skip_no_pil
    def test_happy_path_returns_correct_count(self):
        """Valid JSON with count=5 returns CellTypeCount.count == 5."""
        from takeoff.extraction import count_fixture_type_in_cell
        cell = _make_cell()
        with patch("takeoff.extraction._call_vision_with_retry",
                   return_value='{"type_tag": "A1", "count": 5, "confidence": "high", "notes": "ok"}'):
            result = count_fixture_type_in_cell(
                MagicMock(), cell, "A1", "Recessed Downlight", "  A1: Recessed Downlight", "3x3"
            )
        self.assertEqual(result.count, 5)
        self.assertEqual(result.type_tag, "A1")
        self.assertEqual(result.cell_id, "A1")
        self.assertNotEqual(result.notes, "EXTRACTION_FAILED")

    @_skip_no_pil
    def test_zero_count_is_valid(self):
        """count=0 is valid — not treated as failure."""
        from takeoff.extraction import count_fixture_type_in_cell
        cell = _make_cell()
        with patch("takeoff.extraction._call_vision_with_retry",
                   return_value='{"type_tag": "B2", "count": 0, "confidence": "high", "notes": "none"}'):
            result = count_fixture_type_in_cell(
                MagicMock(), cell, "B2", "Track Light", "  B2: Track Light", "3x3"
            )
        self.assertEqual(result.count, 0)
        self.assertNotEqual(result.notes, "EXTRACTION_FAILED")

    @_skip_no_pil
    def test_non_numeric_count_defaults_to_zero(self):
        """Non-numeric 'count' in JSON defaults to 0 (not EXTRACTION_FAILED)."""
        from takeoff.extraction import count_fixture_type_in_cell
        cell = _make_cell()
        with patch("takeoff.extraction._call_vision_with_retry",
                   return_value='{"type_tag": "C3", "count": "many", "confidence": "low", "notes": "bad parse"}'):
            result = count_fixture_type_in_cell(
                MagicMock(), cell, "C3", "Panel", "  C3: Panel", "2x2"
            )
        self.assertEqual(result.count, 0)

    @_skip_no_pil
    def test_negative_count_defaults_to_zero(self):
        """Negative 'count' in JSON defaults to 0."""
        from takeoff.extraction import count_fixture_type_in_cell
        cell = _make_cell()
        with patch("takeoff.extraction._call_vision_with_retry",
                   return_value='{"type_tag": "D4", "count": -3, "confidence": "low", "notes": "negative"}'):
            result = count_fixture_type_in_cell(
                MagicMock(), cell, "D4", "Fixture", "  D4: Fixture", "3x3"
            )
        self.assertEqual(result.count, 0)

    @_skip_no_pil
    def test_extraction_failed_on_exception(self):
        """If _call_vision_with_retry raises, result has notes='EXTRACTION_FAILED' and count=0."""
        from takeoff.extraction import count_fixture_type_in_cell
        cell = _make_cell()
        with patch("takeoff.extraction._call_vision_with_retry",
                   side_effect=RuntimeError("vision timeout")):
            result = count_fixture_type_in_cell(
                MagicMock(), cell, "E5", "Fixture", "  E5: Fixture", "3x3"
            )
        self.assertEqual(result.notes, "EXTRACTION_FAILED")
        self.assertEqual(result.count, 0)


# ══════════════════════════════════════════════════════════════════════
# 3. _call_vision_with_retry() — retry logic
# ══════════════════════════════════════════════════════════════════════

class TestCallVisionWithRetry(unittest.TestCase):
    """Unit tests for _call_vision_with_retry() retry behavior."""

    def test_success_on_first_attempt(self):
        """If _call_vision succeeds, returns content immediately (1 call)."""
        from takeoff.extraction import _call_vision_with_retry
        with patch("takeoff.extraction._call_vision", return_value='{"count": 3}') as mock_cv:
            result = _call_vision_with_retry(MagicMock(), "system", "user", "b64abc")
        self.assertEqual(result, '{"count": 3}')
        self.assertEqual(mock_cv.call_count, 1)

    def test_success_on_second_attempt(self):
        """If first _call_vision raises a retriable error, retries and succeeds."""
        from takeoff.extraction import _call_vision_with_retry
        with patch("takeoff.extraction._call_vision",
                   side_effect=[RuntimeError("transient"), "ok"]) as mock_cv, \
             patch("takeoff.extraction.time.sleep"):
            result = _call_vision_with_retry(MagicMock(), "sys", "user", "b64img", max_retries=2)
        self.assertEqual(result, "ok")
        self.assertEqual(mock_cv.call_count, 2)

    def test_raises_last_error_after_retries_exhausted(self):
        """If all retries fail, raises the last exception (not wraps it)."""
        from takeoff.extraction import _call_vision_with_retry
        err = RuntimeError("persistent failure")
        with patch("takeoff.extraction._call_vision", side_effect=err), \
             patch("takeoff.extraction.time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                _call_vision_with_retry(MagicMock(), "sys", "user", "b64img", max_retries=2)
        self.assertIs(ctx.exception, err)

    def test_total_attempts_equals_max_retries_plus_one(self):
        """With max_retries=2, _call_vision is called exactly 3 times total."""
        from takeoff.extraction import _call_vision_with_retry
        with patch("takeoff.extraction._call_vision",
                   side_effect=RuntimeError("fail")) as mock_cv, \
             patch("takeoff.extraction.time.sleep"):
            with self.assertRaises(RuntimeError):
                _call_vision_with_retry(MagicMock(), "sys", "user", "b64img", max_retries=2)
        self.assertEqual(mock_cv.call_count, 3)


# ══════════════════════════════════════════════════════════════════════
# 4. extract_rcp_counts_gridded() — schedule_context/type_items consistency
# ══════════════════════════════════════════════════════════════════════

class TestExtractRcpCountsGriddedScheduleContext(unittest.TestCase):
    """Regression test for Bug #1: schedule_context must match type_items exactly."""

    @_skip_no_pil
    def test_empty_description_excluded_from_both_context_and_tasks(self):
        """Fixture types with empty description must be absent from both
        schedule_context lines AND the task list."""
        from takeoff.extraction import extract_rcp_counts_gridded

        schedule = _make_fixture_schedule({
            "GOOD": {"description": "Recessed Downlight"},
            "EMPTY": {"description": ""},
            "NONE": {"description": None},
        })

        b64 = _make_png_b64(300, 300)
        captured_contexts = []
        captured_tags = []

        def _fake_count(client, cell, type_tag, type_desc, schedule_context, grid_dims):
            from takeoff.extraction import CellTypeCount
            captured_contexts.append(schedule_context)
            captured_tags.append(type_tag)
            return CellTypeCount(cell_id=cell.cell_id, type_tag=type_tag, count=1)

        with patch("takeoff.extraction.count_fixture_type_in_cell", side_effect=_fake_count), \
             patch("takeoff.extraction._get_vision_client", return_value=MagicMock()):
            extract_rcp_counts_gridded(b64, schedule, "TestArea", grid_rows=2, grid_cols=2)

        self.assertIn("GOOD", captured_tags, "GOOD type must be in tasks")
        self.assertNotIn("EMPTY", captured_tags, "EMPTY-desc type must be excluded from tasks")
        self.assertNotIn("NONE", captured_tags, "None-desc type must be excluded from tasks")

        for ctx in captured_contexts:
            self.assertNotIn("EMPTY", ctx, "schedule_context must not list EMPTY type")
            self.assertNotIn("NONE", ctx, "schedule_context must not list NONE type")
            self.assertIn("GOOD", ctx, "schedule_context must still list valid types")

    @_skip_no_pil
    def test_all_empty_descriptions_returns_empty_result(self):
        """If all fixtures have empty descriptions, returns GridResult with no counts."""
        from takeoff.extraction import extract_rcp_counts_gridded

        schedule = _make_fixture_schedule({
            "T1": {"description": ""},
            "T2": {},
        })

        b64 = _make_png_b64(200, 200)
        with patch("takeoff.extraction._get_vision_client", return_value=MagicMock()):
            result = extract_rcp_counts_gridded(b64, schedule, "EmptyArea", grid_rows=2, grid_cols=2)

        self.assertEqual(result.cell_type_counts, [])
        self.assertEqual(result.area_totals, {})
        self.assertTrue(len(result.warnings) > 0)


# ══════════════════════════════════════════════════════════════════════
# 5. New bug-fix regression tests (Bugs 1–5)
# ══════════════════════════════════════════════════════════════════════

class TestBug1RowsCappedAt26(unittest.TestCase):
    """Bug 1: generate_grid must cap rows at 26 to avoid cell_id overflow past 'Z'."""

    @_skip_no_pil
    def test_rows_above_26_capped(self):
        """Requesting rows=30 must be capped to 26; cell IDs must all be valid letters A-Z."""
        from takeoff.extraction import generate_grid
        # Tall enough image so auto-reduction doesn't fire (30 * 50 = 1500px min)
        b64 = _make_png_b64(300, 2000)
        cells = generate_grid(b64, "TallArea", rows=30, cols=1)
        row_letters = [c.cell_id[0] for c in cells]
        for letter in row_letters:
            self.assertTrue(
                "A" <= letter <= "Z",
                f"Cell ID starts with non-letter character: {letter!r}"
            )
        # Must have at most 26 rows
        self.assertLessEqual(len(cells), 26)

    @_skip_no_pil
    def test_rows_exactly_26_not_capped(self):
        """Requesting rows=26 must not be capped — 26 rows is the valid maximum."""
        from takeoff.extraction import generate_grid
        b64 = _make_png_b64(300, 2000)
        cells = generate_grid(b64, "MaxRows", rows=26, cols=1)
        # All 26 rows should be present (image is large enough)
        row_letters = {c.cell_id[0] for c in cells}
        self.assertIn("Z", row_letters, "Row 26 must produce a 'Z' row letter")


class TestBug2DeterministicNoRetry(unittest.TestCase):
    """Bug 2: Deterministic errors (JSONDecodeError, ValueError) must NOT be retried."""

    def test_json_decode_error_raises_immediately_no_sleep(self):
        """json.JSONDecodeError must propagate immediately without sleeping."""
        import json
        from takeoff.extraction import _call_vision_with_retry
        err = json.JSONDecodeError("bad json", "", 0)
        with patch("takeoff.extraction._call_vision", side_effect=err) as mock_cv, \
             patch("takeoff.extraction.time.sleep") as mock_sleep:
            with self.assertRaises(json.JSONDecodeError):
                _call_vision_with_retry(MagicMock(), "sys", "user", "b64img", max_retries=2)
        # Must have been called only once (no retries)
        self.assertEqual(mock_cv.call_count, 1)
        mock_sleep.assert_not_called()

    def test_value_error_raises_immediately_no_sleep(self):
        """ValueError must propagate immediately without sleeping."""
        from takeoff.extraction import _call_vision_with_retry
        err = ValueError("corrupt image")
        with patch("takeoff.extraction._call_vision", side_effect=err) as mock_cv, \
             patch("takeoff.extraction.time.sleep") as mock_sleep:
            with self.assertRaises(ValueError):
                _call_vision_with_retry(MagicMock(), "sys", "user", "b64img", max_retries=2)
        self.assertEqual(mock_cv.call_count, 1)
        mock_sleep.assert_not_called()

    def test_transient_runtime_error_is_retried(self):
        """RuntimeError (transient) must still be retried as before."""
        from takeoff.extraction import _call_vision_with_retry
        with patch("takeoff.extraction._call_vision",
                   side_effect=[RuntimeError("timeout"), "ok"]) as mock_cv, \
             patch("takeoff.extraction.time.sleep"):
            result = _call_vision_with_retry(MagicMock(), "sys", "user", "b64img", max_retries=2)
        self.assertEqual(result, "ok")
        self.assertEqual(mock_cv.call_count, 2)


class TestBug3PilImageClosed(unittest.TestCase):
    """Bug 3: PIL images opened in generate_grid must be closed after use."""

    @_skip_no_pil
    def test_image_close_called(self):
        """The full PIL image must be closed via try/finally in generate_grid.

        The patching strategy wraps the convert() call (called on the result of open())
        so we can intercept the final converted image and track its close() calls.
        """
        from PIL import Image
        from takeoff.extraction import generate_grid
        b64 = _make_png_b64(200, 200)

        close_calls = []
        original_open = Image.open

        def patched_open(fp):
            real_img = original_open(fp)
            original_convert = real_img.convert

            def tracking_convert(mode):
                converted = original_convert(mode)
                original_close = converted.close

                def tracked_close():
                    close_calls.append(True)
                    return original_close()

                converted.close = tracked_close
                return converted

            real_img.convert = tracking_convert
            return real_img

        with patch("takeoff.extraction._PilImage.open", side_effect=patched_open):
            generate_grid(b64, "CloseTest", rows=2, cols=2)

        self.assertGreater(len(close_calls), 0, "img.close() must be called at least once")


class TestBug4NegativeMaxRetries(unittest.TestCase):
    """Bug 4: Negative max_retries must raise ValueError immediately (not TypeError via raise None)."""

    def test_negative_max_retries_raises_value_error(self):
        """max_retries=-1 must raise ValueError before any call is made."""
        from takeoff.extraction import _call_vision_with_retry
        with patch("takeoff.extraction._call_vision") as mock_cv:
            with self.assertRaises(ValueError):
                _call_vision_with_retry(MagicMock(), "sys", "user", "b64img", max_retries=-1)
        mock_cv.assert_not_called()

    def test_zero_max_retries_makes_exactly_one_call(self):
        """max_retries=0 must make exactly one attempt (no retries)."""
        from takeoff.extraction import _call_vision_with_retry
        with patch("takeoff.extraction._call_vision", return_value="result") as mock_cv:
            result = _call_vision_with_retry(MagicMock(), "sys", "user", "b64img", max_retries=0)
        self.assertEqual(result, "result")
        self.assertEqual(mock_cv.call_count, 1)


class TestBug5ColsTwoDigitWarning(unittest.TestCase):
    """Bug 5: generate_grid must log a warning when cols >= 10 (two-digit column IDs)."""

    @_skip_no_pil
    def test_warning_logged_when_cols_ge_10(self):
        """A warning must be emitted when the effective column count is >= 10."""
        from takeoff.extraction import generate_grid
        # Wide enough image to avoid auto-reduction: 10 cols * 50 px = 500px min
        b64 = _make_png_b64(600, 100)
        with self.assertLogs("takeoff.extraction", level="WARNING") as cm:
            generate_grid(b64, "WideArea", rows=1, cols=10)
        warning_text = " ".join(cm.output)
        self.assertIn("10", warning_text, "Warning must mention the column count")

    @_skip_no_pil
    def test_no_warning_when_cols_lt_10(self):
        """No two-digit column warning should be emitted when cols < 10."""
        import logging
        from takeoff.extraction import generate_grid
        b64 = _make_png_b64(300, 100)
        logger_obj = logging.getLogger("takeoff.extraction")
        with patch.object(logger_obj, "warning") as mock_warn:
            generate_grid(b64, "NormalArea", rows=1, cols=9)
        # Check that none of the warning calls mention two-digit columns
        two_digit_warnings = [
            call for call in mock_warn.call_args_list
            if "two-digit" in str(call) or "cols=" in str(call)
        ]
        self.assertEqual(len(two_digit_warnings), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
