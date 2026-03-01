from pathlib import Path

from config.settings import DEMO_ELECTRICAL_CONFIG, PRODUCTION_CONFIG
from core.engine import AtlantisEngine


def test_demo_electrical_config_overrides_production():
    assert DEMO_ELECTRICAL_CONFIG["founding_era_target_pairs"] == 2
    assert DEMO_ELECTRICAL_CONFIG["governance_cycles"] == 3
    assert DEMO_ELECTRICAL_CONFIG["initial_token_budget"] == PRODUCTION_CONFIG["initial_token_budget"]


def test_demo_electrical_founding_era_seeds_empirical_pairs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("CONSTITUTION.md").write_text("test constitution", encoding="utf-8")

    engine = AtlantisEngine(config=DEMO_ELECTRICAL_CONFIG, mock=True, demo_electrical=True)
    state_manager = engine._run_founding_era([])

    assert len(state_manager.pairs) == 2
    assert {pair.domain for pair in state_manager.pairs} == {"Lighting_Design", "Electrical_Estimation"}
    assert all(pair.domain_type == "empirical" for pair in state_manager.pairs)
