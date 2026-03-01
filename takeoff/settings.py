"""Takeoff-only configuration — self-contained, no core/ or config/ dependency.

Extracted from the Atlantis config/settings.py with only the takeoff-relevant
settings retained.
"""

import os

# ─── API Configuration ────────────────────────────────────────────────────────

def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key, str(default))
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"Invalid value for env var {key}={raw!r}: must be an integer")


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key, str(default))
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"Invalid value for env var {key}={raw!r}: must be a float")


API_CONFIG = {
    "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
    "max_tokens": _env_int("ANTHROPIC_MAX_TOKENS", 4096),
    "temperature": _env_float("ANTHROPIC_TEMPERATURE", 0.4),
    "rate_limit_seconds": _env_float("RATE_LIMIT_SECONDS", 1.0),
    "rate_limit_enabled": os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true",
    "temperature_research": _env_float("TEMPERATURE_RESEARCH", 0.3),
    "vision_max_workers": _env_int("VISION_MAX_WORKERS", 4),
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
