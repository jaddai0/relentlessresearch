# Mac Training Constraints Blueprint

This blueprint is the durable guidance file for the main RelentlessResearch training-speed project.

## Mission

Reduce end-to-end model training time on the Mac by improving software utilization of the available hardware.

The primary metric is lower wall-clock training time, expressed as `ms_per_step` or an equivalent throughput metric. Hardware utilization counters are diagnostics, not success metrics.

## Non-Goals

- Do not count bigger batches, lower image resolution, fewer epochs, smaller models, or lower-quality settings as utilization improvements.
- Do not chase ANE, GPU, CPU, or memory utilization percentages directly.
- Do not optimize a phase until the profile map identifies it as the current constraint.
- Do not accept a model's explanation of a bottleneck without command-backed timing evidence.

## Phase 0: Map The Training Process

The first success gate is not a speedup. It is a reliable training process map.

Required artifact:

```text
.relentless-training/profile/training_process_map.json
```

The map must identify:

- whole-command wall time
- profiled training steps
- named phase timings
- phase summary statistics
- current constraint
- data quality warnings
- log artifact path
- environment and git provenance

The phase-0 checker must pass before optimization begins:

```bash
python3 /ABSOLUTE/PATH/TO/relentlessresearch/scripts/check_training_process_map.py --profile .relentless-training/profile/training_process_map.json
```

## Operating Loop

1. Map the end-to-end training process.
2. Identify the current constraint from measured phase timings.
3. Propose one software change aimed at that constraint.
4. Test the change with the same mapper and checker.
5. Compare total `ms_per_step`, phase share, and data-quality warnings.
6. If the constraint moved, update this blueprint and follow the new constraint.
7. If the constraint did not move, continue attacking the same phase or mark the hypothesis weak with evidence.

## Current Hypotheses

Seed hypotheses for new target trainers:

1. The dominant constraint is an unnecessary synchronization or evaluation barrier, not raw GPU math.
2. The dominant constraint is memory movement caused by layout conversion, cache churn, or repeated encoding.
3. The dominant constraint is an unfused or poorly batched hot kernel in forward or backward.
4. The dominant constraint is host-side scheduling, data loading, logging, checkpointing, or validation between steps.
5. ANE offload is only useful if a measured subgraph remains dominant after transfer and dispatch costs are included.

## Evidence Rules

- A successful profile has enough phase markers to name a bottleneck with at least medium confidence.
- A successful optimization reduces total training time or moves the bottleneck to a different phase.
- A utilization increase without lower wall-clock time is not a win.
- A local diagnostic that shares the same timing bug as the trainer is not an independent oracle.
- Coarse wall-clock timing alone can start instrumentation work, but it cannot justify optimization work.

## Standard Phase Markers

Training code should emit one marker per measured phase:

```text
[relentless-profile] step=1 phase=forward duration_ms=123.4
```

JSON is also accepted:

```json
{"relentless_profile_event":"phase","step":1,"phase":"backward","duration_ms":456.7}
```

Recommended phases:

- `data_load`
- `preprocess`
- `encode_cache`
- `vae_encode`
- `text_encode`
- `forward`
- `loss`
- `backward`
- `optimizer`
- `mx_eval`
- `checkpoint`
- `validation`
- `idle_gap`

## Supervisor Steering Notes

When the worker starts guessing, steer it back to the artifact:

- Which phase is the current constraint?
- How many steps were measured?
- How many distinct phases were measured?
- Did the proposed change reduce total `ms_per_step`?
- Did the bottleneck move?
- Are hardware counters explaining a measured bottleneck, or replacing the actual metric?

## Phase 1 Entry Condition

Only after phase 0 passes should the worker investigate solutions. The first phase-1 task should be constrained to the named bottleneck in `current_constraint.phase`.
