#!/usr/bin/env python3
"""RelentlessResearch v2 — goal-driven research loop over real agent sessions.

Outer loop (this file): goal ledger, milestones, harness-owned gates, guardrail
audits, supervisor verdicts, provenance freezing, crash containment.
Inner loop: one full agentic session per iteration in a real harness (Claude
Code headless / Codex exec) via relentless_backends.

The legacy single-completion batch loop lives in relentless_research.py.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from relentless_backends import Backend, SessionRequest, SessionResult, make_backend
from relentless_common import (
    RelentlessResearchError,
    config_environment,
    extract_json_object,
    read_json,
    read_text,
    run_shell,
    utc_now,
    write_json,
    write_text,
)

MILESTONE_STATUSES = {"pending", "active", "done", "blocked", "dropped"}
VERDICT_ACTIONS = {"continue", "steer", "replan", "fresh_session", "halt"}

WORKER_DEFAULTS: dict[str, Any] = {
    "backend": "claude",
    "model": None,
    "permission_mode": "acceptEdits",
    "allowed_tools": [
        "Bash",
        "Read",
        "Edit",
        "Write",
        "Glob",
        "Grep",
        "WebFetch",
        "WebSearch",
        "TodoWrite",
        "Task",
        "NotebookEdit",
    ],
    "session_timeout_seconds": 3600,
    "resume_sessions": True,
    "extra_args": [],
}

SUPERVISOR_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "backend": "claude",
    "model": None,
    "session_timeout_seconds": 900,
    "rulebook": "docs/lessons-learned.md",
    "extra_args": [],
}

LOOP_DEFAULTS: dict[str, Any] = {
    "max_sessions": 40,
    "sleep_seconds": 5,
    "recent_session_rollup": 6,
    "stop_on_complete": True,
    "max_supervisor_notes_chars": 8000,
}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_path(root: Path, value: str | None) -> str | None:
    if not value:
        return value
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (root / path).resolve()
    return str(path)


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve()
    root = repo_root()
    config = json.loads(config_path.read_text(encoding="utf-8"))

    env_files = [value for value in (resolve_path(root, item) for item in config.get("env_files", [])) if value]
    config["env_files"] = env_files

    config["state_dir"] = resolve_path(root, config.get("state_dir", f".relentless-{config.get('name', 'goal')}"))
    workspace = config.setdefault("workspace", {})
    if not workspace.get("path"):
        raise RelentlessResearchError("config.workspace.path is required")
    workspace["path"] = resolve_path(root, workspace["path"])
    workspace.setdefault("editable_globs", [])

    worker = {**WORKER_DEFAULTS, **(config.get("worker") or {})}
    supervisor = {**SUPERVISOR_DEFAULTS, **(config.get("supervisor") or {})}
    loop = {**LOOP_DEFAULTS, **(config.get("loop") or {})}
    config["worker"], config["supervisor"], config["loop"] = worker, supervisor, loop
    for settings in (worker, supervisor):
        if settings.get("fake_script"):
            settings["fake_script"] = resolve_path(root, settings["fake_script"])
    supervisor["rulebook"] = resolve_path(root, supervisor.get("rulebook"))

    prompts = config.setdefault("prompts", {})
    prompts["mission_system"] = resolve_path(root, prompts.get("mission_system", "prompts/mission_system.md"))
    prompts["supervisor_system"] = resolve_path(root, prompts.get("supervisor_system", "prompts/supervisor_system.md"))

    config.setdefault("gates", {})
    config["gates"].setdefault("validation_commands", [])
    config["gates"].setdefault("canary", {"every_sessions": 0, "commands": []})
    config["gates"].setdefault("completion_commands", [])

    config["_meta"] = {"config_path": str(config_path), "repo_root": str(root)}
    return config


# ---------------------------------------------------------------------------
# State layout
# ---------------------------------------------------------------------------

def state_dir(config: dict[str, Any]) -> Path:
    return Path(config["state_dir"])


def workspace_path(config: dict[str, Any]) -> Path:
    return Path(config["workspace"]["path"])


def goal_state_path(config: dict[str, Any]) -> Path:
    return state_dir(config) / "goal_state.json"


def notebook_path(config: dict[str, Any]) -> Path:
    return state_dir(config) / "research_notebook.md"


def hypotheses_path(config: dict[str, Any]) -> Path:
    return state_dir(config) / "hypotheses.json"


def supervisor_notes_path(config: dict[str, Any]) -> Path:
    return state_dir(config) / "supervisor_notes.md"


def agent_session_path(config: dict[str, Any]) -> Path:
    return state_dir(config) / "agent_session.json"


def status_path(config: dict[str, Any]) -> Path:
    return state_dir(config) / "status.json"


def pid_path(config: dict[str, Any]) -> Path:
    return state_dir(config) / "relentless.pid"


def log_path(config: dict[str, Any]) -> Path:
    return state_dir(config) / "relentless.log"


def reports_dir(config: dict[str, Any]) -> Path:
    return state_dir(config) / "reports"


def session_dir(config: dict[str, Any], session: int) -> Path:
    return state_dir(config) / "sessions" / f"session-{session:04d}"


def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def initial_goal_state(config: dict[str, Any]) -> dict[str, Any]:
    goal = config.get("goal", {})
    milestones = []
    for index, item in enumerate(config.get("milestones") or [], start=1):
        milestones.append(normalize_milestone(item, f"M{index}"))
    head = git(["rev-parse", "HEAD"], workspace_path(config))
    return {
        "schema": "relentless-goal-v2",
        "title": goal.get("title", config.get("name", "RelentlessResearch goal")),
        "objective": goal.get("objective", ""),
        "context": list(goal.get("context", [])),
        "success_criteria": list(goal.get("success_criteria", [])),
        "non_goals": list(goal.get("non_goals", [])),
        "status": "active" if milestones else "planning",
        "needs_replan": False,
        "milestones": milestones,
        "open_questions": [],
        "findings": [],
        "workspace_baseline": head.stdout.strip() if head.returncode == 0 else None,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


def normalize_milestone(item: dict[str, Any], default_id: str) -> dict[str, Any]:
    status = str(item.get("status", "pending"))
    if status not in MILESTONE_STATUSES:
        status = "pending"
    return {
        "id": str(item.get("id") or default_id),
        "title": str(item.get("title", "")).strip(),
        "acceptance": str(item.get("acceptance", "")).strip(),
        "verification_commands": list(item.get("verification_commands") or []),
        "status": status,
        "notes": str(item.get("notes", "")),
        "updated_at": utc_now(),
    }


def initial_notebook(config: dict[str, Any]) -> str:
    goal = config.get("goal", {})
    lines = [
        f"# {goal.get('title', config.get('name', 'RelentlessResearch'))} — research notebook",
        "",
        "## Objective",
        str(goal.get("objective", "")).strip(),
        "",
        "## Durable Facts",
    ]
    lines.extend(f"- {fact}" for fact in goal.get("context", []))
    lines.extend(
        [
            "",
            "## Ruled Out / Failed Attempts",
            "- None yet.",
            "",
            "## Latest State",
            "- No sessions have run yet.",
            "",
        ]
    )
    return "\n".join(lines)


def initial_supervisor_notes() -> str:
    return (
        "# Supervisor Notes\n\n"
        "Steering channel for the goal loop. The model supervisor appends a section after\n"
        "each session it reviews; humans may add notes here too. The worker reads the tail\n"
        "of this file in every mission brief and treats it as the freshest guidance.\n"
    )


def ensure_layout(config: dict[str, Any]) -> None:
    for part in ("sessions", "reports", "frozen", "logs"):
        (state_dir(config) / part).mkdir(parents=True, exist_ok=True)
    if not goal_state_path(config).exists():
        write_json(goal_state_path(config), initial_goal_state(config))
    if not notebook_path(config).exists():
        write_text(notebook_path(config), initial_notebook(config))
    if not hypotheses_path(config).exists():
        write_json(
            hypotheses_path(config),
            {"schema": "relentless-hypotheses-v1", "updated_at": utc_now(), "hypotheses": []},
        )
    if not supervisor_notes_path(config).exists():
        write_text(supervisor_notes_path(config), initial_supervisor_notes())


def load_goal_state(config: dict[str, Any]) -> dict[str, Any]:
    return read_json(goal_state_path(config), default=None) or initial_goal_state(config)


def save_goal_state(config: dict[str, Any], goal_state: dict[str, Any]) -> None:
    goal_state["updated_at"] = utc_now()
    write_json(goal_state_path(config), goal_state)


# ---------------------------------------------------------------------------
# Mission selection and briefs
# ---------------------------------------------------------------------------

def pick_mission(goal_state: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    if goal_state.get("status") in ("complete", "halted"):
        return None, None
    if goal_state.get("needs_replan"):
        return "plan", None
    milestones = goal_state.get("milestones", [])
    if not milestones:
        return "plan", None
    for status in ("active", "pending"):
        for milestone in milestones:
            if milestone.get("status") == status:
                return "work", milestone
    if any(m.get("status") == "done" for m in milestones):
        return "synthesize", None
    return "plan", None


def recent_session_rollup(config: dict[str, Any], goal_state: dict[str, Any]) -> str:
    keep = int(config["loop"]["recent_session_rollup"])
    entries = goal_state.get("findings", [])[-keep:]
    if not entries:
        return "- None yet."
    lines = []
    for entry in entries:
        lines.append(
            f"- session {entry.get('session')} ({entry.get('mission')}"
            f"{', ' + str(entry.get('milestone_id')) if entry.get('milestone_id') else ''}): "
            f"{entry.get('summary') or '(no summary)'}"
        )
    return "\n".join(lines)


def supervisor_notes_tail(config: dict[str, Any]) -> str:
    text = read_text(supervisor_notes_path(config)) if supervisor_notes_path(config).exists() else ""
    cap = int(config["loop"]["max_supervisor_notes_chars"])
    if len(text) > cap:
        return "[OLDER NOTES TRUNCATED — read the full file]\n\n" + text[-cap:]
    return text


def render_milestones(goal_state: dict[str, Any]) -> str:
    milestones = goal_state.get("milestones", [])
    if not milestones:
        return "- None defined yet."
    lines = []
    for m in milestones:
        lines.append(f"- {m['id']} [{m['status']}] {m['title']}")
        if m.get("acceptance"):
            lines.append(f"  acceptance: {m['acceptance']}")
    return "\n".join(lines)


MISSION_INSTRUCTIONS = {
    "plan": (
        "This is a PLANNING mission. Explore the workspace and the durable memory, then decompose "
        "the goal into 3-7 milestones. Each milestone needs a title, observable acceptance criteria, "
        "and verification commands only where a genuinely binary check exists. Order them so each "
        "milestone produces evidence the next one builds on. If milestones already exist, your job is "
        "to RE-plan: keep what the evidence supports, drop or reshape what it does not, and say why in "
        "your report. Put the proposal in `proposed_milestones` in your outcome file. Do not start "
        "implementation work in this mission."
    ),
    "work": (
        "This is a WORK mission on the active milestone shown below. Drive it toward its acceptance "
        "criteria. If you complete it, set `milestone_status_proposal` to \"done\". If you hit a wall "
        "that needs re-planning, set it to \"blocked\" and explain in the report. Stay on this "
        "milestone; record out-of-scope discoveries as findings or open questions instead of chasing them."
    ),
    "synthesize": (
        "This is a SYNTHESIS mission — every milestone is done or dropped. Read the session reports, "
        "the notebook, and the goal's success criteria, then write the final report to "
        "`reports/final_report.md` in the state dir: what was established, the evidence behind each "
        "conclusion, what remains open, and recommended next steps. Judge the goal honestly against its "
        "success criteria. Then set `goal_complete` to true in your outcome file."
    ),
}


def build_brief(
    config: dict[str, Any],
    goal_state: dict[str, Any],
    session: int,
    mission: str,
    milestone: dict[str, Any] | None,
) -> str:
    sdir = session_dir(config, session)
    report_path = f"reports/session-{session:04d}.md"
    timeout_minutes = max(1, int(config["worker"]["session_timeout_seconds"]) // 60)
    editable = config["workspace"].get("editable_globs") or []
    lines = [
        f"# RelentlessResearch mission — session {session:04d} ({mission})",
        "",
        f"Time budget: about {timeout_minutes} minutes of wall clock. Leave the final minutes for the notebook, report, and outcome file.",
        "",
        "## Goal",
        f"Title: {goal_state.get('title')}",
        f"Objective: {goal_state.get('objective')}",
        "",
        "Success criteria:",
    ]
    lines.extend(f"- {item}" for item in goal_state.get("success_criteria", []) or ["(none stated)"])
    if goal_state.get("non_goals"):
        lines.append("")
        lines.append("Non-goals:")
        lines.extend(f"- {item}" for item in goal_state["non_goals"])
    if goal_state.get("context"):
        lines.append("")
        lines.append("Durable context:")
        lines.extend(f"- {item}" for item in goal_state["context"])
    lines.extend(
        [
            "",
            "## Mission",
            MISSION_INSTRUCTIONS[mission],
            "",
            "## Milestones",
            render_milestones(goal_state),
        ]
    )
    if milestone is not None:
        lines.extend(
            [
                "",
                f"## Active milestone: {milestone['id']} — {milestone['title']}",
                f"Acceptance: {milestone.get('acceptance') or '(graded by supervisor against the goal)'}",
            ]
        )
        if milestone.get("verification_commands"):
            lines.append("Harness verification commands (run by the harness when you propose done):")
            lines.extend(f"- {item.get('name')}: `{item.get('command')}`" for item in milestone["verification_commands"])
        if milestone.get("notes"):
            lines.append(f"Notes: {milestone['notes']}")
    if goal_state.get("open_questions"):
        lines.append("")
        lines.append("## Open questions")
        lines.extend(f"- {item}" for item in goal_state["open_questions"])
    lines.extend(
        [
            "",
            "## Recent sessions",
            recent_session_rollup(config, goal_state),
            "",
            "## Supervisor notes (freshest steering — follow unless evidence disproves)",
            supervisor_notes_tail(config),
            "",
            "## Durable memory (read these yourself)",
            f"- Notebook: {notebook_path(config)}",
            f"- Hypothesis ledger: {hypotheses_path(config)}",
            f"- Session reports: {reports_dir(config)}",
            f"- Goal state (read-only for you): {goal_state_path(config)}",
            "",
            "## Workspace",
            f"- Path: {workspace_path(config)}",
            f"- Editable globs: {json.dumps(editable) if editable else 'NONE — this is a read/analyze mission, do not edit the workspace'}",
            "- Do not commit, push, or rewrite git history. The harness audits and reverts out-of-scope edits.",
            "- Validation/canary/success gates are run by the harness after your session; do not run or modify them to fake progress.",
            "",
            "## Required outputs",
            f"- Findings report: {state_dir(config) / report_path}",
            f"- Outcome file: {sdir / 'outcome.json'} (schema relentless-outcome-v2, report_path \"{report_path}\")",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

def is_path_editable(config: dict[str, Any], relative: str) -> bool:
    rel = relative.strip().lstrip("/")
    if not rel or rel.startswith("../") or "/../" in rel:
        return False
    for pattern in config["workspace"].get("editable_globs", []):
        # fnmatch has no `**` semantics: its `*` already crosses `/`, but a
        # literal `**/` segment then demands an extra directory level. Test the
        # collapsed variant too so `src/**/*.py` matches `src/module.py`.
        candidates = {pattern, pattern.replace("**/", "")}
        if any(fnmatch.fnmatch(rel, candidate) for candidate in candidates):
            return True
    return False


def guardrail_baseline(config: dict[str, Any]) -> dict[str, Any]:
    head = git(["rev-parse", "HEAD"], workspace_path(config))
    return {
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "hypotheses_backup": read_json(hypotheses_path(config), default=None),
    }


def porcelain_paths(line: str) -> list[str]:
    body = line[3:].strip()
    if " -> " in body:
        old, new = body.split(" -> ", 1)
        return [old.strip().strip('"'), new.strip().strip('"')]
    return [body.strip('"')]


def enforce_guardrails(config: dict[str, Any], sdir: Path, baseline: dict[str, Any]) -> dict[str, Any]:
    repo = workspace_path(config)
    audit: dict[str, Any] = {"head_reset": False, "reverted": [], "quarantined": [], "hypotheses_restored": False}

    head = git(["rev-parse", "HEAD"], repo)
    current_head = head.stdout.strip() if head.returncode == 0 else None
    if baseline.get("head") and current_head and current_head != baseline["head"]:
        git(["reset", "--soft", baseline["head"]], repo)
        audit["head_reset"] = True

    status = git(["status", "--porcelain"], repo)
    quarantine = sdir / "quarantine"
    for line in status.stdout.splitlines():
        if not line.strip():
            continue
        code = line[:2]
        for rel in porcelain_paths(line):
            if is_path_editable(config, rel):
                continue
            absolute = repo / rel
            if code.strip() == "??":
                if absolute.exists():
                    destination = quarantine / rel
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(absolute), str(destination))
                    audit["quarantined"].append(rel)
            else:
                restore = git(["checkout", "HEAD", "--", rel], repo)
                if restore.returncode != 0:
                    git(["rm", "--cached", "-q", "--", rel], repo)
                    if absolute.exists():
                        destination = quarantine / rel
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(absolute), str(destination))
                        audit["quarantined"].append(rel)
                        continue
                audit["reverted"].append(rel)

    try:
        data = read_json(hypotheses_path(config), default=None)
        if not isinstance(data, dict):
            raise ValueError("hypotheses.json is not a JSON object")
    except Exception:
        backup = baseline.get("hypotheses_backup")
        if backup is not None:
            write_json(hypotheses_path(config), backup)
        audit["hypotheses_restored"] = True

    write_json(sdir / "audit.json", audit)
    return audit


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def command_env(config: dict[str, Any]) -> dict[str, str]:
    env = config_environment(config.get("env_files", []))
    env.update({"PYTHONUNBUFFERED": "1"})
    if isinstance(config.get("command_env"), dict):
        env.update({str(key): str(value) for key, value in config["command_env"].items()})
    return env


def run_gate_commands(
    config: dict[str, Any],
    commands: list[dict[str, Any]],
    sdir: Path,
    phase: str,
) -> tuple[list[dict[str, Any]], bool]:
    results = []
    env = command_env(config)
    gates = sdir / "gates"
    for index, item in enumerate(commands, start=1):
        command = str(item.get("command") if isinstance(item, dict) else item)
        name = str(item.get("name") or f"{phase}-{index}") if isinstance(item, dict) else f"{phase}-{index}"
        result = run_shell(
            command,
            cwd=workspace_path(config),
            env=env,
            log_path=gates / f"{phase}-{index:02d}-{name}.log",
            timeout_seconds=(item.get("timeout_seconds") if isinstance(item, dict) else None),
        )
        result["name"] = name
        result["command"] = command
        tail = Path(result["log_path"]).read_text(errors="replace")[-2000:]
        result["tail"] = tail
        results.append(result)
        if result["returncode"] != 0:
            break
    passed = all(int(item.get("returncode", 1)) == 0 for item in results)
    return results, passed


# ---------------------------------------------------------------------------
# Worker and supervisor sessions
# ---------------------------------------------------------------------------

def load_resume_id(config: dict[str, Any]) -> str | None:
    data = read_json(agent_session_path(config), default={}) or {}
    return data.get("worker_session_id")


def store_resume_id(config: dict[str, Any], session_id: str | None) -> None:
    write_json(agent_session_path(config), {"worker_session_id": session_id, "updated_at": utc_now()})


def run_worker_session(
    config: dict[str, Any],
    backend: Backend,
    session: int,
    brief: str,
) -> SessionResult:
    worker = config["worker"]
    sdir = session_dir(config, session)
    resume_id = load_resume_id(config) if worker.get("resume_sessions") else None
    request = SessionRequest(
        prompt=brief,
        cwd=str(workspace_path(config)),
        add_dirs=[str(state_dir(config))],
        system_prompt=read_text(config["prompts"]["mission_system"]),
        model=worker.get("model"),
        timeout_seconds=int(worker["session_timeout_seconds"]),
        resume_id=resume_id,
        readonly=False,
        permission_mode=str(worker["permission_mode"]),
        allowed_tools=list(worker.get("allowed_tools") or []),
        extra_args=list(worker.get("extra_args") or []),
        env=command_env(config),
        session_dir=str(sdir),
        state_dir=str(state_dir(config)),
    )
    result = backend.run(request)
    write_json(
        sdir / "result.json",
        {
            "ok": result.ok,
            "session_id": result.session_id,
            "duration_seconds": result.duration_seconds,
            "error": result.error,
            "resumed_from": resume_id,
            "text_tail": result.text[-4000:],
        },
    )
    if worker.get("resume_sessions"):
        store_resume_id(config, result.session_id if result.ok else None)
    return result


def read_outcome(config: dict[str, Any], session: int) -> dict[str, Any] | None:
    path = session_dir(config, session) / "outcome.json"
    try:
        data = read_json(path, default=None)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def supervisor_user_prompt(
    config: dict[str, Any],
    session: int,
    mission: str,
    milestone: dict[str, Any] | None,
    outcome: dict[str, Any] | None,
    gate_summary: dict[str, Any],
    worker_ok: bool,
    worker_error: str | None,
) -> str:
    sdir = session_dir(config, session)
    lines = [
        f"Review session {session:04d} (mission: {mission}) of this RelentlessResearch campaign.",
        "",
        f"Worker session ok: {worker_ok}" + (f" (error: {worker_error})" if worker_error else ""),
        f"Active milestone: {milestone['id'] if milestone else '(none)'}",
        f"Worker outcome file present: {outcome is not None}",
        "",
        "Harness gate results (authoritative — the worker cannot influence these):",
        json.dumps(gate_summary, indent=2),
        "",
        "Files to read as needed:",
        f"- Rulebook: {config['supervisor']['rulebook']}",
        f"- Mission brief: {sdir / 'brief.md'}",
        f"- Worker outcome: {sdir / 'outcome.json'}",
        f"- Goal state: {goal_state_path(config)}",
        f"- Notebook: {notebook_path(config)}",
        f"- Hypothesis ledger: {hypotheses_path(config)}",
        f"- Supervisor notes so far: {supervisor_notes_path(config)}",
        f"- Session reports dir: {reports_dir(config)}",
        f"- Gate logs: {sdir / 'gates'}",
        "",
        "Return exactly one relentless-verdict-v2 JSON object as your final message.",
    ]
    return "\n".join(lines)


def run_supervisor(
    config: dict[str, Any],
    backend: Backend,
    session: int,
    prompt: str,
) -> dict[str, Any] | None:
    supervisor = config["supervisor"]
    sdir = session_dir(config, session)
    request = SessionRequest(
        prompt=prompt,
        cwd=str(state_dir(config)),
        add_dirs=[str(workspace_path(config)), str(repo_root())],
        system_prompt=read_text(config["prompts"]["supervisor_system"]),
        model=supervisor.get("model"),
        timeout_seconds=int(supervisor["session_timeout_seconds"]),
        readonly=True,
        extra_args=list(supervisor.get("extra_args") or []),
        env=command_env(config),
        session_dir=str(sdir),
        state_dir=str(state_dir(config)),
    )
    result = backend.run(request)
    if not result.ok:
        write_json(sdir / "verdict.json", {"error": result.error or "supervisor session failed"})
        return None
    try:
        verdict = extract_json_object(result.text)
    except RelentlessResearchError:
        write_json(sdir / "verdict.json", {"error": "verdict was not valid JSON", "text_tail": result.text[-4000:]})
        return None
    write_json(sdir / "verdict.json", verdict)
    return verdict


# ---------------------------------------------------------------------------
# Applying outcomes and verdicts
# ---------------------------------------------------------------------------

def next_milestone_id(goal_state: dict[str, Any]) -> int:
    highest = 0
    for milestone in goal_state.get("milestones", []):
        identifier = str(milestone.get("id", ""))
        if identifier.startswith("M"):
            try:
                highest = max(highest, int(identifier[1:]))
            except ValueError:
                continue
    return highest + 1


def add_proposed_milestones(goal_state: dict[str, Any], proposals: list[dict[str, Any]]) -> list[str]:
    added = []
    start = next_milestone_id(goal_state)
    for offset, item in enumerate(proposals):
        if not isinstance(item, dict) or not str(item.get("title", "")).strip():
            continue
        milestone = normalize_milestone(item, f"M{start + offset}")
        milestone["id"] = f"M{start + offset}"
        goal_state.setdefault("milestones", []).append(milestone)
        added.append(milestone["id"])
    if added:
        goal_state["status"] = "active"
        goal_state["needs_replan"] = False
    return added


def apply_milestone_updates(goal_state: dict[str, Any], updates: list[dict[str, Any]]) -> list[str]:
    applied = []
    by_id = {str(m.get("id")): m for m in goal_state.get("milestones", [])}
    for update in updates or []:
        if not isinstance(update, dict):
            continue
        target = by_id.get(str(update.get("id")))
        status = str(update.get("status", ""))
        if target is None or status not in MILESTONE_STATUSES:
            continue
        target["status"] = status
        if update.get("reason"):
            target["notes"] = str(update["reason"])
        target["updated_at"] = utc_now()
        applied.append(f"{target['id']}->{status}")
    return applied


def record_findings(goal_state: dict[str, Any], session: int, mission: str, milestone: dict[str, Any] | None, outcome: dict[str, Any] | None) -> None:
    entry = {
        "session": session,
        "mission": mission,
        "milestone_id": milestone["id"] if milestone else None,
        "summary": (outcome or {}).get("summary") or "(no outcome file written)",
        "report_path": (outcome or {}).get("report_path"),
        "findings": list((outcome or {}).get("findings") or []),
        "recorded_at": utc_now(),
    }
    goal_state.setdefault("findings", []).append(entry)
    for question in (outcome or {}).get("open_questions") or []:
        if question not in goal_state.setdefault("open_questions", []):
            goal_state["open_questions"].append(question)


def append_supervisor_notes(config: dict[str, Any], session: int, text: str) -> None:
    if not text.strip():
        return
    existing = read_text(supervisor_notes_path(config)) if supervisor_notes_path(config).exists() else ""
    block = f"\n## Session {session:04d} ({utc_now()})\n\n{text.strip()}\n"
    write_text(supervisor_notes_path(config), existing + block)


def milestones_complete(goal_state: dict[str, Any]) -> bool:
    milestones = goal_state.get("milestones", [])
    if not milestones:
        return False
    terminal = all(m.get("status") in ("done", "dropped") for m in milestones)
    return terminal and any(m.get("status") == "done" for m in milestones)


def freeze_completion(config: dict[str, Any], goal_state: dict[str, Any], session: int) -> Path:
    frozen = state_dir(config) / "frozen" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    frozen.mkdir(parents=True, exist_ok=True)
    repo = workspace_path(config)
    write_text(frozen / "workspace.diff", git(["diff"], repo).stdout)
    write_text(frozen / "workspace.status", git(["status", "--short"], repo).stdout)
    write_json(frozen / "goal_state.json", goal_state)
    for name, source in (
        ("research_notebook.md", notebook_path(config)),
        ("hypotheses.json", hypotheses_path(config)),
        ("supervisor_notes.md", supervisor_notes_path(config)),
        ("final_report.md", reports_dir(config) / "final_report.md"),
    ):
        if Path(source).exists():
            shutil.copy2(source, frozen / name)
    write_json(
        state_dir(config) / "success.json",
        {"created_at": utc_now(), "frozen_dir": str(frozen), "final_session": session},
    )
    return frozen


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def next_session_number(config: dict[str, Any]) -> int:
    highest = 0
    for path in (state_dir(config) / "sessions").glob("session-*"):
        try:
            highest = max(highest, int(path.name.split("-")[-1]))
        except ValueError:
            continue
    return highest + 1


def update_status(config: dict[str, Any], **fields: Any) -> None:
    existing = read_json(status_path(config), default={}) or {}
    existing.update(fields)
    existing["updated_at"] = utc_now()
    if "pid" not in fields:
        existing["pid"] = os.getpid()
    write_json(status_path(config), existing)


def run_session(config: dict[str, Any], worker_backend: Backend, supervisor_backend: Backend | None, session: int, *, dry_run: bool = False) -> dict[str, Any]:
    goal_state = load_goal_state(config)
    mission, milestone = pick_mission(goal_state)
    if mission is None:
        return {"session": session, "status": goal_state.get("status", "complete"), "mission": None}

    sdir = session_dir(config, session)
    sdir.mkdir(parents=True, exist_ok=True)
    if mission == "work" and milestone is not None and milestone.get("status") == "pending":
        milestone["status"] = "active"
        milestone["updated_at"] = utc_now()
        save_goal_state(config, goal_state)

    brief = build_brief(config, goal_state, session, mission, milestone)
    write_text(sdir / "brief.md", brief)

    if dry_run:
        request = SessionRequest(
            prompt=brief,
            cwd=str(workspace_path(config)),
            add_dirs=[str(state_dir(config))],
            model=config["worker"].get("model"),
            permission_mode=str(config["worker"]["permission_mode"]),
            allowed_tools=list(config["worker"].get("allowed_tools") or []),
            session_dir=str(sdir),
            state_dir=str(state_dir(config)),
        )
        return {
            "session": session,
            "status": "dry_run",
            "mission": mission,
            "milestone": milestone["id"] if milestone else None,
            "brief_path": str(sdir / "brief.md"),
            "brief_chars": len(brief),
            "worker_argv": worker_backend.build_argv(request),
        }

    update_status(config, status="worker_session", session=session, mission=mission)
    print(f"[{utc_now()}] session {session}: {mission} mission via {worker_backend.name}", flush=True)
    baseline = guardrail_baseline(config)
    result = run_worker_session(config, worker_backend, session, brief)
    audit = enforce_guardrails(config, sdir, baseline)
    outcome = read_outcome(config, session)
    print(
        f"[{utc_now()}] session {session}: worker ok={result.ok} outcome={'present' if outcome else 'MISSING'}"
        + (f" audit={audit}" if (audit["reverted"] or audit["quarantined"] or audit["head_reset"]) else ""),
        flush=True,
    )

    gates = config["gates"]
    gate_summary: dict[str, Any] = {}
    validation_results, validation_ok = run_gate_commands(config, list(gates.get("validation_commands") or []), sdir, "validation")
    gate_summary["validation"] = {"passed": validation_ok, "results": [{k: r[k] for k in ("name", "returncode")} for r in validation_results]}

    canary = gates.get("canary") or {}
    canary_every = int(canary.get("every_sessions", 0) or 0)
    if canary.get("commands") and canary_every > 0 and session % canary_every == 0:
        canary_results, canary_ok = run_gate_commands(config, list(canary["commands"]), sdir, "canary")
        gate_summary["canary"] = {"passed": canary_ok, "results": [{k: r[k] for k in ("name", "returncode")} for r in canary_results]}

    verification_ok = None
    proposal = str((outcome or {}).get("milestone_status_proposal") or "")
    if mission == "work" and milestone is not None and proposal == "done" and milestone.get("verification_commands"):
        verification_results, verification_ok = run_gate_commands(config, list(milestone["verification_commands"]), sdir, "verification")
        gate_summary["verification"] = {"passed": verification_ok, "results": [{k: r[k] for k in ("name", "returncode")} for r in verification_results]}

    completion_ok = None
    if mission == "synthesize" and (outcome or {}).get("goal_complete"):
        completion_results, completion_ok = run_gate_commands(config, list(gates.get("completion_commands") or []), sdir, "completion")
        gate_summary["completion"] = {"passed": completion_ok, "results": [{k: r[k] for k in ("name", "returncode")} for r in completion_results]}

    verdict = None
    if supervisor_backend is not None:
        update_status(config, status="supervisor_session", session=session, mission=mission)
        print(f"[{utc_now()}] session {session}: supervisor review via {supervisor_backend.name}", flush=True)
        prompt = supervisor_user_prompt(config, session, mission, milestone, outcome, gate_summary, result.ok, result.error)
        verdict = run_supervisor(config, supervisor_backend, session, prompt)
        if verdict is None:
            print(f"[{utc_now()}] session {session}: supervisor verdict unavailable — continuing with worker proposals", flush=True)

    goal_state = load_goal_state(config)
    record_findings(goal_state, session, mission, milestone, outcome)

    action = "continue"
    applied: list[str] = []
    if verdict is not None:
        action = str(verdict.get("action") or "continue")
        if action not in VERDICT_ACTIONS:
            action = "continue"
        if (outcome or {}).get("proposed_milestones") and verdict.get("approve_proposed_milestones"):
            applied += [f"+{m}" for m in add_proposed_milestones(goal_state, list(outcome["proposed_milestones"]))]
        applied += apply_milestone_updates(goal_state, list(verdict.get("milestone_updates") or []))
        append_supervisor_notes(config, session, str(verdict.get("steering_notes") or ""))
        if action == "replan":
            goal_state["needs_replan"] = True
        if action == "fresh_session":
            store_resume_id(config, None)
        if action == "halt":
            goal_state["status"] = "halted"
            append_supervisor_notes(config, session, f"HALT: {verdict.get('halt_reason') or '(no reason given)'}")
    else:
        # No supervisor (disabled or failed): apply worker proposals, gated by
        # verification commands when they exist.
        if (outcome or {}).get("proposed_milestones") and mission == "plan":
            applied += [f"+{m}" for m in add_proposed_milestones(goal_state, list(outcome["proposed_milestones"]))]
        if mission == "work" and milestone is not None and proposal in MILESTONE_STATUSES:
            if proposal != "done" or verification_ok in (True, None):
                applied += apply_milestone_updates(goal_state, [{"id": milestone["id"], "status": proposal}])

    completed = False
    if (
        mission == "synthesize"
        and (outcome or {}).get("goal_complete")
        and milestones_complete(goal_state)
        and completion_ok in (True, None)
        and action not in ("halt", "replan")
    ):
        goal_state["status"] = "complete"
        completed = True

    save_goal_state(config, goal_state)

    record = {
        "session": session,
        "finished_at": utc_now(),
        "status": "complete" if completed else ("halted" if action == "halt" else "continue"),
        "mission": mission,
        "milestone": milestone["id"] if milestone else None,
        "worker_ok": result.ok,
        "worker_error": result.error,
        "outcome_present": outcome is not None,
        "gates": gate_summary,
        "audit": audit,
        "verdict_action": action if verdict else None,
        "supervisor_used": verdict is not None,
        "applied": applied,
    }
    write_json(sdir / "session.json", record)

    if completed:
        frozen = freeze_completion(config, goal_state, session)
        record["frozen_dir"] = str(frozen)
        print(f"[{utc_now()}] session {session}: GOAL COMPLETE — frozen at {frozen}", flush=True)
    return record


def run_loop(config: dict[str, Any], *, once: bool = False, dry_run: bool = False) -> int:
    ensure_layout(config)
    worker_backend = make_backend(config["worker"])
    supervisor_backend = make_backend(config["supervisor"]) if config["supervisor"].get("enabled") else None
    max_sessions = int(config["loop"]["max_sessions"])
    start_session = next_session_number(config)
    update_status(config, status="running", config_path=config["_meta"]["config_path"], backend=worker_backend.name)

    for session in range(start_session, start_session + max_sessions):
        record = run_session(config, worker_backend, supervisor_backend, session, dry_run=dry_run)
        update_status(
            config,
            status=record["status"],
            session=session,
            mission=record.get("mission"),
            last_session_dir=str(session_dir(config, session)),
        )
        if dry_run or once:
            print(json.dumps(record, indent=2, sort_keys=True))
            return 0
        if record.get("mission") is None:
            print(f"[{utc_now()}] goal status is {record['status']} — nothing to do", flush=True)
            return 0
        if record["status"] == "complete" and config["loop"].get("stop_on_complete", True):
            update_status(config, status="complete")
            return 0
        if record["status"] == "halted":
            update_status(config, status="halted")
            return 0
        time.sleep(float(config["loop"]["sleep_seconds"]))

    update_status(config, status="max_sessions")
    return 2


# ---------------------------------------------------------------------------
# Daemon plumbing and CLI
# ---------------------------------------------------------------------------

def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start(config: dict[str, Any]) -> int:
    ensure_layout(config)
    existing = read_json(status_path(config), default={}) or {}
    pid = existing.get("pid")
    if pid and process_alive(int(pid)) and existing.get("status") in ("running", "worker_session", "supervisor_session"):
        print(f"Goal loop already running with pid {pid}")
        return 0
    with log_path(config).open("a") as handle:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "run", "--config", config["_meta"]["config_path"]],
            cwd=str(repo_root()),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    pid_path(config).write_text(str(process.pid) + "\n", encoding="utf-8")
    update_status(config, status="starting", pid=process.pid, log_path=str(log_path(config)))
    print(f"Started goal loop pid={process.pid}")
    print(f"Log: {log_path(config)}")
    return 0


def stop(config: dict[str, Any]) -> int:
    status = read_json(status_path(config), default={}) or {}
    pid = status.get("pid")
    if not pid or not process_alive(int(pid)):
        print("No running goal loop found.")
        return 0
    pid = int(pid)
    os.killpg(pid, signal.SIGTERM)
    time.sleep(2)
    if process_alive(pid):
        os.killpg(pid, signal.SIGKILL)
    update_status(config, status="stopped")
    print(f"Stopped goal loop pid={pid}")
    return 0


def show_status(config: dict[str, Any]) -> int:
    ensure_layout(config)
    status = read_json(status_path(config), default={}) or {"status": "not_started"}
    pid = status.get("pid")
    status["process_alive"] = bool(pid and process_alive(int(pid)))
    goal_state = load_goal_state(config)
    status["goal_status"] = goal_state.get("status")
    status["milestones"] = [f"{m['id']} [{m['status']}] {m['title']}" for m in goal_state.get("milestones", [])]
    print(json.dumps(status, indent=2, sort_keys=True))
    print(f"\nNotebook: {notebook_path(config)}")
    print(f"Reports:  {reports_dir(config)}")
    return 0


def show_report(config: dict[str, Any]) -> int:
    final = reports_dir(config) / "final_report.md"
    if final.exists():
        print(final.read_text())
        return 0
    candidates = sorted(reports_dir(config).glob("session-*.md"))
    if not candidates:
        print("No reports written yet.")
        return 0
    print(f"(no final report yet — latest session report: {candidates[-1]})\n")
    print(candidates[-1].read_text())
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Goal-driven research loop over real agent sessions.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("dry-run", "once", "run", "start", "stop", "status", "report"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--config", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    if args.command == "dry-run":
        return run_loop(config, once=True, dry_run=True)
    if args.command == "once":
        return run_loop(config, once=True)
    if args.command == "run":
        return run_loop(config)
    if args.command == "start":
        return start(config)
    if args.command == "stop":
        return stop(config)
    if args.command == "status":
        return show_status(config)
    if args.command == "report":
        return show_report(config)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
