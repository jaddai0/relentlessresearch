# Training Profile Schema

This document defines the phase-0 artifact for the Mac training constraints workflow.

The goal of the artifact is to answer one question before any optimization work starts:

> Which measured phase currently constrains end-to-end training speed?

The answer must come from data, not model confidence.

## Artifact

The mapper writes a JSON file, usually:

```text
.relentless-training/profile/training_process_map.json
```

The top-level schema version is:

```json
{
  "schema_version": "relentless-training-process-map-v1"
}
```

## Required Fields

```json
{
  "schema_version": "relentless-training-process-map-v1",
  "created_at": "2026-05-22T00:00:00+00:00",
  "command": "python3 train.py --profile",
  "cwd": "/absolute/path/to/training-repo",
  "returncode": 0,
  "timed_out": false,
  "duration_ms": 12345.0,
  "artifacts": {
    "log_path": "/absolute/path/to/training_process_map.log"
  },
  "environment": {
    "platform": "...",
    "machine": "arm64",
    "python_version": "...",
    "python_executable": "...",
    "git_commit": "...",
    "git_branch": "..."
  },
  "events": [],
  "steps": [],
  "phase_summary": {},
  "current_constraint": {},
  "data_quality": {}
}
```

## Profile Markers

The mapper always records whole-command wall time. To identify real bottlenecks, the training command must emit explicit phase markers.

Text marker:

```text
[relentless-profile] step=1 phase=forward duration_ms=123.4
```

JSON marker:

```json
{"relentless_profile_event":"phase","step":1,"phase":"backward","duration_ms":456.7}
```

Start/end markers are supported when they include comparable timestamps:

```text
[relentless-profile] step=1 phase=optimizer event=start timestamp_ms=1000.0
[relentless-profile] step=1 phase=optimizer event=end timestamp_ms=1042.5
```

Duration markers are preferred because they avoid clock-source ambiguity.

## Recommended Phase Names

Use stable snake_case names so results can be compared across runs:

- `data_load`
- `caption_load`
- `image_decode`
- `preprocess`
- `encode_cache`
- `vae_encode`
- `text_encode`
- `forward`
- `loss`
- `backward`
- `gradient_sync`
- `optimizer`
- `ema`
- `mx_eval`
- `cache_clear`
- `checkpoint`
- `validation`
- `idle_gap`

Target-specific phases are allowed. Keep names stable once the first baseline is recorded.

## Current Constraint

`current_constraint` names the largest measured phase by total profiled phase time:

```json
{
  "phase": "backward",
  "total_ms": 900.0,
  "mean_ms": 300.0,
  "share_of_measured_phase_time": 0.652174,
  "confidence": "high",
  "rationale": "backward accounts for 65.2% of measured profiled phase time across 3 event(s)."
}
```

Confidence levels:

- `high`: enough explicit steps, at least two phases, and the leading phase is a clear share of measured phase time.
- `medium`: enough phase diversity to name a candidate, but not enough evidence for a strong gate.
- `low`: only whole-process timing, too few phases, too few steps, or ambiguous evidence.

## Data Quality Gate

The phase-0 success gate should usually require:

- command return code `0`
- no timeout
- at least `3` explicit profiled steps
- at least `2` distinct profiled phases
- current constraint confidence at least `medium`
- current constraint phase is not `process`

If the checker fails, do not optimize yet. Improve instrumentation first.

## Theory Of Constraints Rule

After a profile passes the phase-0 gate, the next RelentlessResearch iteration should target only the named current constraint.

If an optimization lowers total `ms_per_step` but leaves the same phase dominant, keep working that phase. If an optimization moves the bottleneck, update the blueprint and follow the new constraint.
