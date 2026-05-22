#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "relentless-training-process-map-v1"
MARKER_PREFIX = "[relentless-profile]"
PHASE_PATTERN = re.compile(r"[^a-z0-9_.]+")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_phase(value: object) -> str:
    text = str(value or "unknown").strip().lower()
    text = PHASE_PATTERN.sub("_", text).strip("_")
    return text or "unknown"


def coerce_float(value: object, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def coerce_int(value: object, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_key_value_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in shlex.split(text):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


def profile_event_from_payload(payload: dict[str, Any], *, line: str) -> dict[str, Any] | None:
    if "relentless_profile_event" not in payload and "phase" not in payload:
        return None

    phase = normalize_phase(payload.get("phase"))
    step = coerce_int(payload.get("step"))
    duration_ms = coerce_float(payload.get("duration_ms", payload.get("ms")))
    event_name = str(payload.get("event") or payload.get("relentless_profile_event") or "").lower()
    timestamp_ms = coerce_float(payload.get("timestamp_ms", payload.get("time_ms")))

    if duration_ms is not None:
        event_type = "phase_duration"
    elif event_name in {"start", "phase_start"}:
        event_type = "phase_start"
    elif event_name in {"end", "phase_end"}:
        event_type = "phase_end"
    elif "metric" in payload:
        event_type = "metric"
    else:
        event_type = "phase_marker"

    event: dict[str, Any] = {
        "type": event_type,
        "phase": phase,
        "step": step,
        "raw": line.rstrip("\n"),
    }
    if duration_ms is not None:
        event["duration_ms"] = duration_ms
    if timestamp_ms is not None:
        event["timestamp_ms"] = timestamp_ms
    if "metric" in payload:
        event["metric"] = str(payload.get("metric"))
        event["value"] = payload.get("value")
        event["unit"] = payload.get("unit")
    return event


def parse_profile_line(line: str) -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped:
        return None

    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return profile_event_from_payload(payload, line=line)

    if not stripped.startswith(MARKER_PREFIX):
        return None

    fields = parse_key_value_text(stripped[len(MARKER_PREFIX):].strip())
    if not fields:
        return None

    phase = normalize_phase(fields.get("phase"))
    step = coerce_int(fields.get("step"))
    duration_ms = coerce_float(fields.get("duration_ms", fields.get("ms")))
    event_name = str(fields.get("event") or "").lower()
    timestamp_ms = coerce_float(fields.get("timestamp_ms", fields.get("time_ms")))

    if duration_ms is not None:
        event_type = "phase_duration"
    elif event_name == "start":
        event_type = "phase_start"
    elif event_name == "end":
        event_type = "phase_end"
    else:
        event_type = "phase_marker"

    event = {
        "type": event_type,
        "phase": phase,
        "step": step,
        "raw": line.rstrip("\n"),
    }
    if duration_ms is not None:
        event["duration_ms"] = duration_ms
    if timestamp_ms is not None:
        event["timestamp_ms"] = timestamp_ms
    return event


def phase_stats(values: list[float]) -> dict[str, float | int]:
    total = sum(values)
    count = len(values)
    mean = total / count if count else 0.0
    return {
        "count": count,
        "total_ms": round(total, 6),
        "mean_ms": round(mean, 6),
        "min_ms": round(min(values), 6) if values else 0.0,
        "max_ms": round(max(values), 6) if values else 0.0,
    }


def complete_start_end_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    completed = list(events)
    starts: dict[tuple[int | None, str], dict[str, Any]] = {}
    for event in events:
        if event.get("type") == "phase_start":
            starts[(event.get("step"), str(event.get("phase")))] = event
            continue
        if event.get("type") != "phase_end":
            continue
        key = (event.get("step"), str(event.get("phase")))
        start_event = starts.get(key)
        start_ms = coerce_float(start_event.get("timestamp_ms") if start_event else None)
        end_ms = coerce_float(event.get("timestamp_ms"))
        if start_ms is None or end_ms is None or end_ms < start_ms:
            continue
        completed.append(
            {
                "type": "phase_duration",
                "phase": event.get("phase"),
                "step": event.get("step"),
                "duration_ms": round(end_ms - start_ms, 6),
                "raw": f"derived from phase_start/phase_end for {event.get('phase')}",
            }
        )
    return completed


def summarize_events(
    events: list[dict[str, Any]],
    *,
    command_duration_ms: float,
    min_steps: int,
    min_constraint_share: float,
) -> dict[str, Any]:
    completed_events = complete_start_end_events(events)
    duration_events = [
        event for event in completed_events
        if event.get("type") == "phase_duration" and coerce_float(event.get("duration_ms")) is not None
    ]

    phase_values: dict[str, list[float]] = {}
    steps: dict[str, dict[str, Any]] = {}
    explicit_steps: set[int] = set()

    for event in duration_events:
        phase = normalize_phase(event.get("phase"))
        duration_ms = float(event["duration_ms"])
        phase_values.setdefault(phase, []).append(duration_ms)
        step = coerce_int(event.get("step"))
        if step is not None:
            explicit_steps.add(step)
            step_key = str(step)
            step_record = steps.setdefault(step_key, {"step": step, "phases": {}, "total_measured_ms": 0.0})
            phase_record = step_record["phases"].setdefault(phase, {"count": 0, "total_ms": 0.0})
            phase_record["count"] += 1
            phase_record["total_ms"] += duration_ms
            step_record["total_measured_ms"] += duration_ms

    for step_record in steps.values():
        step_record["total_measured_ms"] = round(float(step_record["total_measured_ms"]), 6)
        for phase_record in step_record["phases"].values():
            phase_record["total_ms"] = round(float(phase_record["total_ms"]), 6)

    measured_phase_ms = sum(sum(values) for values in phase_values.values())
    phase_summary = {
        phase: {
            **phase_stats(values),
            "share_of_measured_phase_time": round((sum(values) / measured_phase_ms), 6)
            if measured_phase_ms
            else 0.0,
        }
        for phase, values in sorted(phase_values.items())
    }

    warnings: list[str] = []
    if not duration_events:
        warnings.append("No phase duration markers were observed; only whole-process timing is available.")
    if len(explicit_steps) < min_steps:
        warnings.append(f"Observed {len(explicit_steps)} explicit profiled step(s), below required {min_steps}.")
    if len(phase_summary) < 2:
        warnings.append("Fewer than two profiled phases were observed; bottleneck confidence is limited.")

    if phase_summary:
        constraint_phase = max(phase_summary.items(), key=lambda item: float(item[1]["total_ms"]))
        phase_name, stats = constraint_phase
        share = float(stats["share_of_measured_phase_time"])
        if len(explicit_steps) >= min_steps and len(phase_summary) >= 2 and share >= min_constraint_share:
            confidence = "high"
        elif len(phase_summary) >= 2:
            confidence = "medium"
        else:
            confidence = "low"
        rationale = (
            f"{phase_name} accounts for {share:.1%} of measured profiled phase time "
            f"across {int(stats['count'])} event(s)."
        )
        current_constraint = {
            "phase": phase_name,
            "total_ms": stats["total_ms"],
            "mean_ms": stats["mean_ms"],
            "share_of_measured_phase_time": share,
            "confidence": confidence,
            "rationale": rationale,
        }
    else:
        current_constraint = {
            "phase": "process",
            "total_ms": round(command_duration_ms, 6),
            "mean_ms": round(command_duration_ms, 6),
            "share_of_measured_phase_time": 1.0,
            "confidence": "low",
            "rationale": "Only whole-process timing was captured; add phase markers before optimizing.",
        }

    return {
        "events": completed_events,
        "steps": [steps[key] for key in sorted(steps, key=lambda value: int(value))],
        "phase_summary": phase_summary,
        "current_constraint": current_constraint,
        "data_quality": {
            "observed_profile_events": len(completed_events),
            "observed_phase_duration_events": len(duration_events),
            "observed_steps": len(explicit_steps),
            "observed_phases": len(phase_summary),
            "measured_phase_ms": round(measured_phase_ms, 6),
            "warnings": warnings,
        },
    }


def git_value(cwd: Path, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def parse_env_items(items: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--env must be KEY=VALUE, got: {item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"--env has empty key: {item}")
        env[key] = value
    return env


def read_command(args: argparse.Namespace) -> str:
    if args.command and args.command_file:
        raise SystemExit("Use --command or --command-file, not both.")
    if args.command:
        return str(args.command)
    if args.command_file:
        return Path(args.command_file).expanduser().read_text(encoding="utf-8").strip()
    raise SystemExit("A training command is required via --command or --command-file.")


def run_training_command(command: str, *, cwd: Path, env: dict[str, str], timeout_seconds: float | None) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            shell=True,
            executable="/bin/zsh",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )
        output = result.stdout or ""
        returncode = result.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        output += f"\n[TIMEOUT after {timeout_seconds} seconds]\n"
        returncode = 124
        timed_out = True
    duration_ms = (time.perf_counter() - start) * 1000.0
    return {
        "output": output,
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
    }


def build_profile(
    *,
    command: str,
    cwd: Path,
    result: dict[str, Any],
    log_path: Path,
    min_steps: int,
    min_constraint_share: float,
) -> dict[str, Any]:
    events = []
    for line in str(result["output"]).splitlines():
        event = parse_profile_line(line)
        if event:
            events.append(event)

    summary = summarize_events(
        events,
        command_duration_ms=float(result["duration_ms"]),
        min_steps=min_steps,
        min_constraint_share=min_constraint_share,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "command": command,
        "cwd": str(cwd),
        "returncode": int(result["returncode"]),
        "timed_out": bool(result["timed_out"]),
        "duration_ms": round(float(result["duration_ms"]), 6),
        "artifacts": {
            "log_path": str(log_path),
        },
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
            "git_commit": git_value(cwd, ["rev-parse", "HEAD"]),
            "git_branch": git_value(cwd, ["branch", "--show-current"]),
        },
        **summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Map an end-to-end training command into profiled phases.")
    parser.add_argument("--command", help="Training command to execute.")
    parser.add_argument("--command-file", help="File containing the training command to execute.")
    parser.add_argument("--cwd", default=".", help="Working directory for the training command.")
    parser.add_argument("--output", default="training_process_map.json", help="Path for the JSON profile map.")
    parser.add_argument("--log", help="Path for captured command output. Defaults beside --output.")
    parser.add_argument("--timeout-seconds", type=float, default=None, help="Optional command timeout.")
    parser.add_argument("--env", action="append", default=[], help="Extra environment value as KEY=VALUE.")
    parser.add_argument("--min-steps", type=int, default=3, help="Expected minimum profiled steps.")
    parser.add_argument(
        "--min-constraint-share",
        type=float,
        default=0.25,
        help="Phase share required for high confidence.",
    )
    parser.add_argument(
        "--allow-command-failure",
        action="store_true",
        help="Write a map and exit 0 even if the wrapped command fails.",
    )
    args = parser.parse_args()

    command = read_command(args)
    cwd = Path(args.cwd).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    log_path = Path(args.log).expanduser().resolve() if args.log else output_path.with_suffix(".log")

    env = os.environ.copy()
    env.update(parse_env_items(args.env))

    result = run_training_command(command, cwd=cwd, env=env, timeout_seconds=args.timeout_seconds)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(str(result["output"]), encoding="utf-8")

    profile = build_profile(
        command=command,
        cwd=cwd,
        result=result,
        log_path=log_path,
        min_steps=args.min_steps,
        min_constraint_share=args.min_constraint_share,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    constraint = profile["current_constraint"]
    print(
        "Training process map written to "
        f"{output_path} | current_constraint={constraint['phase']} "
        f"confidence={constraint['confidence']} returncode={profile['returncode']}"
    )
    if int(profile["returncode"]) != 0 and not args.allow_command_failure:
        return int(profile["returncode"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
