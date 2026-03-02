# Grid + Fixture-Type Isolation Counting — Spec & Implementation Notes

> **Status: Implemented** (March 2026, Round 16)
> Original prompt: `Atlantis Lighting Takeoff Prompt (2).md`
> This file incorporates all deviations and improvements made during implementation.

---

## Context

Each RCP snippet was previously sent as one image with one prompt: "count all fixtures." This fails on dense drawings because the model tries to track multiple symbol types across a large image simultaneously.

**The fix: grid the snippet into cells, then count one fixture type at a time per cell.** Two axes of simplification — smaller area AND single fixture type — multiply. The engine generates the grid once and both the Counter and Checker use the identical cells.

The user experience does not change. They snip one rectangle per RCP area, same as today.

---

## Architecture

```
User snips "Floor 2 North Wing"
    ↓
Engine generates 3x3 grid → 9 cells (GridCell objects)
    ↓
Grid cells stored — SAME cells used by Counter AND Checker
    ↓
COUNTER PHASE (extraction.py):
  For each cell:
    For each fixture type in schedule:
      Vision call: "How many Type A in this cell?" → count
    Cell total = sum of per-type counts
  Area total = sum of cell totals
    ↓
CHECKER PHASE (agents.py — independent verification):
  For each cell:
    For each fixture type in schedule:
      Vision call: "How many Type A in this cell? Counter claims [N]." → independent count
    Compare per-type per-cell against Counter
    Disagreements → CELL### attacks with cell_id + type_tag
    ↓
Reconciler + Judge proceed as normal on the aggregated counts
```

---

## New Data Structures (`extraction.py`)

```python
@dataclass
class GridCell:
    """A single cell from a gridded RCP snippet."""
    cell_id: str          # "A1", "B2", etc. (row letter + col number)
    image_base64: str     # base64 PNG of this cell
    row: int              # 0-indexed
    col: int              # 0-indexed
    bounds: dict          # {x, y, width, height} as fraction of full image (0.0–1.0)
    area_label: str       # parent area label

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
    cell_type_counts: List[CellTypeCount]   # every (cell, type) pair
    area_totals: Dict[str, int]             # type_tag → total across all cells
    warnings: List[str] = field(default_factory=list)
```

---

## `generate_grid()` (`extraction.py`)

```python
def generate_grid(
    image_base64: str,
    area_label: str,
    rows: int = 3,
    cols: int = 3
) -> List[GridCell]:
```

**Implementation:**
1. Decode base64 PNG → PIL Image
2. Calculate `cell_w = img.width // cols`, `cell_h = img.height // rows`
3. **Minimum cell size guard** *(spec change)*: if `cell_w < 50 or cell_h < 50`, reduce to `rows=2, cols=2`; if still too small, return a single cell (the full image). Original spec had no guard; omitting it produces useless 20×20 cells on small images.
4. For last column/row use `img.width` / `img.height` (not `(c+1)*cell_w`) to capture remainder pixels
5. Cell IDs: `chr(ord('A') + r) + str(c + 1)` → "A1", "B3" etc.
6. Bounds: fractional `{x: c/cols, y: r/rows, width: 1/cols, height: 1/rows}`
7. Return list of `GridCell` objects

**No overlap. Clean cuts.** A fixture on a boundary appears partially in whichever cell it falls into; the per-type isolation prompt handles edge disambiguation.

---

## `count_fixture_type_in_cell()` (`extraction.py`)

```python
def count_fixture_type_in_cell(
    client,
    cell: GridCell,
    type_tag: str,
    type_description: str,
    schedule_context: str,
    grid_dimensions: str
) -> CellTypeCount:
```

**System prompt (all values f-string interpolated before API call):**

```
You are an expert electrical estimator counting fixtures in a section of a Reflected Ceiling Plan.

FIXTURE TO COUNT: {type_tag} — {type_description}
CELL POSITION: {cell.cell_id} in a {grid_dimensions} grid of area "{cell.area_label}"

RULES:
1. Count ONLY fixture type {type_tag}. Ignore all other types completely.
2. Count a fixture if its center point or more than 50% of its symbol is within this cell image.
3. If a fixture symbol appears cut off at the edge and you cannot determine if >50% is in this cell,
   note it but do NOT count it.
4. If you cannot identify a symbol as type {type_tag}, do NOT count it — describe it in notes.
5. Be precise. Count individually. Do not estimate.

FIXTURE SCHEDULE (reference only — do NOT count these types):
{schedule_context}

Respond with ONLY valid JSON:
{"type_tag": "{type_tag}", "count": <integer>, "confidence": "low|medium|high", "notes": "..."}
```

> **Boundary rule change vs. spec:** Original spec said "count if partially visible at the edge." Changed to "count if center or >50% of symbol is in this cell." This prevents the same fixture from being counted in two adjacent cells when its symbol straddles a grid boundary. Electrical fixture symbols (troffers, downlights) are large enough that >50% is determinable.

---

## `extract_rcp_counts_gridded()` (`extraction.py`)

```python
def extract_rcp_counts_gridded(
    snippet_image: str,
    fixture_schedule: FixtureSchedule,
    area_label: str,
    grid_rows: int = 3,
    grid_cols: int = 3
) -> GridResult:
```

**Steps:**
1. Call `generate_grid()` → cells
2. Build `schedule_context` string: all types formatted as `"{tag}: {desc}"`
3. Build task list: `[(cell, type_tag, type_desc) for cell in cells for type_tag, info in schedule]`
4. Skip fixture types with empty/None descriptions
5. Run with `ThreadPoolExecutor(max_workers=min(10, API_CONFIG["vision_max_workers"] * 2))`
   - `vision_max_workers` is configurable via `VISION_MAX_WORKERS` env var (default 4)
6. Failure handling: if `notes == "EXTRACTION_FAILED"` > 30% of tasks → raise `RuntimeError` → engine falls back to full-image extraction
7. Aggregate: `area_totals[type_tag] += ctc.count` across all cells
8. Collect warnings (low-confidence cells, failures)
9. Return `GridResult`

> **Workers change vs. spec:** Original spec hardcoded `max_workers=10`. Changed to `min(10, vision_max_workers * 2)` (default 8) and made configurable via env var to avoid rate limit bursts in production.

---

## `grid_result_to_area_count()` (`extraction.py`)

```python
def grid_result_to_area_count(grid_result: GridResult) -> AreaCount:
    notes = [ctc.notes for ctc in grid_result.cell_type_counts
             if ctc.notes and ctc.notes != "EXTRACTION_FAILED"]
    return AreaCount(
        area_label=grid_result.area_label,
        counts_by_type=grid_result.area_totals,
        notes=notes[:20],
        warnings=grid_result.warnings,
    )
```

`EXTRACTION_FAILED` notes are filtered — they are operational metadata, not content the Counter agent should reason about.

---

## Engine Changes (`engine.py`)

### New `run_takeoff()` Parameters

```python
def run_takeoff(
    self,
    snippets: List[Dict],
    mode: Optional[str] = "strict",
    drawing_name: Optional[str] = None,
    status_callback=None,
    use_grid: bool = True,
    grid_rows: int = 3,
    grid_cols: int = 3,
) -> Dict:
```

### Extraction Phase

> **Parallelism change vs. spec:** Original spec put gridded RCP extraction in the outer parallel pool alongside notes/panel. This creates nested `ThreadPoolExecutor` instances (outer pool × inner 10 workers per area = N×10 threads). For 5 areas: 50 simultaneous vision calls, hitting rate limits hard.

**Actual implementation:**
- Notes + panel extraction run in a **background** `ThreadPoolExecutor` (4 workers)
- RCP areas processed **sequentially** for meaningful progress SSE updates
- Each area runs its own internal parallel cell×type calls
- Background executor shut down with `finally: _bg_ex.shutdown(wait=False)`

```python
grid_results: Dict[str, GridResult] = {}
area_counts: List[AreaCount] = []

if use_grid:
    for area_label, rcp_snippet in rcp_jobs:
        try:
            grid_result = extract_rcp_counts_gridded(...)
            grid_results[area_label] = grid_result
            area_counts.append(grid_result_to_area_count(grid_result))
        except Exception as _ge:
            # fallback to full-image extraction per area
            ac = extract_rcp_counts(img_data, fixture_schedule, area_label)
            area_counts.append(ac)
else:
    # original parallel extraction unchanged
    ...
```

### Checker Call

```python
# Grid mode:
checker_response = self.checker.generate_attacks(
    counter_output, fixture_schedule, area_counts, plan_notes, panel_data,
    rcp_images=None,
    grid_results=grid_results,
)

# Non-grid mode (use_grid=False):
checker_response = self.checker.generate_attacks(
    counter_output, fixture_schedule, area_counts, plan_notes, panel_data,
    rcp_images=_rcp_images,
)
```

### `_build_result()` New Fields

```python
# Per fixture entry — cell_counts aggregated across all grid areas.
# Key format: "AreaLabel::CellID" — "::" separator is unambiguous even when
# area labels contain "/" (e.g. "Floor 2/North"). Cell IDs never contain "::".
fixture_entry["cell_counts"] = {"Floor 2::A1": 3, "Floor 3::A1": 2, ...}  # or None if no grid

# Per adversarial log entry:
log_entry["cell_id"] = attack.get("cell_id")  # None for non-cell attacks

# Top-level result when grid was used — per-area dict (each area may have
# different actual grid size due to auto-reduction on small images):
result["grid_config"] = {
    "Floor 2 North Wing": {"rows": 3, "cols": 3, "cells": ["A1", "A2", ..., "C3"]},
    "Lobby": {"rows": 2, "cols": 2, "cells": ["A1", "A2", "B1", "B2"]},
}
```

---

## Checker Changes (`agents.py`)

### Updated Signature

```python
def generate_attacks(
    self,
    counter_output: dict,
    fixture_schedule: FixtureSchedule,
    area_counts: List[AreaCount],
    plan_notes: Optional[List] = None,
    panel_data: Optional[PanelData] = None,
    rcp_images: Optional[List[Dict]] = None,
    grid_results: Optional[Dict] = None,  # Dict[str, GridResult]
) -> TakeoffResponse:
```

### Grid Verification Path

The new path uses `if grid_results:` (not `elif`) — both grid and full-image phases can run simultaneously when some areas succeeded grid extraction and others fell back to full-image (`rcp_images` contains the fallback areas). Both paths populate `vision_attacks`, which are merged with text-based attacks before deduplication.

> **Round 17 fix:** Originally `elif grid_results:` — changed to `if grid_results:` so both phases run when grid mode has partial fallback areas.

**Inner function `_check_cell_type()` — all prompt values f-string interpolated:**

```python
# schedule_context built once before tasks, used in every cell prompt:
_sched_ctx = "\n".join(
    f"  {t}: {(i.get('description', '') if isinstance(i, dict) else str(i))}"
    for t, i in fixture_schedule.fixtures.items()
)

def _check_cell_type(area_label, cell, type_tag, type_desc, counter_count):
    gr = grid_results[area_label]
    n_rows = max(c.row for c in gr.grid_cells) + 1 if gr.grid_cells else 3
    n_cols = max(c.col for c in gr.grid_cells) + 1 if gr.grid_cells else 3
    grid_dims = f"{n_rows}x{n_cols}"
    system_prompt = (
        f"You are independently verifying a fixture count.\n\n"
        f"CELL: {cell.cell_id} of area \"{area_label}\" ({grid_dims} grid)\n"
        f"FIXTURE TYPE: {type_tag} — {type_desc}\n"
        f"COUNTER'S CLAIM: {counter_count} fixtures of this type in this cell\n\n"
        f"RULES:\n"
        f"1. Count ONLY Type {type_tag}. Ignore all other types.\n"
        f"2. Count if center or >50% of symbol is in this cell.\n"
        f"3. Be independent — do not assume Counter is correct.\n\n"
        f"FIXTURE SCHEDULE (for type disambiguation — do NOT count other types):\n"
        f"{_sched_ctx}\n\n"
        f"Respond ONLY valid JSON:\n"
        f"{{\"type_tag\": \"{type_tag}\", \"independent_count\": <int>, "
        f"\"agrees_with_counter\": true|false, \"discrepancy\": <int>, \"notes\": \"...\"}}"
    )
    ...
```

> **Critical implementation note:** Every `{placeholder}` in the prompt must be an f-string variable, not a literal brace. Raw triple-quoted strings with `{cell.cell_id}` will send the literal text `{cell.cell_id}` to the API and return garbage counts.

### Attack Generation

> **Severity change vs. spec:** Original spec used `abs(discrepancy) >= 3 → critical`. This is wrong for large cells — a 3-fixture miss out of 100 is minor; out of 3 it is critical.

**Actual implementation (percentage-based with floor):**
```python
threshold_critical = max(3, int(counter_count * 0.2) + 1)
severity = "critical" if abs_d >= threshold_critical else "major" if abs_d >= 2 else "minor"
```

**Attack IDs use `CELL###` prefix** (not `ATK-###`) to distinguish grid cell attacks from text-based attacks (`ATK-###`) and full-image vision attacks (`VIS###`).

```python
{
    "attack_id": f"CELL{_cell_atk_n:03d}",
    "severity": severity,
    "category": "cell_count_mismatch",
    "description": f"[GRID CHECK] Cell {cell_id} of area '{area_label}': Counter counted {counter_count} × Type {type_tag}, Checker found {checker_count} (diff: {discrepancy:+d})",
    "affected_type_tag": type_tag,
    "affected_area": area_label,
    "cell_id": cell_id,
    "suggested_correction": f"Revise {type_tag} in cell {cell_id} from {counter_count} to {checker_count}",
    "evidence": notes or "Independent grid cell verification",
}
```

---

## TypeScript Types (`lib/types.ts`)

```typescript
interface FixtureRow {
  // ... existing fields ...
  cell_counts?: Record<string, number> | null;  // NEW — key format: "AreaLabel::CellID"
}

interface AdversarialEntry {
  // ... existing fields ...
  cell_id?: string | null;  // NEW
}

interface TakeoffResult {
  // ... existing fields ...
  // Per-area dict — each area may have a different actual grid size due to auto-reduction.
  // Updated Round 18: was a single object {rows, cols, cells}; now keyed by area label.
  grid_config?: Record<string, {
    rows: number;
    cols: number;
    cells: string[];  // e.g. ["A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3"]
  }> | null;
}
```

---

## Cost Math

For a job with 5 RCP areas, 6 fixture types, 3×3 grid:

- **Per area:** 9 cells × 6 types = 54 vision calls (Counter) + 54 (Checker) = 108 calls
- **Per job:** 108 × 5 areas = 540 vision calls total
- Each cell call: ~500 input tokens + ~50 output tokens (small image, short prompt)
- At Sonnet pricing ($3/M input, $15/M output):
  - Input: 540 × 500 = 270K tokens → $0.81
  - Output: 540 × 50 = 27K tokens → $0.41
  - **Total: ~$1.22 per job** (strict mode, 5 areas, 6 types)

For a contractor bidding a $200K electrical job, $1.22 for a verified fixture count is negligible.

---

## Error Handling

- Single cell×type failure → `CellTypeCount(count=0, notes="EXTRACTION_FAILED")`
- `>30%` failures for an area → `RuntimeError` → engine falls back to `extract_rcp_counts()` for that area
- `generate_grid()` failure (PIL error, image too small) → engine falls back to full-image extraction
- Never crashes the pipeline; all fallbacks log with `logger.warning`

---

## What Was NOT Changed

- **No frontend changes.** Grid is invisible to the user.
- **No schema changes.** Cell counts embedded in result JSON.
- **No constitution changes.** Judge sees aggregated counts.
- **No confidence scoring changes.** Same inputs.
- **Counter agent class does NOT change.** Receives `AreaCount` objects; grid counting is in extraction.
- **Reconciler does NOT change.** Works on Counter output vs Checker attacks.
- **`extract_rcp_counts()` still exists** as fallback. Not deleted.

---

## Tests Added

```
tests/test_takeoff_pipeline.py:
  TestGenerateGrid::test_generate_grid_produces_correct_cells
    — 300×300 image → 9 cells, correct IDs A1-C3, correct 100×100 size, fractional bounds

  TestGenerateGrid::test_generate_grid_small_image_reduces_grid
    — 60×60 image → grid reduces to ≤4 cells (2×2 fallback)

  TestGridResultToAreaCount::test_grid_result_to_area_count_preserves_totals
    — area_totals propagate correctly to AreaCount.counts_by_type

  TestGridResultToAreaCount::test_grid_result_to_area_count_collects_notes
    — EXTRACTION_FAILED filtered out; real notes preserved

  TestCheckerGridPath::test_checker_cell_disagreement_generates_cell_attack
    — mocked vision returns count=3 vs Counter's 5 → CELL001 attack with cell_id="B2"

  TestEngineGridFlag::test_engine_use_grid_false_calls_extract_rcp_counts
    — use_grid=False → extract_rcp_counts called; extract_rcp_counts_gridded NOT called

  TestEngineGridFlag::test_engine_all_areas_fallback_checker_still_runs
    — all areas fail grid → fallback to extract_rcp_counts; grid_config absent from result

  TestCheckerGridDedupKey::test_multiple_cells_same_type_all_survive_dedup
    — CELL attacks for same (type, area) but different cells must NOT collapse (Round 17 regression)

  TestCheckerGridFallbackCoverage::test_fallback_area_passed_to_checker_as_rcp_image
    — partial grid fallback: fallback area passed as rcp_images to Checker (Round 17 regression)
```

**Test results:** 265 Python passed, 1 skipped; 9 frontend passed; `npm run build` clean; `npm run lint` clean (1 pre-existing `no-img-element` warning)
