from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import relentless as goal_loop  # noqa: E402
from relentless_backends import make_backend  # noqa: E402


def make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "mlx-workspace"
    workspace.mkdir()
    (workspace / "model.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "model.py"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=workspace,
        check=True,
    )
    return workspace


def make_config(
    tmp_path: Path, *, mode: str = "auto", completion_stage: str = "promote"
) -> dict:
    worker_script = tmp_path / "worker.json"
    config_data = {
        "name": "mlx-speed-test",
        "state_dir": str(tmp_path / "state"),
        "workspace": {
            "path": str(make_workspace(tmp_path)),
            "editable_globs": ["model.py"],
        },
        "constraint_workflow": {"mode": mode, "completion_stage": completion_stage},
        "goal": {
            "title": "Optimize native MLX decode on Apple Silicon",
            "objective": "Improve accepted-token throughput without changing model behavior.",
            "success_criteria": ["Matched end-to-end throughput improves."],
        },
        "milestones": [
            {
                "title": "Identify and improve the constraint",
                "acceptance": "measured result",
            }
        ],
        "worker": {"backend": "fake", "fake_script": str(worker_script)},
        "supervisor": {"enabled": False},
        "loop": {"max_sessions": 1, "sleep_seconds": 0},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data), encoding="utf-8")
    return goal_loop.load_config(config_path)


def constraint_record() -> dict:
    return {
        "schema_version": "mlx-toc-constraint/v1",
        "workload": {
            "model": "Nemotron",
            "phase": "decode",
            "hardware": "M3 Ultra 512GB",
            "os": "macOS 26",
            "runtime": "Python 3.14, MLX source build",
            "precision": "bf16",
            "shape": "batch 1, block 32, 512 accepted tokens",
            "batch": "physical 1",
        },
        "goal": {
            "metric": "accepted emitted tokens per second",
            "unit": "tok/s",
            "direction": "higher",
            "quality_floor": "exact greedy parity and image conditioning",
        },
        "baseline": {"value": 100.0, "sample_count": 3, "evidence": ["baseline.json"]},
        "constraint": {
            "kind": "compute",
            "phase": "vocabulary projection",
            "roofline_regime": "compute",
            "evidence": ["trace.json"],
            "shared_assumptions": ["synchronized decode timer"],
        },
        "actions": {
            "exploit": ["reuse the compiled projection graph"],
            "subordinate": ["keep logging outside the decode timer"],
            "elevate": [],
            "elevate_decision": "not_yet",
        },
        "candidate": {
            "value": 115.0,
            "sample_count": 3,
            "parity": "PASS",
            "evidence": ["candidate.json"],
        },
        "decision": {
            "verdict": "promote",
            "basis": "throughput",
            "noise_assessment": "outside_noise",
            "rationale": "matched runs improve accepted-token throughput",
            "accepted_tradeoffs": [],
        },
        "next_constraint": "draft attention",
    }


def write_worker_outcome(config: dict, *, constraint_update: dict | None) -> None:
    outcome = {
        "schema": "relentless-outcome-v2",
        "mission": "work",
        "summary": "work complete",
        "milestone_id": "M1",
        "milestone_status_proposal": "done",
        "findings": [],
        "open_questions": [],
        "report_path": "reports/session-0001.md",
        "goal_complete": False,
    }
    if constraint_update is not None:
        outcome["constraint_update"] = constraint_update
    Path(config["worker"]["fake_script"]).write_text(
        json.dumps(
            [
                {
                    "text": "done",
                    "ok": True,
                    "session_id": "fake-1",
                    "files": {
                        "{session_dir}/outcome.json": json.dumps(outcome),
                        "{state_dir}/reports/session-0001.md": "# report\n",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )


def test_auto_detection_creates_constraint_state_and_brief(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    assert goal_loop.mlx_constraint_required(config)
    goal_loop.ensure_layout(config)
    state = json.loads(
        goal_loop.constraint_state_path(config).read_text(encoding="utf-8")
    )
    assert state["required"] is True
    goal_state = goal_loop.load_goal_state(config)
    brief = goal_loop.build_brief(
        config, goal_state, 1, "work", goal_state["milestones"][0]
    )
    assert "Mandatory MLX constraint workflow" in brief
    assert "constraint_update" in brief


def test_explicit_off_disables_auto_detection(tmp_path: Path) -> None:
    config = make_config(tmp_path, mode="off")
    assert not goal_loop.mlx_constraint_required(config)
    goal_loop.ensure_layout(config)
    assert not goal_loop.constraint_state_path(config).exists()


def test_mlx_correctness_goal_with_performance_non_goal_does_not_auto_trigger() -> None:
    config = goal_loop.load_config(
        Path(__file__).parents[1] / "config" / "klein-flux2.json"
    )
    assert not goal_loop.mlx_constraint_required(config)


def test_promote_record_passes_and_failed_parity_is_rejected() -> None:
    update = {"stage": "promote", "record": constraint_record()}
    _, failures = goal_loop.validate_constraint_update(update)
    assert failures == []
    update["record"]["candidate"]["parity"] = "FAIL"
    _, failures = goal_loop.validate_constraint_update(update)
    assert "promotion_requires_parity_PASS" in failures


def test_nonperformance_promotion_requires_tradeoff() -> None:
    record = constraint_record()
    record["decision"]["basis"] = "memory"
    record["decision"]["noise_assessment"] = "not_measured"
    record["candidate"]["value"] = record["baseline"]["value"]
    _, failures = goal_loop.validate_constraint_update({"stage": "promote", "record": record})
    assert "nonperformance_promotion_requires_accepted_tradeoffs" in failures
    record["decision"]["accepted_tradeoffs"] = ["throughput unchanged; peak memory falls"]
    _, failures = goal_loop.validate_constraint_update({"stage": "promote", "record": record})
    assert failures == []


def test_completion_requires_configured_constraint_stage(tmp_path: Path) -> None:
    config = make_config(tmp_path, completion_stage="promote")
    goal_loop.ensure_layout(config)
    goal_loop.record_constraint_update(
        config,
        1,
        "work",
        {"constraint_update": {"stage": "identify", "record": constraint_record()}},
    )
    ready, failures = goal_loop.constraint_completion_ready(config)
    assert not ready
    assert failures == ["completion_requires_promote_stage"]
    goal_loop.record_constraint_update(
        config,
        2,
        "work",
        {"constraint_update": {"stage": "promote", "record": constraint_record()}},
    )
    assert goal_loop.constraint_completion_ready(config) == (True, [])


def test_missing_constraint_update_blocks_done_proposal(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    write_worker_outcome(config, constraint_update=None)
    goal_loop.ensure_layout(config)
    record = goal_loop.run_session(config, make_backend(config["worker"]), None, 1)
    assert record["gates"]["mlx_constraint"]["passed"] is False
    assert goal_loop.load_goal_state(config)["milestones"][0]["status"] == "active"


def test_valid_constraint_update_allows_done_proposal(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    write_worker_outcome(
        config, constraint_update={"stage": "promote", "record": constraint_record()}
    )
    goal_loop.ensure_layout(config)
    record = goal_loop.run_session(config, make_backend(config["worker"]), None, 1)
    assert record["gates"]["mlx_constraint"]["passed"] is True
    assert goal_loop.load_goal_state(config)["milestones"][0]["status"] == "done"
