# Takeoff: Adversarial Lighting & Electrical Takeoff System

Takeoff is an adversarial multi-agent system for electrical contractors to count and verify lighting fixtures from construction drawings. Powered by the Atlantis governance infrastructure. Think of it as "a second estimator who never agrees with the first."

## Features

- **Vision-Based Extraction** - Base64 snippet images sent to Claude Sonnet for OCR + structured extraction
- **4-Agent Pipeline** - Counter, Checker, Reconciler, Judge
- **Interactive Drawing Workspace** - PDF viewer with zoom/pan and rectangle snipping tool
- **Snippet Management** - Label, reorder, and re-snip RCPs, fixture schedules, panel schedules, and plan notes
- **Feature-Based Confidence** - 7 features with explicit weights (not vibes)
- **Constitutional Rules** - 6 hard rules + 5 articles
- **Per-Area Accountability** - Counts broken down by area, not just grand totals
- **Adversarial Verification** - Every count must survive independent Checker challenge before Judge approval
- **SSE Streaming** - Real-time pipeline progress updates to the frontend

## Installation

```bash
# Install Python dependencies
pip install anthropic python-dotenv Pillow

# Set Anthropic API key
export ANTHROPIC_API_KEY="your_api_key"
```

## Usage

### CLI: JSON snippet file

```bash
python -m takeoff snippets.json --mode strict --verbose
```

### CLI: Directory with manifest

```bash
python -m takeoff drawing_snippets/ --mode fast --format json --save-db
```

### CLI: Fast mode

```bash
python -m takeoff job.json --mode fast
```

### JSON Output

```bash
python -m takeoff job.json --format json
```

### Verbose Mode

```bash
python -m takeoff job.json --verbose
```

## Modes

### Fast Mode
- **Agents:** Counter + Checker + Judge (skip Reconciler)
- **Latency:** ~40-55s
- **Cost:** ~$0.06-0.09
- **Use for:** Quick preliminary estimate, simple single-sheet drawings

### Strict Mode (DEFAULT)
- **Agents:** Counter + Checker + Reconciler + Judge
- **Latency:** ~60-80s
- **Cost:** ~$0.12-0.18
- **Use for:** Bid submissions, multi-floor projects, complex fixture schedules

### Liability Mode
- **Agents:** Same as Strict
- **Latency:** ~70-90s
- **Cost:** ~$0.15-0.22
- **Use for:** Large commercial projects, design-build contracts, GMP bids

## Architecture

```
User Uploads PDF Drawing Set
   ↓
Interactive Drawing Viewer (PDF.js canvas)
   ↓
User Snips Regions → Labels as fixture_schedule | rcp | panel_schedule | plan_notes
   ↓
Snippet Tray — thumbnails, labels, delete/re-snip controls
   ↓
"Run Takeoff" → POST /takeoff/run (base64 snippet images + labels)
   ↓
TakeoffEngine (SSE streaming status updates)
   ↓
  ┌────────────────────────────────────────────────────────┐
  │ Extraction Phase (Vision Model — Claude Sonnet)        │
  │  extract_fixture_schedule()  → FixtureSchedule         │
  │  extract_rcp_counts()        → AreaCount × N           │
  │  extract_plan_notes()        → List[PlanNote]          │
  │  extract_panel_schedule()    → PanelData (optional)    │
  └────────────────────────────────────────────────────────┘
   ↓
Counter (Sonnet) → Fixture counts by type tag × area
   ↓
Checker (Sonnet) → Adversarial attacks on the count
   ↓
Reconciler (Sonnet) → Address attacks [skipped in fast mode]
   ↓
Judge (Sonnet) → Constitutional ruling (PASS | WARN | BLOCK)
   ↓
Confidence Calculator → Feature-based score (7 features)
   ↓
Results Panel — fixture table, adversarial log, confidence, export
```

## Constitution

### 6 Hard Rules (Judge enforces)

1. **Schedule Traceability** — Every counted fixture must map to a type tag in the fixture schedule. No phantom fixtures. Severity: FATAL
2. **Complete Coverage** — Every RCP area in the snippet set must be accounted for. No skipped rooms. Severity: FATAL
3. **No Double-Counting** — Fixtures in overlapping detail views cannot be counted twice. Severity: MAJOR
4. **Cross-Sheet Consistency** — If panel schedule data is available, total fixture wattage must be within 15% of panel load calculations. Severity: MAJOR
5. **Emergency Fixture Tracking** — Exit signs, emergency battery units, and emergency-circuit fixtures must be separately tracked. Severity: MAJOR
6. **Flag Assumptions** — Any ambiguous fixture type, unclear symbol, or assumed quantity must be explicitly flagged, not silently guessed. Severity: MAJOR

### 5 Constitutional Articles (Guidelines)

1. **Accuracy Over Speed** — Take the time to count correctly; rushing causes missed fixtures
2. **Per-Area Accountability** — Counts must be broken down by area, not just grand totals
3. **Adversarial Verification** — Every count must survive independent challenge before approval
4. **Accessory Awareness** — Fixtures are not just the luminaire; consider mounting hardware, whips, sensors, battery packs
5. **Revision Awareness** — Note which drawing revision was counted; flag if revision bubbles are visible

## Confidence Features

```python
base_confidence = 0.5

weights = {
    "schedule_match_rate": 0.25,     # % of counted fixtures that trace to schedule
    "area_coverage": 0.20,           # % of visible RCP areas accounted for
    "adversarial_resolved": 0.15,    # % of Checker attacks resolved (conceded or defended)
    "constitutional_clean": 0.15,    # no violations = boost
    "cross_reference_match": 0.10,   # panel schedule alignment
    "note_compliance": 0.10,         # plan notes addressed
    "fast_mode_penalty": -0.05       # fast mode skips Reconciler
}

confidence = clamp(base_confidence + sum(feature * weight), 0.0, 1.0)
```

**Confidence Bands:**
- 0.85–1.0: HIGH
- 0.65–0.84: MODERATE
- 0.40–0.64: LOW
- 0.0–0.39: VERY_LOW

**Hard Overrides:**
- FATAL violation → confidence forced to 0.25
- MAJOR violation → confidence − 0.20 (floor 0.40)
- MINOR violation → confidence − 0.10 (floor 0.50)

## Snippet Labels

| Label | Description | Example |
|-------|-------------|---------|
| `fixture_schedule` | Fixture type table (E-001, E-002) | Type A = 2x4 LED Troffer, 277V |
| `rcp` | Reflected Ceiling Plan area | Floor 2 North Wing |
| `panel_schedule` | Panel/circuit schedule | Panel LP-2A, 42-circuit |
| `plan_notes` | General notes, specifications | "All exit signs on emergency circuit" |
| `detail` | Enlarged detail drawing | Wall sconce mounting detail |
| `site_plan` | Exterior / site lighting | Parking lot pole lights |

## Database Schema

Takeoff stores all job results in `takeoff.db` (SQLite, WAL mode):

- `takeoff_jobs` — job_id, drawing_name, snippet_count, mode, status
- `snippets` — snippet_id, job_id, page_number, label, sub_label, bbox_json, image_path
- `fixture_schedule` — job_id, type_tag, description, manufacturer, voltage, mounting, dimming
- `fixture_counts` — job_id, type_tag, area, count, confidence, difficulty_code, flags
- `adversarial_log` — job_id, agent, attack_id, severity, description, resolution, final_verdict
- `results` — job_id, grand_total, confidence_score, confidence_band, violations_json

## Model Allocation

All models use Sonnet (vision capability required):

- **Extraction (schedule/RCP/notes/panel):** Sonnet — needs vision for OCR
- **Counter:** Sonnet — needs vision for symbol reading
- **Checker:** Sonnet — needs vision for independent verification
- **Reconciler:** Sonnet — needs vision for evidence review
- **Judge:** Sonnet — constitutional ruling

## Cost Targets

- **Fast mode:** ~$0.12-0.15 per takeoff (was <$0.09)
- **Strict mode:** ~$0.20-0.30 per takeoff (was <$0.18)
- **Liability mode:** ~$0.25-0.35 per takeoff (was <$0.22)

*Costs scale with number of snippets. Each RCP snippet triggers ~2-3 vision calls during extraction plus 1 additional vision call during the Checker's independent verification pass.*

## Examples

### Simple Project (Fast Mode)

```bash
$ python -m takeoff small_office.json --mode fast

======================================================================
TAKEOFF - Adversarial Lighting Takeoff
======================================================================

Drawing: small_office.json
Mode: FAST
Snippets: 4 (1 fixture schedule, 3 RCPs)

FIXTURE COUNTS:
----------------------------------------------------------------------
  Type A  2x4 LED Troffer 277V            48  [S] ✓
  Type B  2x2 LED Troffer 277V            12  [S] ✓
  Type C  6" Recessed Downlight           24  [M] ✓
  Type X  LED Exit Sign w/ Battery         8  [S] ✓
  ─────────────────────────────────────────────────────
  TOTAL                                   92

AREAS COVERED: Open Office North, Open Office South, Corridor 1A
CHECKER ATTACKS: 2 attacks (1 major, 1 minor) — Judge reviewed
CONFIDENCE: 0.79 (MODERATE)
- Schedule match: 100%
- Area coverage: 100%
- Adversarial resolved: 50% (Reconciler skipped — fast mode)
- Constitutional violations: none ✓

✓ VERDICT: PASS with warnings

Completed in 38.4s | Cost: $0.0820

======================================================================
```

### Large Commercial Project (Strict Mode)

```bash
$ python -m takeoff hospital_drawings.json --mode strict --verbose

======================================================================
TAKEOFF - Adversarial Lighting Takeoff
======================================================================

Drawing: hospital_drawings.json
Mode: STRICT
Snippets: 18 (1 fixture schedule, 14 RCPs, 2 panel schedules, 1 plan notes)

[COUNTER] Counting 14 RCP areas...
[CHECKER] Found 4 attacks: 2 critical, 1 major, 1 minor
[RECONCILER] Conceded 2, Defended 1, Partial 1
[JUDGE] Constitutional review...

FIXTURE COUNTS (after reconciliation):
----------------------------------------------------------------------
  Type A  2x4 LED Troffer 347V           312  [S] ✓
  Type B  Recessed 6" Downlight          187  [M] ✓
  Type C  Linear LED Pendant             24   [M] ✓
  Type D  Wall Sconce                    44   [S] ✓
  Type E  High Bay 347V                  36   [D] ✓
  Type X  LED Exit Sign w/ Battery       52   [S] ✓
  Type Y  Emergency Bug-Eye              18   [S] ✓
  ─────────────────────────────────────────────────────
  TOTAL                                 673

CONFIDENCE: 0.87 (HIGH)
- Schedule match: 100%
- Area coverage: 92% (2 corridor areas flagged uncertain)
- Adversarial resolved: 88%
- Constitutional violations: none ✓
- Panel load alignment: within 11%

✓ VERDICT: PASS

Completed in 72.1s | Cost: $0.1640

======================================================================
```

## License

Part of Project Atlantis — see main repo for license.
