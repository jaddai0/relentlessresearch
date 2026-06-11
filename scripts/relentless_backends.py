"""Agent session backends for the RelentlessResearch goal loop.

Each backend runs ONE full agentic session in a real harness (Claude Code
headless or Codex exec) and returns the final message plus a resumable session
id when the harness provides one. The `fake` backend replays scripted sessions
for tests and dry verification without spending tokens.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from relentless_common import read_json, write_json, write_text

READONLY_TOOLS = ["Read", "Grep", "Glob"]


@dataclass
class SessionRequest:
    prompt: str
    cwd: str
    add_dirs: list[str] = field(default_factory=list)
    system_prompt: str | None = None
    model: str | None = None
    timeout_seconds: int = 3600
    resume_id: str | None = None
    readonly: bool = False
    permission_mode: str = "acceptEdits"
    allowed_tools: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    session_dir: str | None = None
    state_dir: str | None = None


@dataclass
class SessionResult:
    ok: bool
    text: str
    session_id: str | None
    duration_seconds: float
    error: str | None = None
    raw_path: str | None = None


class Backend:
    name = "backend"

    def build_argv(self, request: SessionRequest) -> list[str]:
        raise NotImplementedError

    def run(self, request: SessionRequest) -> SessionResult:
        raise NotImplementedError

    def _execute(self, argv: list[str], request: SessionRequest) -> tuple[int, str, str, float, str | None]:
        start = time.time()
        try:
            process = subprocess.run(
                argv,
                cwd=request.cwd,
                env=request.env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=request.timeout_seconds,
            )
            return process.returncode, process.stdout, process.stderr, time.time() - start, None
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
            stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
            return 124, stdout, stderr, time.time() - start, f"session timed out after {request.timeout_seconds}s"
        except FileNotFoundError as exc:
            return 127, "", str(exc), time.time() - start, f"backend binary not found: {argv[0]}"

    def _write_raw(self, request: SessionRequest, payload: dict[str, Any]) -> str | None:
        if not request.session_dir:
            return None
        raw_path = Path(request.session_dir) / f"{self.name}-raw.json"
        write_json(raw_path, payload)
        return str(raw_path)


class ClaudeBackend(Backend):
    """Claude Code headless: `claude -p --output-format json`."""

    name = "claude"

    def build_argv(self, request: SessionRequest) -> list[str]:
        argv = ["claude", "-p", request.prompt, "--output-format", "json"]
        if request.model:
            argv += ["--model", request.model]
        if request.resume_id:
            argv += ["--resume", request.resume_id]
        if request.system_prompt:
            argv += ["--append-system-prompt", request.system_prompt]
        for directory in request.add_dirs:
            argv += ["--add-dir", directory]
        if request.readonly:
            argv += ["--tools", ",".join(READONLY_TOOLS), "--permission-mode", "default"]
        else:
            argv += ["--permission-mode", request.permission_mode]
            if request.allowed_tools:
                argv += ["--allowedTools", ",".join(request.allowed_tools)]
        argv += list(request.extra_args)
        return argv

    def run(self, request: SessionRequest) -> SessionResult:
        argv = self.build_argv(request)
        returncode, stdout, stderr, duration, error = self._execute(argv, request)
        payload: dict[str, Any] = {
            "argv_head": argv[:1] + ["<prompt omitted>"] + argv[3:],
            "returncode": returncode,
            "stderr_tail": stderr[-4000:],
        }
        text = stdout
        session_id = None
        if error is None:
            try:
                data = json.loads(stdout)
                payload["result_meta"] = {key: data.get(key) for key in ("subtype", "is_error", "num_turns", "total_cost_usd", "session_id")}
                text = str(data.get("result") or "")
                session_id = data.get("session_id")
                if returncode != 0 or data.get("is_error"):
                    error = f"claude session reported failure (subtype={data.get('subtype')}, rc={returncode})"
            except (json.JSONDecodeError, TypeError):
                payload["stdout_tail"] = stdout[-4000:]
                if returncode != 0:
                    error = f"claude exited rc={returncode} with unparseable output"
        raw_path = self._write_raw(request, payload)
        return SessionResult(
            ok=error is None,
            text=text,
            session_id=session_id,
            duration_seconds=duration,
            error=error,
            raw_path=raw_path,
        )


class CodexBackend(Backend):
    """Codex CLI non-interactive: `codex exec --json -o <file>`."""

    name = "codex"

    def __init__(self) -> None:
        self._last_message_file: Path | None = None

    def build_argv(self, request: SessionRequest) -> list[str]:
        session_dir = Path(request.session_dir or request.cwd)
        self._last_message_file = session_dir / "codex-last-message.txt"
        argv = [
            "codex",
            "exec",
            "--json",
            "-o",
            str(self._last_message_file),
            "-C",
            request.cwd,
            "--skip-git-repo-check",
            "--sandbox",
            "read-only" if request.readonly else "workspace-write",
        ]
        if request.model:
            argv += ["-m", request.model]
        for directory in request.add_dirs:
            argv += ["--add-dir", directory]
        argv += list(request.extra_args)
        if request.resume_id:
            argv += ["resume", request.resume_id]
        prompt = request.prompt
        if request.system_prompt:
            prompt = f"{request.system_prompt}\n\n---\n\n{prompt}"
        argv.append(prompt)
        return argv

    def run(self, request: SessionRequest) -> SessionResult:
        argv = self.build_argv(request)
        returncode, stdout, stderr, duration, error = self._execute(argv, request)
        session_id = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = event.get("msg") if isinstance(event.get("msg"), dict) else {}
            for key in ("session_id", "thread_id"):
                value = event.get(key) or message.get(key)
                if isinstance(value, str) and value:
                    session_id = session_id or value
        text = ""
        if self._last_message_file and self._last_message_file.exists():
            text = self._last_message_file.read_text(errors="replace")
        if error is None and returncode != 0:
            error = f"codex exited rc={returncode}: {stderr[-1000:]}"
        raw_path = self._write_raw(
            request,
            {"returncode": returncode, "stderr_tail": stderr[-4000:], "stdout_tail": stdout[-8000:]},
        )
        return SessionResult(
            ok=error is None,
            text=text,
            session_id=session_id,
            duration_seconds=duration,
            error=error,
            raw_path=raw_path,
        )


class FakeBackend(Backend):
    """Replays scripted sessions from a JSON file. For tests and rehearsal.

    The script file holds a list of entries:
        {"text": "...", "ok": true, "session_id": "fake-1",
         "files": {"{session_dir}/outcome.json": "<content>", ...}}
    Placeholders {session_dir}, {state_dir}, {workspace} are substituted at run
    time. A cursor file in the state dir makes replay survive restarts.
    """

    name = "fake"

    def __init__(self, script_path: str) -> None:
        self.script_path = Path(script_path)

    def _cursor_path(self, request: SessionRequest) -> Path:
        base = Path(request.state_dir or self.script_path.parent)
        return base / f"fake-cursor-{self.script_path.stem}.json"

    def build_argv(self, request: SessionRequest) -> list[str]:
        return ["fake", str(self.script_path)]

    def run(self, request: SessionRequest) -> SessionResult:
        entries = read_json(self.script_path, default=[]) or []
        cursor_path = self._cursor_path(request)
        cursor = int((read_json(cursor_path, default={}) or {}).get("next", 0))
        if cursor >= len(entries):
            return SessionResult(ok=False, text="", session_id=None, duration_seconds=0.0, error="fake script exhausted")
        entry = entries[cursor]
        write_json(cursor_path, {"next": cursor + 1})
        substitutions = {
            "{session_dir}": request.session_dir or "",
            "{state_dir}": request.state_dir or "",
            "{workspace}": request.cwd,
        }

        def substitute(value: str) -> str:
            for token, replacement in substitutions.items():
                value = value.replace(token, replacement)
            return value

        for raw_path, content in (entry.get("files") or {}).items():
            write_text(Path(substitute(raw_path)), substitute(str(content)))
        return SessionResult(
            ok=bool(entry.get("ok", True)),
            text=substitute(str(entry.get("text", ""))),
            session_id=entry.get("session_id"),
            duration_seconds=0.0,
            error=entry.get("error"),
        )


def make_backend(settings: dict[str, Any]) -> Backend:
    name = str(settings.get("backend", "claude")).lower()
    if name == "claude":
        return ClaudeBackend()
    if name == "codex":
        return CodexBackend()
    if name == "fake":
        script = settings.get("fake_script")
        if not script:
            raise ValueError("fake backend requires worker/supervisor 'fake_script' path")
        return FakeBackend(str(script))
    raise ValueError(f"Unknown agent backend: {name}")
