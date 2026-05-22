#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "relentless-training-process-map-v1"
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


def validate_profile(
    profile: dict[str, Any],
    *,
    min_steps: int,
    min_phases: int,
    min_confidence: str,
    require_success: bool,
    allow_process_only: bool,
) -> list[str]:
    errors: list[str] = []
    if profile.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")

    if require_success and int(profile.get("returncode", 1)) != 0:
        errors.append(f"training command returncode is {profile.get('returncode')}")

    if profile.get("timed_out"):
        errors.append("training command timed out")

    data_quality = profile.get("data_quality") or {}
    observed_steps = int(data_quality.get("observed_steps", 0) or 0)
    observed_phases = int(data_quality.get("observed_phases", 0) or 0)
    if observed_steps < min_steps:
        errors.append(f"observed_steps {observed_steps} is below required {min_steps}")
    if observed_phases < min_phases:
        errors.append(f"observed_phases {observed_phases} is below required {min_phases}")

    constraint = profile.get("current_constraint") or {}
    phase = str(constraint.get("phase") or "")
    if not allow_process_only and phase == "process":
        errors.append("current_constraint is only whole-process timing; add phase markers")

    confidence = str(constraint.get("confidence") or "low")
    required_rank = CONFIDENCE_ORDER.get(min_confidence, 1)
    actual_rank = CONFIDENCE_ORDER.get(confidence, -1)
    if actual_rank < required_rank:
        errors.append(f"constraint confidence {confidence!r} is below required {min_confidence!r}")

    phase_summary = profile.get("phase_summary") or {}
    if phase and phase != "process" and phase not in phase_summary:
        errors.append(f"current_constraint phase {phase!r} is missing from phase_summary")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Relentless training process map.")
    parser.add_argument("--profile", default="training_process_map.json", help="Profile JSON to validate.")
    parser.add_argument("--min-steps", type=int, default=3, help="Minimum explicit profiled steps.")
    parser.add_argument("--min-phases", type=int, default=2, help="Minimum distinct profiled phases.")
    parser.add_argument(
        "--min-confidence",
        choices=["low", "medium", "high"],
        default="medium",
        help="Minimum current constraint confidence.",
    )
    parser.add_argument("--allow-command-failure", action="store_true", help="Do not require command returncode 0.")
    parser.add_argument("--allow-process-only", action="store_true", help="Allow whole-process timing as the constraint.")
    args = parser.parse_args()

    profile_path = Path(args.profile).expanduser().resolve()
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    errors = validate_profile(
        profile,
        min_steps=args.min_steps,
        min_phases=args.min_phases,
        min_confidence=args.min_confidence,
        require_success=not args.allow_command_failure,
        allow_process_only=args.allow_process_only,
    )

    constraint = profile.get("current_constraint") or {}
    if errors:
        print(f"FAIL: {len(errors)} issue(s) blocking training process map success")
        for error in errors:
            print(f"  - {error}")
        print(f"Current constraint candidate: {constraint}")
        return 1

    print(
        "PASS: training process map identifies current constraint "
        f"{constraint.get('phase')} with confidence {constraint.get('confidence')} "
        f"({constraint.get('rationale')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
