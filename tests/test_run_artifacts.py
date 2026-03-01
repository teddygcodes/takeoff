import json
from pathlib import Path

from config.settings import MOCK_CONFIG
from core.engine import AtlantisEngine


def test_save_run_artifacts_copies_required_outputs_and_links_latest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("CONSTITUTION.md").write_text("test constitution", encoding="utf-8")

    engine = AtlantisEngine(config=MOCK_CONFIG, mock=True)

    # Seed representative outputs generated during a run.
    (engine.output_dir / "archive.md").write_text("archive", encoding="utf-8")
    (engine.output_dir / "archive.json").write_text("[]", encoding="utf-8")
    (engine.output_dir / "domain_health.json").write_text("{}", encoding="utf-8")
    (engine.output_dir / "content" / "blog" / "post.md").write_text("blog", encoding="utf-8")
    (engine.output_dir / "content" / "newsroom" / "news.md").write_text("news", encoding="utf-8")
    (engine.output_dir / "content" / "debate" / "debate.md").write_text("debate", encoding="utf-8")
    (engine.output_dir / "content" / "explorer" / "map.md").write_text("map", encoding="utf-8")
    (engine.output_dir / "logs" / "cycle_1.md").write_text("log", encoding="utf-8")

    engine._save_run_artifacts()

    run_dir = engine.run_output_dir
    assert (run_dir / "archive.md").exists()
    assert (run_dir / "archive.json").exists()
    assert (run_dir / "domain_health.json").exists()
    assert (run_dir / "content" / "blog" / "post.md").exists()
    assert (run_dir / "content" / "newsroom" / "news.md").exists()
    assert (run_dir / "content" / "debate" / "debate.md").exists()
    assert (run_dir / "content" / "explorer" / "map.md").exists()
    assert (run_dir / "logs" / "cycle_1.md").exists()

    cost_summary = json.loads((run_dir / "cost_summary.json").read_text(encoding="utf-8"))
    run_config = json.loads((run_dir / "run_config.json").read_text(encoding="utf-8"))
    assert "model_router_cost_usd" in cost_summary
    assert run_config["mock"] is True
    assert run_config["config"] == MOCK_CONFIG

    latest_output = Path("output")
    assert latest_output.is_symlink()
    assert latest_output.resolve() == run_dir.resolve()
