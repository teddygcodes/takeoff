import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meta.apply import (
    _estimate_input_tokens,
    _estimated_cost_impact_per_run,
)


def test_estimate_input_tokens_uses_char_quartering():
    assert _estimate_input_tokens("x" * 40) == 10


def test_estimated_cost_impact_per_run_researcher_uses_sonnet_pricing_and_frequency():
    # 12 tokens/call * 60 calls/run * $3/1M tokens = $0.00216
    assert _estimated_cost_impact_per_run("RESEARCHER_PROMPT", 12) == 0.00216


def test_estimated_cost_impact_per_run_critic_negative_delta_supported():
    # -8 tokens/call * 60 calls/run * $3/1M tokens = -$0.00144
    assert _estimated_cost_impact_per_run("CRITIC_PROMPT", -8) == -0.00144
