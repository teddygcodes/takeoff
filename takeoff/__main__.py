"""Takeoff CLI entry point.

Usage:
    python -m takeoff sample_snippets.json --verbose
    python -m takeoff snippets/ --mode strict --format json
    python -m takeoff job.json --mode fast --save-db
"""

import argparse
import json
import sys
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(override=True)

from takeoff.engine import TakeoffEngine
from takeoff.models import verify_api_key


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Takeoff — Adversarial Lighting Takeoff System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m takeoff snippets.json                         # Run with default strict mode
  python -m takeoff snippets.json --mode fast             # Fast mode (no Reconciler)
  python -m takeoff snippets.json --mode strict --verbose # Strict mode with detailed output
  python -m takeoff snippets/ --format json               # Directory of snippets
  python -m takeoff snippets.json --save-db               # Save to database

Snippet JSON format (single file):
  {
    "drawing_name": "Office Building Level 2",
    "snippets": [
      {
        "id": "s1",
        "label": "fixture_schedule",
        "page_number": 1,
        "image_data": "<base64 PNG>",
        "bbox": {"x": 100, "y": 200, "width": 800, "height": 600}
      },
      {
        "id": "s2",
        "label": "rcp",
        "sub_label": "Floor 2 North Wing",
        "page_number": 5,
        "image_data": "<base64 PNG>",
        "bbox": {"x": 0, "y": 0, "width": 1200, "height": 900}
      }
    ]
  }
        """
    )

    parser.add_argument(
        "input",
        type=str,
        help="Path to snippet JSON file or directory with manifest.json"
    )

    parser.add_argument(
        "--mode",
        type=str,
        choices=["fast", "strict", "liability"],
        default="strict",
        help="Takeoff mode (default: strict)"
    )

    parser.add_argument(
        "--format",
        type=str,
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)"
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed agent reasoning and pipeline steps"
    )

    parser.add_argument(
        "--save-db",
        action="store_true",
        help="Save job to persistent database (default: in-memory)"
    )

    parser.add_argument(
        "--db-path",
        type=str,
        default="takeoff.db",
        help="Path to SQLite database (default: takeoff.db)"
    )

    args = parser.parse_args()

    # Verify API key and connectivity using the shared helper
    print("[TAKEOFF] Verifying Anthropic API key...")
    try:
        verify_api_key(os.getenv("ANTHROPIC_API_KEY", ""))
        print("[TAKEOFF] ✓ API key verified\n")
    except Exception as e:
        print(f"[ERROR] API verification failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Load snippet data
    input_path = Path(args.input)

    if input_path.is_dir():
        manifest_path = input_path / "manifest.json"
        if not manifest_path.exists():
            print(f"[ERROR] Directory mode requires a manifest.json in {input_path}", file=sys.stderr)
            sys.exit(1)
        with open(manifest_path) as f:
            job_data = json.load(f)
        # Load image data from files referenced in manifest
        import base64
        for snippet in job_data.get("snippets", []):
            if "image_path" in snippet and not snippet.get("image_data"):
                img_path = input_path / snippet["image_path"]
                if not img_path.exists():
                    print(f"[ERROR] Snippet '{snippet.get('id', '?')}' references missing image file: {img_path}", file=sys.stderr)
                    sys.exit(1)
                with open(img_path, "rb") as img_file:
                    snippet["image_data"] = base64.b64encode(img_file.read()).decode()
    elif input_path.is_file():
        with open(input_path) as f:
            job_data = json.load(f)
    else:
        print(f"[ERROR] Input path not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    snippets = job_data.get("snippets", [])
    drawing_name = job_data.get("drawing_name")

    if not snippets:
        print("[ERROR] No snippets found in input file", file=sys.stderr)
        sys.exit(1)

    print(f"[TAKEOFF] Loaded {len(snippets)} snippets")
    if drawing_name:
        print(f"[TAKEOFF] Drawing: {drawing_name}")
    print(f"[TAKEOFF] Mode: {args.mode.upper()}\n")

    # Initialize engine
    db_path = args.db_path if args.save_db else ":memory:"
    try:
        engine = TakeoffEngine(db_path=db_path)
    except Exception as e:
        print(f"[ERROR] Failed to initialize engine: {e}", file=sys.stderr)
        sys.exit(1)

    # Run takeoff
    try:
        def _cli_status(msg: str):
            if args.verbose:
                print(f"  → {msg}")
            else:
                # Non-verbose: print key milestones only
                if any(kw in msg for kw in ("Extracting", "Running", "Counter", "Checker", "Judge", "Reconciler", "complete")):
                    print(f"  {msg}")

        result = engine.run_takeoff(
            snippets=snippets,
            mode=args.mode,
            drawing_name=drawing_name,
            status_callback=_cli_status
        )
    except Exception as e:
        print(f"[ERROR] Takeoff failed: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)

    # Output
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        _format_text_output(result, verbose=args.verbose)


def _format_text_output(result: dict, verbose: bool = False):
    """Format result as human-readable text."""
    print()
    print("=" * 70)
    print("TAKEOFF — Adversarial Lighting Takeoff System")
    print("=" * 70)
    print()

    if result.get("error"):
        print(f"ERROR: {result.get('message', result['error'])}")
        return

    print(f"Job ID:       {result.get('job_id', 'N/A')}")
    print(f"Mode:         {result.get('mode', 'N/A').upper()}")
    print(f"Grand Total:  {result.get('grand_total', 0)} fixtures")
    print()

    # Fixture count table
    print("FIXTURE COUNT TABLE:")
    print("-" * 70)
    print(f"{'Type':<6} {'Description':<40} {'Total':>6} {'Diff'}")
    print("-" * 70)
    for fc in result.get("fixture_table", []):
        tag = fc.get("type_tag", "?")
        desc = fc.get("description", "")[:38]
        total = fc.get("total", 0)
        diff = fc.get("difficulty", "S")
        delta = fc.get("delta", "0")
        delta_str = f" ({delta})" if delta and delta != "0" else ""
        flags = " ⚠" if fc.get("flags") else ""
        print(f"{tag:<6} {desc:<40} {total:>6}{delta_str}{flags} [{diff}]")
    print("-" * 70)
    print(f"{'TOTAL':<47} {result.get('grand_total', 0):>6}")
    print()

    # Areas
    areas = result.get("areas_covered", [])
    if areas:
        print(f"Areas Counted ({len(areas)}): {', '.join(areas)}")
        print()

    # Confidence
    if result.get("confidence_explanation"):
        print(result["confidence_explanation"])
    else:
        print(f"CONFIDENCE: {result.get('confidence', 0.0):.2f} ({result.get('confidence_band', 'UNKNOWN')})")
    print()

    # Verdict
    verdict = result.get("verdict", "UNKNOWN")
    if verdict == "BLOCK":
        print("⚠️  VERDICT: BLOCKED — constitutional violations prevent approval")
    elif verdict == "WARN":
        print("⚠️  VERDICT: WARNING — approved with noted caveats")
    elif verdict == "PASS":
        print("✓  VERDICT: PASS — takeoff approved")
    print()

    # Violations
    violations = result.get("violations", [])
    if violations:
        print("CONSTITUTIONAL VIOLATIONS:")
        print("-" * 70)
        for v in violations:
            print(f"• [{v.get('severity')}] {v.get('rule')}: {v.get('explanation')}")
        print()

    # Flags
    flags = result.get("flags", [])
    if flags:
        print("FLAGS (items to verify):")
        for f in flags:
            print(f"• {f}")
        print()

    # Adversarial log (verbose or summary)
    adv_log = result.get("adversarial_log", [])
    if adv_log:
        if verbose:
            print("ADVERSARIAL LOG:")
            print("-" * 70)
            for entry in adv_log:
                verdict_str = f" → {entry.get('verdict', 'UNRESOLVED').upper()}" if entry.get("verdict") else " → UNRESOLVED"
                print(f"[{entry.get('attack_id')}] {entry.get('severity', '?').upper()} — {entry.get('category')}{verdict_str}")
                print(f"  {entry.get('description')}")
                if entry.get("resolution"):
                    print(f"  Resolution: {entry.get('resolution')}")
            print()
        else:
            critical = sum(1 for e in adv_log if e.get("severity") == "critical")
            resolved = sum(1 for e in adv_log if e.get("verdict"))
            print(f"Adversarial Review: {len(adv_log)} issues ({critical} critical), {resolved} resolved")
            print()

    # Agent counts
    ac = result.get("agent_counts", {})
    if ac:
        print(f"Pipeline: {ac.get('counter_types', 0)} type tags counted → {ac.get('checker_attacks', 0)} attacks → {ac.get('reconciler_responses', 0)} responses")

    elapsed = result.get("latency_ms", 0)
    print(f"Completed in {elapsed / 1000:.1f}s")
    print()
    print("=" * 70)


if __name__ == "__main__":
    main()
