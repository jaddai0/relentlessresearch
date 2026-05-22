from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


mapper = load_script("map_training_process")
checker = load_script("check_training_process_map")


class TrainingProcessToolTests(unittest.TestCase):
    def test_parse_text_marker_duration(self) -> None:
        event = mapper.parse_profile_line(
            "[relentless-profile] step=2 phase=backward duration_ms=42.5"
        )

        self.assertIsNotNone(event)
        self.assertEqual(event["type"], "phase_duration")
        self.assertEqual(event["step"], 2)
        self.assertEqual(event["phase"], "backward")
        self.assertEqual(event["duration_ms"], 42.5)

    def test_parse_json_marker_duration(self) -> None:
        line = json.dumps(
            {
                "relentless_profile_event": "phase",
                "step": 1,
                "phase": "optimizer step",
                "duration_ms": 12.75,
            }
        )
        event = mapper.parse_profile_line(line)

        self.assertIsNotNone(event)
        self.assertEqual(event["type"], "phase_duration")
        self.assertEqual(event["phase"], "optimizer_step")
        self.assertEqual(event["duration_ms"], 12.75)

    def test_summarize_events_identifies_constraint(self) -> None:
        events = []
        for step in range(1, 4):
            events.extend(
                [
                    {"type": "phase_duration", "step": step, "phase": "data_load", "duration_ms": 10.0},
                    {"type": "phase_duration", "step": step, "phase": "forward", "duration_ms": 100.0},
                    {"type": "phase_duration", "step": step, "phase": "backward", "duration_ms": 300.0},
                    {"type": "phase_duration", "step": step, "phase": "optimizer", "duration_ms": 50.0},
                ]
            )

        summary = mapper.summarize_events(
            events,
            command_duration_ms=1500.0,
            min_steps=3,
            min_constraint_share=0.25,
        )

        self.assertEqual(summary["current_constraint"]["phase"], "backward")
        self.assertEqual(summary["current_constraint"]["confidence"], "high")
        self.assertEqual(summary["data_quality"]["observed_steps"], 3)

    def test_checker_rejects_process_only_profile(self) -> None:
        profile = {
            "schema_version": checker.SCHEMA_VERSION,
            "returncode": 0,
            "timed_out": False,
            "phase_summary": {},
            "current_constraint": {"phase": "process", "confidence": "low"},
            "data_quality": {"observed_steps": 0, "observed_phases": 0},
        }

        errors = checker.validate_profile(
            profile,
            min_steps=3,
            min_phases=2,
            min_confidence="medium",
            require_success=True,
            allow_process_only=False,
        )

        self.assertGreaterEqual(len(errors), 3)

    def test_mapper_writes_profile_for_marker_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "profile.json"
            command = (
                "python3 -c \""
                "print('[relentless-profile] step=1 phase=forward duration_ms=100');"
                "print('[relentless-profile] step=1 phase=backward duration_ms=250');"
                "print('[relentless-profile] step=2 phase=forward duration_ms=110');"
                "print('[relentless-profile] step=2 phase=backward duration_ms=260');"
                "print('[relentless-profile] step=3 phase=forward duration_ms=105');"
                "print('[relentless-profile] step=3 phase=backward duration_ms=255')"
                "\""
            )
            result = mapper.run_training_command(
                command,
                cwd=ROOT,
                env=os.environ.copy(),
                timeout_seconds=30,
            )
            profile = mapper.build_profile(
                command=command,
                cwd=ROOT,
                result=result,
                log_path=output_path.with_suffix(".log"),
                min_steps=3,
                min_constraint_share=0.25,
            )

            output_path.write_text(json.dumps(profile), encoding="utf-8")
            self.assertEqual(profile["current_constraint"]["phase"], "backward")
            self.assertEqual(profile["data_quality"]["observed_steps"], 3)


if __name__ == "__main__":
    unittest.main()
