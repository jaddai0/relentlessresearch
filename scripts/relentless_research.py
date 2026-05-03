#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from relentless_common import (
    RelentlessResearchError,
    config_environment,
    extract_json_object,
    parse_env_file,
    read_json,
    read_text,
    resolve_env_reference,
    run_shell,
    utc_now,
    write_json,
    write_text,
)


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
    env_files = [resolve_path(root, value) for value in config.get("env_files", [])]
    env_files = [value for value in env_files if value]
    env = config_environment(env_files)

    config["_meta"] = {
        "config_path": str(config_path),
        "repo_root": str(root),
    }
    config["env_files"] = env_files
    config["state_dir"] = resolve_path(root, config.get("state_dir", ".relentless"))
    config["target_repo"]["path"] = resolve_path(root, config["target_repo"]["path"])
    prompt = config.setdefault("prompt", {})
    prompt["system"] = resolve_path(root, prompt.get("system", "prompts/relentless_system.md"))

    worker_api = config.setdefault("worker_api", {})
    for key in ("api_key", "base_url"):
        if isinstance(worker_api.get(key), str):
            worker_api[key] = resolve_env_reference(worker_api[key], env)
    return config


def state_dir(config: dict[str, Any]) -> Path:
    return Path(config["state_dir"])


def target_repo(config: dict[str, Any]) -> Path:
    return Path(config["target_repo"]["path"])


def ensure_layout(config: dict[str, Any]) -> None:
    for part in ("iterations", "logs", "snapshots", "frozen", "compacted", "archives"):
        (state_dir(config) / part).mkdir(parents=True, exist_ok=True)
    if not notebook_path(config).exists():
        write_text(notebook_path(config), initial_notebook(config))
    if not hypotheses_path(config).exists():
        write_json(hypotheses_path(config), initial_hypotheses(config))
    if not supervisor_notes_path(config).exists():
        write_text(supervisor_notes_path(config), initial_supervisor_notes(config))


def notebook_path(config: dict[str, Any]) -> Path:
    return state_dir(config) / "research_notebook.md"


def hypotheses_path(config: dict[str, Any]) -> Path:
    return state_dir(config) / "hypotheses.json"


def supervisor_notes_path(config: dict[str, Any]) -> Path:
    return state_dir(config) / "supervisor_notes.md"


def status_path(config: dict[str, Any]) -> Path:
    return state_dir(config) / "status.json"


def pid_path(config: dict[str, Any]) -> Path:
    return state_dir(config) / "relentless.pid"


def log_path(config: dict[str, Any]) -> Path:
    return state_dir(config) / "relentless.log"


def initial_notebook(config: dict[str, Any]) -> str:
    problem = config.get("problem", {})
    lines = [
        f"# {problem.get('title', config.get('name', 'RelentlessResearch'))}",
        "",
        "## Objective",
        str(problem.get("objective", "")).strip(),
        "",
        "## Known Facts",
    ]
    for fact in problem.get("known_facts", []):
        lines.append(f"- {fact}")
    lines.extend(
        [
            "",
            "## Current Strategy",
            str(problem.get("primary_strategy", "")).strip(),
            "",
            "## Rejected Hypotheses",
            "- None yet.",
            "",
            "## Latest State",
            "- No iterations have run yet.",
            "",
        ]
    )
    return "\n".join(lines)


def initial_hypotheses(config: dict[str, Any]) -> dict[str, Any]:
    problem = config.get("problem", {})
    hypotheses = []
    for index, text in enumerate(problem.get("initial_hypotheses", []), start=1):
        hypotheses.append(
            {
                "id": f"H{index}",
                "summary": str(text),
                "status": "active",
                "evidence": [],
                "shared_assumptions": [],
                "next_discriminating_test": "",
                "do_not_repeat_until": "",
                "updated_at": utc_now(),
            }
        )
    return {
        "schema": "relentless-hypotheses-v1",
        "updated_at": utc_now(),
        "hypotheses": hypotheses,
    }


def initial_supervisor_notes(config: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Supervisor Notes",
            "",
            "This file is injected late in every RelentlessResearch prompt and should contain",
            "fresh architect guidance, interpretation warnings, and stop/steer instructions.",
            "",
            "- Add new notes here when the worker repeats a ruled-out path, over-trusts weak evidence,",
            "  or needs a sharper next experiment.",
            "- If you change config or gates, restart the loop. If you only edit this file, the next",
            "  iteration will see it.",
            "",
        ]
    )


def git(args: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def is_path_allowed(config: dict[str, Any], relative: str) -> bool:
    rel = relative.strip().lstrip("/")
    if not rel or rel.startswith("../") or "/../" in rel:
        return False
    return any(fnmatch.fnmatch(rel, pattern) for pattern in config.get("editable_globs", []))


def discover_allowed_patch_paths(config: dict[str, Any], diff_path: Path) -> list[str]:
    result = subprocess.run(
        ["git", "apply", "--numstat", "--check", str(diff_path)],
        cwd=str(target_repo(config)),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RelentlessResearchError(f"git apply --check failed:\n{result.stderr or result.stdout}")
    paths = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            paths.append(parts[-1])
    return paths


def apply_unified_diff(config: dict[str, Any], run_dir: Path, diff_text: str) -> list[str]:
    cleaned = diff_text.strip()
    if not cleaned:
        return []
    diff_path = run_dir / "proposal.diff"
    diff_path.write_text(cleaned + "\n", encoding="utf-8")
    paths = discover_allowed_patch_paths(config, diff_path)
    blocked = [path for path in paths if not is_path_allowed(config, path)]
    if blocked:
        raise RelentlessResearchError(f"Unified diff edits disallowed path(s): {', '.join(blocked)}")
    result = subprocess.run(
        ["git", "apply", str(diff_path)],
        cwd=str(target_repo(config)),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RelentlessResearchError(f"git apply failed:\n{result.stderr or result.stdout}")
    return paths


def apply_full_edits(config: dict[str, Any], edits: list[dict[str, Any]]) -> list[str]:
    repo = target_repo(config)
    written = []
    for edit in edits:
        relative = str(edit.get("path") or "")
        if not is_path_allowed(config, relative):
            raise RelentlessResearchError(f"Full edit attempted disallowed path: {relative}")
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(str(edit.get("content", "")), encoding="utf-8")
        written.append(relative)
    return written


def changed_files(config: dict[str, Any]) -> list[str]:
    result = git(["status", "--short"], target_repo(config))
    files = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        files.append(line[3:].strip())
    return files


def context_file_paths(config: dict[str, Any]) -> list[str]:
    repo = target_repo(config)
    seen: set[str] = set()
    paths: list[str] = []
    for relative in config.get("context_files", []):
        if (repo / relative).exists() and relative not in seen:
            seen.add(relative)
            paths.append(relative)
    for pattern in config.get("context_globs", []):
        for candidate in sorted(repo.glob(pattern)):
            if candidate.is_file():
                relative = str(candidate.relative_to(repo))
                if relative not in seen:
                    seen.add(relative)
                    paths.append(relative)
    return paths


def collect_context(config: dict[str, Any]) -> str:
    repo = target_repo(config)
    max_per_file = int(config.get("max_context_chars_per_file", 60000))
    max_total = int(config.get("max_total_context_chars", 500000))
    chunks: list[str] = []
    total = 0
    for relative in context_file_paths(config):
        path = repo / relative
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if len(content) > max_per_file:
            content = content[:max_per_file] + "\n\n[TRUNCATED]\n"
        chunk = f"\n### FILE: {relative}\n\n{content}\n"
        if total + len(chunk) > max_total:
            chunks.append("\n[CONTEXT TRUNCATED BY max_total_context_chars]\n")
            break
        chunks.append(chunk)
        total += len(chunk)
    return "\n".join(chunks)


def render_hypotheses(config: dict[str, Any]) -> str:
    data = read_json(hypotheses_path(config), default=initial_hypotheses(config)) or {}
    return json.dumps(data, indent=2, sort_keys=True)


def render_supervisor_notes(config: dict[str, Any]) -> str:
    return supervisor_notes_path(config).read_text(encoding="utf-8", errors="replace")


def apply_hypothesis_updates(config: dict[str, Any], updates: list[dict[str, Any]]) -> None:
    if not updates:
        return
    data = read_json(hypotheses_path(config), default=initial_hypotheses(config)) or initial_hypotheses(config)
    hypotheses = data.setdefault("hypotheses", [])
    by_id = {str(item.get("id")): item for item in hypotheses if isinstance(item, dict)}
    for update in updates:
        if not isinstance(update, dict):
            continue
        hypothesis_id = str(update.get("id") or "").strip()
        if not hypothesis_id:
            hypothesis_id = f"H{len(hypotheses) + 1}"
            update["id"] = hypothesis_id
        target = by_id.get(hypothesis_id)
        if target is None:
            target = {
                "id": hypothesis_id,
                "summary": "",
                "status": "active",
                "evidence": [],
                "shared_assumptions": [],
                "next_discriminating_test": "",
                "do_not_repeat_until": "",
            }
            hypotheses.append(target)
            by_id[hypothesis_id] = target
        for key in (
            "summary",
            "status",
            "next_discriminating_test",
            "do_not_repeat_until",
            "owner_role",
        ):
            if key in update:
                target[key] = update[key]
        for key in ("evidence", "shared_assumptions"):
            if key in update:
                incoming = update.get(key)
                if not isinstance(incoming, list):
                    incoming = [incoming]
                existing = target.setdefault(key, [])
                for item in incoming:
                    if item not in existing:
                        existing.append(item)
        target["updated_at"] = utc_now()
    data["updated_at"] = utc_now()
    write_json(hypotheses_path(config), data)


def tail_text(path: Path, max_chars: int = 8000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(errors="replace")
    return text[-max_chars:]


def recent_iteration_summaries(config: dict[str, Any]) -> str:
    max_recent = int(config.get("loop", {}).get("max_recent_iterations", 8))
    parts = []
    for path in sorted((state_dir(config) / "iterations").glob("iteration-*/iteration.json"))[-max_recent:]:
        data = read_json(path, default={}) or {}
        error = data.get("error")
        error_text = f" error={error}" if error else ""
        parts.append(
            f"- {path.parent.stem}: status={data.get('status')} summary={data.get('iteration_summary')} "
            f"validation={data.get('validation_passed')} canary={data.get('canary_passed')} "
            f"success={data.get('success_passed')}{error_text}"
        )
    return "\n".join(parts) if parts else "- None yet."


def compacted_checkpoint_paths(config: dict[str, Any]) -> list[Path]:
    return sorted((state_dir(config) / "compacted").glob("checkpoint-*.md"))


def latest_compacted_iteration(config: dict[str, Any]) -> int:
    latest = 0
    for path in compacted_checkpoint_paths(config):
        try:
            latest = max(latest, int(path.stem.split("-")[-1]))
        except ValueError:
            continue
    return latest


def compacted_context(config: dict[str, Any]) -> str:
    compaction = config.get("compaction", {})
    keep = int(compaction.get("prompt_checkpoints", 1))
    max_chars = int(compaction.get("max_prompt_chars", 24000))
    paths = compacted_checkpoint_paths(config)[-keep:]
    if not paths:
        return "- No compacted checkpoints yet."
    chunks = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[-max_chars:]
            text = "[CHECKPOINT TRUNCATED TO RECENT CONTENT]\n\n" + text
        chunks.append(f"### {path.name}\n\n{text}")
    return "\n\n".join(chunks)


def recent_observations(config: dict[str, Any]) -> str:
    max_recent = int(config.get("loop", {}).get("max_recent_iterations", 8))
    compacted_after = latest_compacted_iteration(config)
    tail_chars = int(config.get("loop", {}).get("max_observation_tail_chars", 2500))
    max_total = int(config.get("loop", {}).get("max_recent_observation_chars", 36000))
    chunks: list[str] = []
    paths = []
    for path in sorted((state_dir(config) / "iterations").glob("iteration-*/iteration.json")):
        try:
            iteration = int(path.parent.stem.split("-")[-1])
        except ValueError:
            iteration = 0
        if iteration > compacted_after:
            paths.append(path)
    if not paths:
        paths = sorted((state_dir(config) / "iterations").glob("iteration-*/iteration.json"))[-max_recent:]
    else:
        paths = paths[-max_recent:]
    for path in paths:
        data = read_json(path, default={}) or {}
        chunks.append(f"\n### {path.parent.stem} ({data.get('status')})\n")
        if data.get("error"):
            chunks.append(f"\nError: {data.get('error')}\n")
        if data.get("worker_response_tail"):
            chunks.append(
                "\nWorker response tail:\n"
                f"```text\n{data.get('worker_response_tail')}\n```\n"
            )
        for phase in ("diagnostics", "external_reference", "validation", "canary", "success"):
            results = data.get(phase) or []
            if not results:
                continue
            chunks.append(f"\n#### {phase}\n")
            for result in results:
                name = result.get("name")
                returncode = result.get("returncode")
                command = result.get("command")
                tail = str(result.get("tail") or "").strip()
                if len(tail) > tail_chars:
                    tail = tail[-tail_chars:]
                chunks.append(
                    f"\n{name}: returncode={returncode}\n"
                    f"command: {command}\n"
                    f"tail:\n```text\n{tail}\n```\n"
                )
    text = "\n".join(chunks).strip() if chunks else "- None yet."
    if len(text) > max_total:
        text = "[RECENT OBSERVATIONS TRUNCATED]\n\n" + text[-max_total:]
    return text


def build_prompt(config: dict[str, Any], iteration: int) -> str:
    repo = target_repo(config)
    problem = config.get("problem", {})
    status = git(["status", "--short"], repo).stdout.strip() or "(clean)"
    max_status = int(config.get("loop", {}).get("max_git_status_chars", 12000))
    if len(status) > max_status:
        status = status[:max_status] + "\n[STATUS TRUNCATED]\n"
    diff = git(["diff", "--", *config.get("editable_globs", [])], repo).stdout
    max_diff = int(config.get("loop", {}).get("max_diff_chars", 50000))
    if len(diff) > max_diff:
        diff = diff[:max_diff] + "\n\n[DIFF TRUNCATED]\n"

    return "\n".join(
        [
            f"RelentlessResearch iteration: {iteration}",
            f"Target repo: {repo}",
            "",
            "## Problem",
            json.dumps(problem, indent=2),
            "",
            "## Persistent Research Notebook",
            notebook_path(config).read_text(encoding="utf-8"),
            "",
            "## Structured Hypothesis Ledger",
            render_hypotheses(config),
            "",
            "## Compacted Research Checkpoints",
            compacted_context(config),
            "",
            "## Recent Iterations",
            recent_iteration_summaries(config),
            "",
            "## Recent Command Observations",
            recent_observations(config),
            "",
            "## Git Status",
            status,
            "",
            "## Current Editable Diff",
            diff or "(no diff)",
            "",
            "## Fixed Validation Commands",
            json.dumps(config.get("validation_commands", []), indent=2),
            "",
            "## Success Commands",
            json.dumps(config.get("success_commands", []), indent=2),
            "",
            "## Context Files",
            collect_context(config),
            "",
            "## Supervisor Notes (Highest Priority, Freshest Guidance)",
            render_supervisor_notes(config),
            "",
            "Choose the next experiment. Return exactly the configured JSON object.",
        ]
    )


def call_model(config: dict[str, Any], system_prompt: str, user_prompt: str) -> str:
    worker_api = config["worker_api"]
    base_url = worker_api["base_url"].rstrip("/")
    headers = {"Content-Type": "application/json"}
    api_key = worker_api.get("api_key")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if isinstance(worker_api.get("extra_headers"), dict):
        headers.update(worker_api["extra_headers"])

    payload: dict[str, Any] = {
        "model": worker_api["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": worker_api.get("max_tokens", 8192),
    }
    if "temperature" in worker_api:
        payload["temperature"] = worker_api["temperature"]
    if isinstance(worker_api.get("provider_extras"), dict):
        payload.update(worker_api["provider_extras"])

    last_error: Exception | None = None
    for attempt in range(int(worker_api.get("max_retries", 3))):
        try:
            response = requests.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=worker_api.get("timeout_seconds", 300),
            )
            response.raise_for_status()
            data = response.json()
            message = data["choices"][0]["message"]
            content = message.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    item.get("text", "") if isinstance(item, dict) else str(item)
                    for item in content
                )
            reasoning = message.get("reasoning_content")
            if reasoning:
                return f"<reasoning>\n{reasoning}\n</reasoning>\n\n{content}"
            return str(content)
        except Exception as exc:  # noqa: BLE001 - preserve provider detail in logs.
            last_error = exc
            if attempt + 1 < int(worker_api.get("max_retries", 3)):
                time.sleep(float(worker_api.get("retry_backoff_base_seconds", 10.0)) * (2**attempt))
    raise RelentlessResearchError(f"Worker API request failed: {last_error}") from last_error


def validate_model_command(config: dict[str, Any], command: str) -> None:
    policy = config.get("command_policy", {})
    stripped = command.strip()
    forbidden = [token for token in policy.get("forbidden_substrings", []) if token and token in stripped]
    if forbidden:
        raise RelentlessResearchError(f"Command contains forbidden token(s): {', '.join(forbidden)}")
    prefixes = policy.get("allowed_prefixes", [])
    if prefixes and not any(stripped.startswith(prefix) for prefix in prefixes):
        raise RelentlessResearchError(f"Command is not allowed by prefix policy: {stripped}")


def run_command_list(
    config: dict[str, Any],
    commands: list[dict[str, Any]],
    run_dir: Path,
    phase: str,
    *,
    validate_commands: bool,
) -> list[dict[str, Any]]:
    results = []
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "PYTHONFAULTHANDLER": "1",
        }
    )
    if isinstance(config.get("command_env"), dict):
        env.update({str(key): str(value) for key, value in config["command_env"].items()})
    for index, item in enumerate(commands, start=1):
        command = str(item.get("command") if isinstance(item, dict) else item)
        if validate_commands:
            validate_model_command(config, command)
        name = str(item.get("name") or f"{phase}-{index}") if isinstance(item, dict) else f"{phase}-{index}"
        progress_path = run_dir / f"{phase}-progress.json"
        write_json(
            progress_path,
            {
                "phase": phase,
                "status": "running",
                "current": name,
                "index": index,
                "total": len(commands),
                "updated_at": utc_now(),
                "results": results,
            },
        )
        result = run_shell(
            command,
            cwd=target_repo(config),
            env=env,
            log_path=run_dir / f"{phase}-{index:02d}-{name}.log",
            timeout_seconds=(item.get("timeout_seconds") if isinstance(item, dict) else None),
        )
        result["name"] = name
        result["command"] = command
        result["tail"] = tail_text(Path(result["log_path"]))
        results.append(result)
        write_json(
            progress_path,
            {
                "phase": phase,
                "status": "running" if result["returncode"] == 0 and index < len(commands) else "finished",
                "current": name,
                "index": index,
                "total": len(commands),
                "updated_at": utc_now(),
                "results": results,
            },
        )
        if result["returncode"] != 0:
            break
    return results


def phase_passed(results: list[dict[str, Any]]) -> bool:
    return all(int(item.get("returncode", 1)) == 0 for item in results)


def should_run_periodic(config: dict[str, Any], section: str, iteration: int, *, default_every: int = 0) -> bool:
    settings = config.get(section, {})
    if isinstance(settings, list):
        return bool(settings)
    if not isinstance(settings, dict):
        return False
    commands = settings.get("commands", [])
    if not commands:
        return False
    every = int(settings.get("every_iterations", default_every))
    if every <= 0:
        return False
    return iteration % every == 0


def periodic_commands(config: dict[str, Any], section: str) -> list[dict[str, Any]]:
    settings = config.get(section, {})
    if isinstance(settings, list):
        return list(settings)
    if isinstance(settings, dict):
        return list(settings.get("commands") or [])
    return []


def write_snapshot(config: dict[str, Any], run_dir: Path, label: str) -> str:
    diff = git(["diff"], target_repo(config)).stdout
    path = run_dir / f"{label}.diff"
    path.write_text(diff, encoding="utf-8")
    return str(path)


def freeze_success(config: dict[str, Any], run_dir: Path) -> None:
    frozen_dir = state_dir(config) / "frozen" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    frozen_dir.mkdir(parents=True, exist_ok=True)
    write_snapshot(config, frozen_dir, "success")
    frozen_config = json.loads(json.dumps(config))
    if isinstance(frozen_config.get("worker_api"), dict) and frozen_config["worker_api"].get("api_key"):
        frozen_config["worker_api"]["api_key"] = "[redacted]"
    write_json(frozen_dir / "config.json", frozen_config)
    write_json(frozen_dir / "hypotheses.json", read_json(hypotheses_path(config), default={}) or {})
    write_text(frozen_dir / "research_notebook.md", notebook_path(config).read_text(encoding="utf-8", errors="replace"))
    write_text(frozen_dir / "supervisor_notes.md", render_supervisor_notes(config))
    provenance_results = run_command_list(
        config,
        list(config.get("provenance_commands") or []),
        frozen_dir,
        "provenance",
        validate_commands=False,
    )
    write_json(
        state_dir(config) / "success.json",
        {
            "created_at": utc_now(),
            "run_dir": str(run_dir),
            "diff": str(frozen_dir / "success.diff"),
            "target_repo": str(target_repo(config)),
            "hypotheses": str(frozen_dir / "hypotheses.json"),
            "notebook": str(frozen_dir / "research_notebook.md"),
            "supervisor_notes": str(frozen_dir / "supervisor_notes.md"),
            "provenance": provenance_results,
        },
    )


def iteration_record_paths(config: dict[str, Any], *, up_to_iteration: int | None = None) -> list[Path]:
    paths = []
    for path in sorted((state_dir(config) / "iterations").glob("iteration-*/iteration.json")):
        try:
            iteration = int(path.parent.stem.split("-")[-1])
        except ValueError:
            continue
        if up_to_iteration is None or iteration <= up_to_iteration:
            paths.append(path)
    return paths


def _compact_result_line(result: dict[str, Any], *, max_tail: int) -> str:
    tail = str(result.get("tail") or "").strip()
    if len(tail) > max_tail:
        tail = tail[-max_tail:]
    return (
        f"- `{result.get('name')}` rc={result.get('returncode')} "
        f"cmd=`{result.get('command')}`\n"
        f"  tail: {tail.replace(chr(10), ' | ')}"
    )


def build_checkpoint_summary(config: dict[str, Any], up_to_iteration: int) -> str:
    records = []
    for path in iteration_record_paths(config, up_to_iteration=up_to_iteration):
        data = read_json(path, default={}) or {}
        data["_iteration"] = int(path.parent.stem.split("-")[-1])
        records.append(data)

    status_counts: dict[str, int] = {}
    for record in records:
        status = str(record.get("status"))
        status_counts[status] = status_counts.get(status, 0) + 1

    compaction = config.get("compaction", {})
    keep_iterations = int(compaction.get("checkpoint_recent_iterations", 20))
    result_tail = int(compaction.get("checkpoint_result_tail_chars", 1000))
    recent = records[-keep_iterations:]

    lines = [
        f"# RelentlessResearch Checkpoint Through Iteration {up_to_iteration:04d}",
        "",
        f"Created: {utc_now()}",
        f"Target repo: {target_repo(config)}",
        f"Model: {config.get('worker_api', {}).get('model')}",
        "",
        "## Status Counts",
    ]
    for status, count in sorted(status_counts.items()):
        lines.append(f"- {status}: {count}")

    lines.extend(
        [
            "",
            "## Current Durable Notebook",
            notebook_path(config).read_text(encoding="utf-8", errors="replace")[-12000:],
            "",
            "## Structured Hypothesis Ledger",
            render_hypotheses(config)[-12000:],
            "",
            "## Supervisor Notes",
            render_supervisor_notes(config)[-12000:],
            "",
            "## Recent Iteration Rollup",
        ]
    )
    for record in recent:
        lines.append(
            f"- iteration-{record.get('_iteration'):04d}: status={record.get('status')} "
            f"validation={record.get('validation_passed')} success={record.get('success_passed')} "
            f"summary={record.get('iteration_summary')} "
            f"error={record.get('error') or ''}"
        )

    lines.extend(["", "## Recent Command Evidence"])
    for record in recent:
        lines.append(f"\n### iteration-{record.get('_iteration'):04d} ({record.get('status')})")
        if record.get("current_hypothesis"):
            lines.append(f"hypothesis: {record.get('current_hypothesis')}")
        if record.get("expected_observation"):
            lines.append(f"expected: {record.get('expected_observation')}")
        if record.get("error"):
            lines.append(f"error: {record.get('error')}")
        for phase in ("diagnostics", "external_reference", "validation", "canary", "success"):
            results = record.get(phase) or []
            if not results:
                continue
            lines.append(f"{phase}:")
            for result in results:
                lines.append(_compact_result_line(result, max_tail=result_tail))

    lines.extend(
        [
            "",
            "## Supervisor Reminder",
            "Treat this checkpoint as compressed memory. Prefer stable human guidance and validated command output over worker summaries. Do not revive failed diagnostic loops unless the new attempt fixes the documented failure mode.",
            "",
        ]
    )
    return "\n".join(lines)


def archive_iterations(config: dict[str, Any], start_iteration: int, end_iteration: int) -> Path | None:
    if start_iteration > end_iteration:
        return None
    archive_path = state_dir(config) / "archives" / f"iterations-{start_iteration:04d}-{end_iteration:04d}.tar.gz"
    mode = "w:gz"
    try:
        import gzip  # noqa: F401
    except Exception:
        archive_path = state_dir(config) / "archives" / f"iterations-{start_iteration:04d}-{end_iteration:04d}.tar"
        mode = "w"
    if archive_path.exists():
        return archive_path
    with tarfile.open(archive_path, mode) as archive:
        for iteration in range(start_iteration, end_iteration + 1):
            run_dir = state_dir(config) / "iterations" / f"iteration-{iteration:04d}"
            if run_dir.exists():
                archive.add(run_dir, arcname=f"iteration-{iteration:04d}")
    return archive_path


def compact_state(config: dict[str, Any], up_to_iteration: int | None = None) -> Path | None:
    ensure_layout(config)
    records = iteration_record_paths(config, up_to_iteration=up_to_iteration)
    if not records:
        return None
    if up_to_iteration is None:
        up_to_iteration = int(records[-1].parent.stem.split("-")[-1])
    previous = latest_compacted_iteration(config)
    summary = build_checkpoint_summary(config, up_to_iteration)
    checkpoint = state_dir(config) / "compacted" / f"checkpoint-{up_to_iteration:04d}.md"
    checkpoint.write_text(summary, encoding="utf-8")
    archive_iterations(config, previous + 1, up_to_iteration)
    write_json(
        state_dir(config) / "compacted" / "latest.json",
        {
            "updated_at": utc_now(),
            "checkpoint": str(checkpoint),
            "up_to_iteration": up_to_iteration,
            "previous_checkpoint_iteration": previous,
        },
    )
    return checkpoint


def maybe_compact_state(config: dict[str, Any], iteration: int) -> None:
    compaction = config.get("compaction", {})
    if not compaction.get("enabled", False):
        return
    every = int(compaction.get("every_iterations", 10))
    if every <= 0 or iteration % every != 0:
        return
    checkpoint = compact_state(config, iteration)
    if checkpoint:
        print(f"[{utc_now()}] iteration {iteration}: compacted checkpoint {checkpoint}", flush=True)


def run_iteration(config: dict[str, Any], iteration: int, *, dry_run: bool = False) -> dict[str, Any]:
    run_dir = state_dir(config) / "iterations" / f"iteration-{iteration:04d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    system_prompt = Path(config["prompt"]["system"]).read_text(encoding="utf-8")
    prompt = build_prompt(config, iteration)
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

    if dry_run:
        return {
            "iteration": iteration,
            "status": "dry_run",
            "prompt_path": str(run_dir / "prompt.txt"),
            "context_chars": len(prompt),
            "changed_files": changed_files(config),
        }

    print(f"[{utc_now()}] iteration {iteration}: calling {config['worker_api'].get('model')}", flush=True)
    write_json(
        status_path(config),
        {
            "status": "calling_model",
            "pid": os.getpid(),
            "iteration": iteration,
            "updated_at": utc_now(),
            "last_run": str(run_dir),
            "model": config["worker_api"].get("model"),
        },
    )
    raw = call_model(config, system_prompt, prompt)
    print(f"[{utc_now()}] iteration {iteration}: model response received", flush=True)
    (run_dir / "worker-response.txt").write_text(raw, encoding="utf-8")
    try:
        proposal = extract_json_object(raw)
    except Exception as exc:  # noqa: BLE001 - keep the loop alive after provider/model formatting failures.
        error = f"Worker model response did not contain a valid JSON object: {exc}"
        print(f"[{utc_now()}] iteration {iteration}: invalid_response", flush=True)
        record = {
            "iteration": iteration,
            "finished_at": utc_now(),
            "status": "invalid_response",
            "error": error,
            "iteration_summary": "Worker response was not valid JSON.",
            "current_hypothesis": None,
            "expected_observation": "The next iteration should account for the invalid response and request a strictly valid JSON object.",
            "edited_files": [],
            "diagnostics": [],
            "external_reference": [],
            "validation": [],
            "canary": [],
            "success": [],
            "validation_passed": None,
            "canary_passed": None,
            "success_passed": False,
            "confidence": None,
            "stop": False,
            "stop_reason": "",
            "worker_response_tail": raw[-4000:],
        }
        write_json(run_dir / "iteration.json", record)
        return record
    write_json(run_dir / "proposal.json", proposal)

    edited_files: list[str] = []
    validation_results: list[dict[str, Any]] = []
    external_reference_results: list[dict[str, Any]] = []
    canary_results: list[dict[str, Any]] = []
    success_results: list[dict[str, Any]] = []
    command_results: list[dict[str, Any]] = []
    status = "running"
    error = None
    try:
        edited_files.extend(apply_unified_diff(config, run_dir, str(proposal.get("unified_diff") or "")))
        if proposal.get("edits"):
            edited_files.extend(apply_full_edits(config, list(proposal.get("edits") or [])))
        if proposal.get("notebook_update"):
            write_text(notebook_path(config), str(proposal["notebook_update"]).rstrip() + "\n")
        apply_hypothesis_updates(config, list(proposal.get("hypothesis_updates") or []))

        print(f"[{utc_now()}] iteration {iteration}: running diagnostics", flush=True)
        command_results = run_command_list(
            config,
            list(proposal.get("commands") or []),
            run_dir,
            "diagnostic",
            validate_commands=True,
        )
        run_external_reference = bool(proposal.get("external_reference_request")) or should_run_periodic(
            config,
            "external_reference_commands",
            iteration,
            default_every=0,
        )
        if run_external_reference:
            print(f"[{utc_now()}] iteration {iteration}: running external-reference commands", flush=True)
            external_reference_results = run_command_list(
                config,
                periodic_commands(config, "external_reference_commands"),
                run_dir,
                "external_reference",
                validate_commands=True,
            )
        print(f"[{utc_now()}] iteration {iteration}: running validation", flush=True)
        validation_results = run_command_list(
            config,
            list(config.get("validation_commands") or []),
            run_dir,
            "validation",
            validate_commands=False,
        )
        run_canary = bool(proposal.get("ready_for_success_check")) or should_run_periodic(
            config,
            "canary_commands",
            iteration,
            default_every=int(config.get("loop", {}).get("run_success_checks_every", 1)),
        )
        if run_canary and phase_passed(validation_results):
            print(f"[{utc_now()}] iteration {iteration}: running canary checks", flush=True)
            canary_results = run_command_list(
                config,
                periodic_commands(config, "canary_commands"),
                run_dir,
                "canary",
                validate_commands=False,
            )
        should_check_success = bool(proposal.get("ready_for_success_check")) or (
            iteration % int(config.get("loop", {}).get("run_success_checks_every", 1)) == 0
        )
        canary_ok = phase_passed(canary_results) if canary_results else True
        if should_check_success and phase_passed(validation_results) and canary_ok:
            print(f"[{utc_now()}] iteration {iteration}: running success checks", flush=True)
            success_results = run_command_list(
                config,
                list(config.get("success_commands") or []),
                run_dir,
                "success",
                validate_commands=False,
            )
        status = "success" if success_results and phase_passed(success_results) else "continue"
    except Exception as exc:  # noqa: BLE001 - captured for research continuity.
        error = str(exc)
        status = "error"

    write_snapshot(config, run_dir, "after")
    print(f"[{utc_now()}] iteration {iteration}: {status}", flush=True)
    record = {
        "iteration": iteration,
        "finished_at": utc_now(),
        "status": status,
        "error": error,
        "iteration_summary": proposal.get("iteration_summary"),
        "current_hypothesis": proposal.get("current_hypothesis"),
        "expected_observation": proposal.get("expected_observation"),
        "edited_files": sorted(set(edited_files)),
        "diagnostics": command_results,
        "external_reference": external_reference_results,
        "validation": validation_results,
        "canary": canary_results,
        "success": success_results,
        "validation_passed": phase_passed(validation_results) if validation_results else None,
        "canary_passed": phase_passed(canary_results) if canary_results else None,
        "success_passed": phase_passed(success_results) if success_results else False,
        "confidence": proposal.get("confidence"),
        "shared_assumptions": proposal.get("shared_assumptions"),
        "external_reference_request": proposal.get("external_reference_request"),
        "role_focus": proposal.get("role_focus"),
        "stop": bool(proposal.get("stop")),
        "stop_reason": proposal.get("stop_reason"),
    }
    write_json(run_dir / "iteration.json", record)
    return record


def run_loop(config: dict[str, Any], *, once: bool = False, dry_run: bool = False) -> int:
    ensure_layout(config)
    status = read_json(status_path(config), default={}) or {}
    completed_iterations = []
    for path in (state_dir(config) / "iterations").glob("iteration-*/iteration.json"):
        try:
            completed_iterations.append(int(path.parent.stem.split("-")[-1]))
        except ValueError:
            continue
    directory_iteration = max(completed_iterations, default=0)
    status_iteration = int(status.get("iteration", 0) or 0)
    start_iteration = max(directory_iteration, status_iteration) + 1
    max_iterations = int(config.get("loop", {}).get("max_iterations", 100))
    write_json(
        status_path(config),
        {
            **status,
            "status": "running",
            "pid": os.getpid(),
            "started_at": status.get("started_at") or utc_now(),
            "updated_at": utc_now(),
            "config": config["_meta"]["config_path"],
            "model": config["worker_api"].get("model"),
        },
    )

    for iteration in range(start_iteration, max_iterations + 1):
        record = run_iteration(config, iteration, dry_run=dry_run)
        write_json(
            status_path(config),
            {
                "status": record["status"],
                "pid": os.getpid(),
                "iteration": iteration,
                "updated_at": utc_now(),
                "last_run": str(state_dir(config) / "iterations" / f"iteration-{iteration:04d}"),
                "last_summary": record.get("iteration_summary"),
                "last_error": record.get("error"),
                "model": config["worker_api"].get("model"),
            },
        )
        maybe_compact_state(config, iteration)
        if dry_run or once:
            print(json.dumps(record, indent=2, sort_keys=True))
            return 0
        if record.get("success_passed") and config.get("loop", {}).get("stop_on_success", True):
            freeze_success(config, state_dir(config) / "iterations" / f"iteration-{iteration:04d}")
            write_json(status_path(config), {**read_json(status_path(config), default={}), "status": "succeeded", "updated_at": utc_now()})
            return 0
        if record.get("stop"):
            write_json(status_path(config), {**read_json(status_path(config), default={}), "status": "stopped_by_model", "updated_at": utc_now()})
            return 0
        time.sleep(float(config.get("loop", {}).get("sleep_seconds", 2)))

    write_json(status_path(config), {**read_json(status_path(config), default={}), "status": "max_iterations", "updated_at": utc_now()})
    return 2


def start(config: dict[str, Any]) -> int:
    ensure_layout(config)
    pid_file = pid_path(config)
    existing = read_json(status_path(config), default={}) or {}
    pid = existing.get("pid")
    if pid and process_alive(int(pid)):
        print(f"RelentlessResearch already running with pid {pid}")
        return 0
    log_file = log_path(config)
    with log_file.open("a") as handle:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "run", "--config", config["_meta"]["config_path"]],
            cwd=str(repo_root()),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    write_json(
        status_path(config),
        {
            **existing,
            "status": "starting",
            "pid": process.pid,
            "started_at": existing.get("started_at") or utc_now(),
            "updated_at": utc_now(),
            "config": config["_meta"]["config_path"],
            "model": config["worker_api"].get("model"),
            "log_path": str(log_file),
        },
    )
    pid_file.write_text(str(process.pid) + "\n", encoding="utf-8")
    print(f"Started RelentlessResearch pid={process.pid}")
    print(f"Log: {log_file}")
    return 0


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop(config: dict[str, Any]) -> int:
    status = read_json(status_path(config), default={}) or {}
    pid = status.get("pid")
    if not pid:
        print("No RelentlessResearch pid recorded.")
        return 0
    pid = int(pid)
    if not process_alive(pid):
        print(f"Recorded pid {pid} is not running.")
        write_json(status_path(config), {**status, "status": "stopped", "updated_at": utc_now()})
        return 0
    os.killpg(pid, signal.SIGTERM)
    time.sleep(2)
    if process_alive(pid):
        os.killpg(pid, signal.SIGKILL)
    write_json(status_path(config), {**status, "status": "stopped", "updated_at": utc_now()})
    print(f"Stopped RelentlessResearch pid={pid}")
    return 0


def show_status(config: dict[str, Any]) -> int:
    ensure_layout(config)
    status = read_json(status_path(config), default={}) or {"status": "not_started"}
    pid = status.get("pid")
    status["process_alive"] = bool(pid and process_alive(int(pid)))
    print(json.dumps(status, indent=2, sort_keys=True))
    if log_path(config).exists():
        print(f"\nLog: {log_path(config)}")
    print(f"Notebook: {notebook_path(config)}")
    return 0


def compact_command(config: dict[str, Any]) -> int:
    checkpoint = compact_state(config)
    if not checkpoint:
        print("No completed iterations to compact.")
        return 0
    print(f"Wrote compacted checkpoint: {checkpoint}")
    latest = read_json(state_dir(config) / "compacted" / "latest.json", default={}) or {}
    archive_end = latest.get("up_to_iteration")
    previous = latest.get("previous_checkpoint_iteration", 0)
    if archive_end:
        archive_path = state_dir(config) / "archives" / f"iterations-{int(previous) + 1:04d}-{int(archive_end):04d}.tar.gz"
        if archive_path.exists():
            print(f"Archived raw iterations: {archive_path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Persistent autonomous debugging loop for hard research problems.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("dry-run", "run", "once", "start", "stop", "status", "compact"):
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
    if args.command == "compact":
        return compact_command(config)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
