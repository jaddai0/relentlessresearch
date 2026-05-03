from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ENV_VAR_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


class RelentlessResearchError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_env_file(path: str | Path) -> dict[str, str]:
    file_path = Path(path)
    if not file_path.exists():
        raise RelentlessResearchError(f"Configured env file does not exist: {file_path}")

    values: dict[str, str] = {}
    for raw_line in file_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def config_environment(env_files: list[str]) -> dict[str, str]:
    env_values = dict(os.environ)
    for env_file in env_files:
        for key, value in parse_env_file(env_file).items():
            env_values.setdefault(key, value)
    return env_values


def resolve_env_reference(value: str | None, env_values: dict[str, str]) -> str | None:
    if value is None:
        return None
    candidate = str(value).strip()
    if not candidate:
        return candidate
    if candidate.startswith("${") and candidate.endswith("}"):
        return env_values.get(candidate[2:-1], candidate)
    if candidate.startswith("$") and ENV_VAR_NAME_PATTERN.fullmatch(candidate[1:]):
        return env_values.get(candidate[1:], candidate)
    if ENV_VAR_NAME_PATTERN.fullmatch(candidate):
        return env_values.get(candidate, candidate)
    return candidate


def read_json(path: str | Path, default: Any | None = None) -> Any:
    file_path = Path(path)
    if not file_path.exists():
        return default
    return json.loads(file_path.read_text())


def write_json(path: str | Path, data: Any) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    temp_path.replace(file_path)


def read_text(path: str | Path) -> str:
    return Path(path).read_text()


def write_text(path: str | Path, value: str) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    temp_path.write_text(value)
    temp_path.replace(file_path)


def run_shell(
    command: str,
    cwd: str | Path,
    env: dict[str, str],
    log_path: str | Path,
    timeout_seconds: int | float | None = None,
) -> dict[str, Any]:
    log_file = Path(log_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()
    with log_file.open("w") as handle:
        handle.write(f"$ {command}\n\n")
        handle.flush()
        try:
            process = subprocess.run(
                command,
                cwd=str(cwd),
                env=env,
                shell=True,
                executable="/bin/zsh",
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=timeout_seconds,
                text=True,
            )
            returncode = process.returncode
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            handle.write(f"\n[TIMEOUT after {timeout_seconds} seconds]\n{exc}\n")
            returncode = 124
            timed_out = True
    duration = time.time() - start
    return {
        "returncode": returncode,
        "duration_seconds": duration,
        "log_path": str(log_file),
        "timed_out": timed_out,
    }


def extract_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text
    reasoning_end = text.find("</reasoning>")
    if reasoning_end != -1:
        text = text[reasoning_end + len("</reasoning>"):]

    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
    if text.endswith("```"):
        text = text[:-3]

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise RelentlessResearchError("Worker model response did not contain a valid JSON object.")
