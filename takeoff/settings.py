"""Takeoff-only configuration — self-contained, no core/ or config/ dependency.

Extracted from the Atlantis config/settings.py with only the takeoff-relevant
settings retained.
"""

import os

# ─── API Configuration ────────────────────────────────────────────────────────

API_CONFIG = {
    "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
    "max_tokens": int(os.getenv("ANTHROPIC_MAX_TOKENS", "4096")),
    "temperature": float(os.getenv("ANTHROPIC_TEMPERATURE", "0.4")),
    "rate_limit_seconds": float(os.getenv("RATE_LIMIT_SECONDS", "1.0")),
    "rate_limit_enabled": os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true",
    "temperature_research": float(os.getenv("TEMPERATURE_RESEARCH", "0.3")),
}

# ─── Model IDs ────────────────────────────────────────────────────────────────

MODEL_IDS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-sonnet-4-6",  # Opus placeholder — use Sonnet until Opus available
}

# ─── Model Allocation for Takeoff Tasks ───────────────────────────────────────

MODEL_ALLOCATION = {
    "takeoff_counter": "sonnet",
    "takeoff_checker": "sonnet",
    "takeoff_reconciler": "sonnet",
    "takeoff_judge": "sonnet",
    "takeoff_test": "haiku",
}
