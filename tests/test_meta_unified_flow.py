import json
from pathlib import Path

from meta.apply import process_proposal_file
from meta.optimizer import MetaOptimizer


def test_rank_failures_respects_max_proposals(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "domain_health.json").write_text(json.dumps({}), encoding="utf-8")

    archive = {
        "archive_entries": [
            {"source_state": "energy_alpha", "reason_tags": ["LOGIC_FAILURE"]},
            {"source_state": "energy_beta", "reason_tags": ["EVIDENCE_INSUFFICIENT"]},
            {"source_state": "health_alpha", "reason_tags": ["PARAMETER_UNJUSTIFIED"]},
        ],
        "domain_metrics": [{"domain": "energy", "survival_rate": 0.5}, {"domain": "health", "survival_rate": 0.5}],
    }

    optimizer = MetaOptimizer(llm_mode="dry-run")
    ranked = optimizer._rank_failures(archive, run_dir, max_proposals=2)

    assert len(ranked) == 2


def test_process_proposal_file_dry_run_shows_without_applying(tmp_path: Path, monkeypatch):
    states_path = tmp_path / "states.py"
    states_path.write_text(
        """
# === RESEARCHER_PROMPT_START ===
RESEARCH TYPE: baseline\nHYPOTHESIS: baseline\nCONCLUSION: baseline\nCITATIONS: baseline
# === RESEARCHER_PROMPT_END ===

# === CRITIC_PROMPT_START ===
challenge flaw evidence
# === CRITIC_PROMPT_END ===
""".strip()
        + "\n",
        encoding="utf-8",
    )

    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(
        json.dumps(
            {
                "prompt_version": "v2.4.0",
                "proposals": [
                    {
                        "proposal_id": "p1",
                        "status": "APPROVE",
                        "marker": "RESEARCHER_PROMPT",
                        "new_text": "RESEARCH TYPE: changed\\nHYPOTHESIS: changed\\nCONCLUSION: changed\\nCITATIONS: changed",
                        "rollback_text": "RESEARCH TYPE: baseline\\nHYPOTHESIS: baseline\\nCONCLUSION: baseline\\nCITATIONS: baseline",
                        "adversarial_review_summary": "ok",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    import meta.apply as apply_mod

    monkeypatch.setattr(apply_mod, "STATES_PATH", states_path)
    monkeypatch.setattr(apply_mod, "HISTORY_PATH", tmp_path / "history.json")
    monkeypatch.setattr(apply_mod, "RUNS_DIR", tmp_path / "runs")

    before = states_path.read_text(encoding="utf-8")
    result = process_proposal_file(proposal_path, dry_run=True)
    after = states_path.read_text(encoding="utf-8")

    assert result["accepted"] == 0
    assert result["version_changed"] is False
    assert before == after
    assert not (tmp_path / "history.json").exists()
