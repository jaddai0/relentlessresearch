"""Tests for the v2 goal loop: unit coverage plus a fake-backend end-to-end run."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import relentless as goal_loop  # noqa: E402
from relentless_backends import ClaudeBackend, CodexBackend, SessionRequest, make_backend  # noqa: E402


def make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "module.py").write_text("VALUE = 1\n")
    (workspace / "notes.md").write_text("original\n")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=workspace,
        check=True,
    )
    return workspace


def make_config(tmp_path: Path, workspace: Path, **overrides) -> dict:
    config_data = {
        "name": "test-goal",
        "state_dir": str(tmp_path / "state"),
        "workspace": {"path": str(workspace), "editable_globs": ["src/**/*.py"]},
        "goal": {
            "title": "Test goal",
            "objective": "Prove the loop works.",
            "context": ["fact one"],
            "success_criteria": ["loop finishes"],
            "non_goals": ["world domination"],
        },
        "worker": {"backend": "fake", "fake_script": str(tmp_path / "worker_script.json")},
        "supervisor": {"enabled": False},
        "loop": {"max_sessions": 10, "sleep_seconds": 0},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(config_data.get(key), dict):
            config_data[key].update(value)
        else:
            config_data[key] = value
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data))
    return goal_loop.load_config(config_path)


def write_script(path: Path, entries: list[dict]) -> None:
    path.write_text(json.dumps(entries))


def outcome_entry(mission: str, session: int, **fields) -> dict:
    outcome = {
        "schema": "relentless-outcome-v2",
        "mission": mission,
        "summary": f"session {session} summary",
        "findings": [f"finding from session {session}"],
        "open_questions": [],
        "report_path": f"reports/session-{session:04d}.md",
        "goal_complete": False,
    }
    outcome.update(fields)
    return {
        "text": "done",
        "ok": True,
        "session_id": f"fake-{session}",
        "files": {
            "{session_dir}/outcome.json": json.dumps(outcome),
            "{state_dir}/" + outcome["report_path"]: f"# report {session}\n",
        },
    }


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestConfigAndState:
    def test_defaults_merged(self, tmp_path):
        config = make_config(tmp_path, make_workspace(tmp_path))
        assert config["worker"]["permission_mode"] == "acceptEdits"
        assert config["loop"]["max_sessions"] == 10
        assert config["gates"]["validation_commands"] == []

    def test_goal_state_planning_when_no_milestones(self, tmp_path):
        config = make_config(tmp_path, make_workspace(tmp_path))
        state = goal_loop.initial_goal_state(config)
        assert state["status"] == "planning"
        assert state["workspace_baseline"]

    def test_goal_state_active_with_seed_milestones(self, tmp_path):
        config = make_config(
            tmp_path,
            make_workspace(tmp_path),
            milestones=[{"title": "first", "acceptance": "it exists"}],
        )
        state = goal_loop.initial_goal_state(config)
        assert state["status"] == "active"
        assert state["milestones"][0]["id"] == "M1"
        assert state["milestones"][0]["status"] == "pending"


class TestMissionSelection:
    def base_state(self):
        return {
            "status": "active",
            "needs_replan": False,
            "milestones": [],
            "findings": [],
        }

    def test_planning_when_empty(self):
        assert goal_loop.pick_mission({**self.base_state(), "status": "planning"})[0] == "plan"

    def test_work_picks_active_before_pending(self):
        state = self.base_state()
        state["milestones"] = [
            {"id": "M1", "status": "pending", "title": "a"},
            {"id": "M2", "status": "active", "title": "b"},
        ]
        mission, milestone = goal_loop.pick_mission(state)
        assert mission == "work" and milestone["id"] == "M2"

    def test_synthesis_when_all_terminal(self):
        state = self.base_state()
        state["milestones"] = [
            {"id": "M1", "status": "done", "title": "a"},
            {"id": "M2", "status": "dropped", "title": "b"},
        ]
        assert goal_loop.pick_mission(state)[0] == "synthesize"

    def test_replan_flag_wins(self):
        state = self.base_state()
        state["needs_replan"] = True
        state["milestones"] = [{"id": "M1", "status": "pending", "title": "a"}]
        assert goal_loop.pick_mission(state)[0] == "plan"

    def test_all_blocked_triggers_planning(self):
        state = self.base_state()
        state["milestones"] = [{"id": "M1", "status": "blocked", "title": "a"}]
        assert goal_loop.pick_mission(state)[0] == "plan"

    def test_no_mission_when_complete(self):
        assert goal_loop.pick_mission({**self.base_state(), "status": "complete"})[0] is None


class TestBrief:
    def test_brief_contains_goal_and_pointers(self, tmp_path):
        config = make_config(tmp_path, make_workspace(tmp_path))
        goal_loop.ensure_layout(config)
        state = goal_loop.load_goal_state(config)
        brief = goal_loop.build_brief(config, state, 1, "plan", None)
        assert "Prove the loop works." in brief
        assert "PLANNING mission" in brief
        assert str(goal_loop.notebook_path(config)) in brief
        assert "session-0001" in brief
        assert "world domination" in brief

    def test_work_brief_shows_milestone(self, tmp_path):
        config = make_config(tmp_path, make_workspace(tmp_path))
        goal_loop.ensure_layout(config)
        state = goal_loop.load_goal_state(config)
        milestone = {"id": "M1", "title": "do it", "acceptance": "x passes", "status": "active", "verification_commands": []}
        brief = goal_loop.build_brief(config, state, 2, "work", milestone)
        assert "Active milestone: M1" in brief
        assert "x passes" in brief


class TestGuardrails:
    def test_reverts_disallowed_and_keeps_allowed(self, tmp_path):
        workspace = make_workspace(tmp_path)
        config = make_config(tmp_path, workspace)
        goal_loop.ensure_layout(config)
        sdir = goal_loop.session_dir(config, 1)
        sdir.mkdir(parents=True)
        baseline = goal_loop.guardrail_baseline(config)

        (workspace / "src" / "module.py").write_text("VALUE = 2\n")  # allowed
        (workspace / "notes.md").write_text("tampered\n")  # disallowed tracked
        (workspace / "rogue.txt").write_text("sneaky\n")  # disallowed untracked
        (workspace / "src" / "new_tool.py").write_text("NEW = True\n")  # allowed untracked

        audit = goal_loop.enforce_guardrails(config, sdir, baseline)
        assert (workspace / "src" / "module.py").read_text() == "VALUE = 2\n"
        assert (workspace / "notes.md").read_text() == "original\n"
        assert not (workspace / "rogue.txt").exists()
        assert (sdir / "quarantine" / "rogue.txt").exists()
        assert (workspace / "src" / "new_tool.py").exists()
        assert "notes.md" in audit["reverted"]
        assert "rogue.txt" in audit["quarantined"]

    def test_head_reset_on_agent_commit(self, tmp_path):
        workspace = make_workspace(tmp_path)
        config = make_config(tmp_path, workspace)
        goal_loop.ensure_layout(config)
        sdir = goal_loop.session_dir(config, 1)
        sdir.mkdir(parents=True)
        baseline = goal_loop.guardrail_baseline(config)

        (workspace / "src" / "module.py").write_text("VALUE = 3\n")
        subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "agent commit"],
            cwd=workspace,
            check=True,
        )
        audit = goal_loop.enforce_guardrails(config, sdir, baseline)
        assert audit["head_reset"] is True
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True)
        assert head.stdout.strip() == baseline["head"]
        assert (workspace / "src" / "module.py").read_text() == "VALUE = 3\n"  # work preserved

    def test_corrupted_hypotheses_restored(self, tmp_path):
        config = make_config(tmp_path, make_workspace(tmp_path))
        goal_loop.ensure_layout(config)
        sdir = goal_loop.session_dir(config, 1)
        sdir.mkdir(parents=True)
        baseline = goal_loop.guardrail_baseline(config)
        goal_loop.hypotheses_path(config).write_text("{not json")
        audit = goal_loop.enforce_guardrails(config, sdir, baseline)
        assert audit["hypotheses_restored"] is True
        assert isinstance(json.loads(goal_loop.hypotheses_path(config).read_text()), dict)


class TestVerdictApplication:
    def test_add_and_update_milestones(self):
        state = {"status": "planning", "needs_replan": True, "milestones": [], "findings": []}
        added = goal_loop.add_proposed_milestones(
            state,
            [{"title": "first", "acceptance": "a"}, {"title": "second", "acceptance": "b"}],
        )
        assert added == ["M1", "M2"]
        assert state["status"] == "active"
        assert state["needs_replan"] is False
        applied = goal_loop.apply_milestone_updates(state, [{"id": "M1", "status": "done", "reason": "verified"}])
        assert applied == ["M1->done"]
        assert state["milestones"][0]["status"] == "done"

    def test_invalid_updates_ignored(self):
        state = {"milestones": [{"id": "M1", "status": "pending", "title": "a"}]}
        applied = goal_loop.apply_milestone_updates(state, [{"id": "M9", "status": "done"}, {"id": "M1", "status": "bogus"}])
        assert applied == []

    def test_milestones_complete_requires_a_done(self):
        assert not goal_loop.milestones_complete({"milestones": []})
        assert not goal_loop.milestones_complete({"milestones": [{"status": "dropped"}]})
        assert goal_loop.milestones_complete({"milestones": [{"status": "done"}, {"status": "dropped"}]})


class TestBackendArgv:
    def test_claude_argv_shape(self):
        request = SessionRequest(
            prompt="brief",
            cwd="/tmp/ws",
            add_dirs=["/tmp/state"],
            system_prompt="sys",
            model="opus",
            resume_id="abc-123",
            allowed_tools=["Bash", "Read"],
            permission_mode="acceptEdits",
        )
        argv = ClaudeBackend().build_argv(request)
        assert argv[:3] == ["claude", "-p", "brief"]
        assert "--output-format" in argv and "json" in argv
        assert argv[argv.index("--model") + 1] == "opus"
        assert argv[argv.index("--resume") + 1] == "abc-123"
        assert argv[argv.index("--add-dir") + 1] == "/tmp/state"
        assert argv[argv.index("--allowedTools") + 1] == "Bash,Read"

    def test_claude_readonly_restricts_tools(self):
        argv = ClaudeBackend().build_argv(SessionRequest(prompt="p", cwd="/tmp", readonly=True))
        assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
        assert "--allowedTools" not in argv

    def test_codex_argv_shape(self, tmp_path):
        request = SessionRequest(
            prompt="brief",
            cwd="/tmp/ws",
            model="gpt-5.5",
            readonly=False,
            session_dir=str(tmp_path),
            resume_id="sess-9",
        )
        argv = CodexBackend().build_argv(request)
        assert argv[:2] == ["codex", "exec"]
        assert argv[argv.index("--sandbox") + 1] == "workspace-write"
        assert argv[argv.index("-C") + 1] == "/tmp/ws"
        assert "resume" in argv and argv[argv.index("resume") + 1] == "sess-9"
        assert argv[-1] == "brief"

    def test_make_backend_rejects_unknown(self):
        with pytest.raises(ValueError):
            make_backend({"backend": "gpt-telepathy"})


class TestOutcomeTolerance:
    def test_missing_outcome_recorded_and_loop_survives(self, tmp_path):
        workspace = make_workspace(tmp_path)
        config = make_config(tmp_path, workspace)
        # Worker session writes no outcome file at all.
        write_script(Path(config["worker"]["fake_script"]), [{"text": "I forgot", "ok": True}])
        goal_loop.ensure_layout(config)
        backend = make_backend(config["worker"])
        record = goal_loop.run_session(config, backend, None, 1)
        assert record["status"] == "continue"
        assert record["outcome_present"] is False
        state = goal_loop.load_goal_state(config)
        assert state["findings"][0]["summary"] == "(no outcome file written)"


# ---------------------------------------------------------------------------
# End-to-end with the fake backend
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_full_campaign(self, tmp_path):
        workspace = make_workspace(tmp_path)
        marker = tmp_path / "verified.marker"
        config = make_config(tmp_path, workspace)

        write_script(
            Path(config["worker"]["fake_script"]),
            [
                # Session 1: planning — proposes two milestones.
                outcome_entry(
                    "plan",
                    1,
                    proposed_milestones=[
                        {
                            "title": "establish baseline",
                            "acceptance": "baseline numbers recorded",
                            "verification_commands": [
                                {"name": "marker", "command": f"test -f {marker}", "timeout_seconds": 10}
                            ],
                        },
                        {"title": "analyze options", "acceptance": "options compared in report"},
                    ],
                ),
                # Session 2: work on M1 — creates the marker, proposes done.
                {
                    **outcome_entry("work", 2, milestone_id="M1", milestone_status_proposal="done"),
                    "files": {
                        **outcome_entry("work", 2, milestone_id="M1", milestone_status_proposal="done")["files"],
                        str(marker): "ok",
                    },
                },
                # Session 3: work on M2 — proposes done (graded, no commands).
                outcome_entry("work", 3, milestone_id="M2", milestone_status_proposal="done"),
                # Session 4: synthesis — writes final report, goal complete.
                {
                    **outcome_entry("synthesize", 4, goal_complete=True),
                    "files": {
                        **outcome_entry("synthesize", 4, goal_complete=True)["files"],
                        "{state_dir}/reports/final_report.md": "# Final report\nIt worked.\n",
                    },
                },
            ],
        )

        rc = goal_loop.run_loop(config)
        assert rc == 0

        state = goal_loop.load_goal_state(config)
        assert state["status"] == "complete"
        assert [m["status"] for m in state["milestones"]] == ["done", "done"]
        assert len(state["findings"]) == 4
        assert (goal_loop.reports_dir(config) / "final_report.md").exists()
        frozen_dirs = list((goal_loop.state_dir(config) / "frozen").iterdir())
        assert len(frozen_dirs) == 1
        assert (frozen_dirs[0] / "final_report.md").exists()
        status = json.loads(goal_loop.status_path(config).read_text())
        assert status["status"] == "complete"

    def test_supervisor_governs_milestones_and_halt(self, tmp_path):
        workspace = make_workspace(tmp_path)
        config = make_config(
            tmp_path,
            workspace,
            supervisor={
                "enabled": True,
                "backend": "fake",
                "fake_script": str(tmp_path / "supervisor_script.json"),
            },
        )

        verdict_approve = {
            "schema": "relentless-verdict-v2",
            "assessment": "plan ok",
            "drift_detected": False,
            "rule_violations": [],
            "milestone_updates": [],
            "approve_proposed_milestones": True,
            "steering_notes": "Focus on the smallest discriminating experiment first.",
            "action": "continue",
            "halt_reason": "",
        }
        verdict_reject_and_halt = {
            "schema": "relentless-verdict-v2",
            "assessment": "worker over-claimed; evidence shares assumptions with the suspect path",
            "drift_detected": True,
            "rule_violations": ["shared-assumption oracle"],
            "milestone_updates": [{"id": "M1", "status": "active", "reason": "not proven"}],
            "approve_proposed_milestones": False,
            "steering_notes": "Do not trust the self-referential check.",
            "action": "halt",
            "halt_reason": "needs human review",
        }
        write_script(
            Path(config["worker"]["fake_script"]),
            [
                outcome_entry("plan", 1, proposed_milestones=[{"title": "only milestone", "acceptance": "a"}]),
                outcome_entry("work", 2, milestone_id="M1", milestone_status_proposal="done"),
            ],
        )
        write_script(
            Path(config["supervisor"]["fake_script"]),
            [
                {"text": json.dumps(verdict_approve), "ok": True},
                {"text": json.dumps(verdict_reject_and_halt), "ok": True},
            ],
        )

        rc = goal_loop.run_loop(config)
        assert rc == 0
        state = goal_loop.load_goal_state(config)
        assert state["status"] == "halted"
        assert state["milestones"][0]["status"] == "active"  # supervisor overrode the worker's "done"
        notes = goal_loop.supervisor_notes_path(config).read_text()
        assert "smallest discriminating experiment" in notes
        assert "HALT: needs human review" in notes

    def test_failed_verification_blocks_done_without_supervisor(self, tmp_path):
        workspace = make_workspace(tmp_path)
        config = make_config(
            tmp_path,
            workspace,
            milestones=[
                {
                    "title": "gated milestone",
                    "acceptance": "marker exists",
                    "verification_commands": [
                        {"name": "marker", "command": f"test -f {tmp_path / 'never-created'}", "timeout_seconds": 10}
                    ],
                }
            ],
            loop={"max_sessions": 1, "sleep_seconds": 0},
        )
        write_script(
            Path(config["worker"]["fake_script"]),
            [outcome_entry("work", 1, milestone_id="M1", milestone_status_proposal="done")],
        )
        rc = goal_loop.run_loop(config)
        assert rc == 2  # hit max_sessions without completing
        state = goal_loop.load_goal_state(config)
        assert state["milestones"][0]["status"] == "active"  # done was rejected
