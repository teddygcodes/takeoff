# Takeoff

**Adversarial multi-agent lighting takeoff for electrical contractors.**

Takeoff uses a pipeline of four AI agents to count and verify lighting fixtures from construction drawings — acting as a second estimator who independently challenges every number before it reaches a bid.

---

## How It Works

Upload a PDF drawing set, snip regions of interest, and run. Four agents execute in sequence:

1. **Counter** — Aggregates RCP extraction data into a complete fixture count by type and area
2. **Checker** — Independently re-counts each RCP via vision and generates adversarial attacks on the Counter's output
3. **Reconciler** — Addresses each attack (concede / defend / partial) and produces revised counts *(strict/liability mode only)*
4. **Judge** — Evaluates the final count against 6 constitutional hard rules and issues a verdict: PASS, WARN, or BLOCK

Every run produces a confidence score, an adversarial log, and a constitutional ruling — not just a number.

---

## Quick Start

**Requirements:** Python 3.10+, Node.js 20+, an Anthropic API key

```bash
# Clone and install Python deps
git clone https://github.com/teddygcodes/takeoff.git
cd takeoff
pip install anthropic python-dotenv Pillow

# Install frontend deps
npm install

# Configure environment
cp .env.example .env
# Add your ANTHROPIC_API_KEY to .env

# Start the backend (port 8001)
./run_api.sh

# Start the frontend (port 3000)
npm run dev
```

Open [http://localhost:3000/takeoff](http://localhost:3000/takeoff), upload a drawing PDF, snip your regions, and click **Run Takeoff**.

---

## Modes

| Mode | Agents | Latency | Cost | Best For |
|------|--------|---------|------|----------|
| **Fast** | Counter + Checker + Judge | ~40–55s | ~$0.12–0.15 | Quick estimates, simple drawings |
| **Strict** *(default)* | + Reconciler | ~60–80s | ~$0.20–0.30 | Bid submissions, multi-floor projects |
| **Liability** | Same as Strict | ~70–90s | ~$0.20–0.30 | GMP bids, design-build contracts |

---

## Snippet Labels

When snipping regions from the drawing, label each one:

| Label | What to Snip |
|-------|-------------|
| `fixture_schedule` | The fixture type table (required) |
| `rcp` | Each Reflected Ceiling Plan area (required, one per area) |
| `panel_schedule` | Panel/circuit schedule (optional, enables cross-reference) |
| `plan_notes` | General notes and specifications (optional) |
| `detail` | Enlarged detail drawings |
| `site_plan` | Exterior/site lighting plans |

---

## Constitutional Rules

The Judge enforces 6 hard rules on every run:

| # | Rule | Severity |
|---|------|----------|
| 1 | **Schedule Traceability** — Every counted fixture must map to a type tag in the schedule | FATAL |
| 2 | **Complete Coverage** — Every RCP snippet area must be accounted for | FATAL |
| 3 | **No Double-Counting** — Overlapping detail views cannot be counted twice | MAJOR |
| 4 | **Cross-Sheet Consistency** — Fixture wattage must be within 15% of panel load data | MAJOR |
| 5 | **Emergency Fixture Tracking** — Exit signs and emergency units must be separately tracked | MAJOR |
| 6 | **Flag Assumptions** — Ambiguous symbols or guessed quantities must be explicitly flagged | MAJOR |

Violation severity overrides the computed confidence score:
- **FATAL** — score forced ≤ 0.25 and result is blocked
- **MAJOR** — score capped at 0.40
- **MINOR** — score capped at 0.50

---

## Confidence Scoring

Each run produces a score from 0.0–1.0 built from 7 weighted features:

```
schedule_match_rate    0.25   % of fixtures tracing to the schedule
area_coverage          0.20   % of RCP areas accounted for
adversarial_resolved   0.15   % of Checker attacks resolved
constitutional_clean   0.15   no violations = boost
cross_reference_match  0.10   panel schedule alignment
note_compliance        0.10   plan notes addressed
fast_mode_penalty     -0.05   Reconciler skipped in fast mode
```

**Bands:** HIGH (0.85–1.0) · MODERATE (0.65–0.84) · LOW (0.40–0.64) · VERY_LOW (0.0–0.39)

---

## Grid Counting

Each RCP snippet is automatically divided into a 3×3 grid of cells before counting. This has two benefits:
1. Each cell is small enough that vision models can count reliably without confusion from dense symbol clusters
2. The Checker independently re-counts every cell, producing per-cell adversarial attacks (`CELL###`) rather than per-area attacks

**Boundary rule:** A fixture is counted in the cell where its center (or >50% of its symbol) falls. Fixtures at cell edges are never double-counted.

**Fallback:** If grid extraction fails for a given area (e.g., the cell image is too small or corrupt), the system automatically falls back to full-image counting for that area. Grid failure is logged as a warning, not an error.

**Grid config** is included in results (`grid_config` field) showing the actual rows × cols and cell IDs used per area.

---

## CLI Usage

```bash
# Run with a JSON snippet file
python -m takeoff snippets.json --mode strict --verbose

# Save results to database
python -m takeoff snippets.json --save-db --db-path takeoff.db

# Output as JSON
python -m takeoff snippets.json --format json
```

---

## Tech Stack

| Layer | Stack |
|-------|-------|
| Frontend | Next.js 16, React 19, TypeScript, Tailwind CSS 4, PDF.js |
| Backend | Python, FastAPI, SSE streaming |
| AI | Anthropic Claude Sonnet (vision + text) |
| Database | SQLite (WAL mode, thread-safe) |

---

## Project Structure

```
takeoff/
├── app/                    # Next.js pages and API routes
├── components/takeoff/     # React components (DrawingViewer, SnippetTray, ResultsPanel)
├── lib/types.ts            # Shared TypeScript types
├── takeoff/                # Python backend package
│   ├── api.py              # FastAPI server with SSE streaming
│   ├── engine.py           # Main orchestrator
│   ├── agents.py           # Counter, Checker, Reconciler, Judge
│   ├── extraction.py       # Vision model calls
│   ├── constitution.py     # Hard rules and articles
│   ├── confidence.py       # Feature-based scoring
│   └── schema.py           # SQLite persistence
└── tests/                  # 237 unit and integration tests
```

---

## License

MIT
