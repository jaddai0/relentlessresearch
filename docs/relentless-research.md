# RelentlessResearch — Legacy Batch Loop (v1)

> This documents the original single-completion batch loop
> (`scripts/relentless_research.py`). It remains the right tool for one crisp
> correctness bug with a binary success gate. For research-oriented goals, use
> the goal loop — see `docs/relentless-goal-loop.md`.

RelentlessResearch is a persistent autonomous debugging loop for problems that need sustained investigation rather than many isolated patch attempts.

It differs from disposable candidate frameworks in three important ways:

- One active workspace is kept across iterations instead of resetting for disposable candidates.
- The model owns the next hypothesis and experiment, while the framework owns guardrails, logging, validation, restarts, and stop conditions.
- A research notebook is updated every iteration so context survives crashes, limits, and handoffs.

## General Template

To adapt this to another hard problem:

1. Copy `config/relentless.template.json`.
2. Change `target_repo.path`, `problem`, `editable_globs`, and `context_files`.
3. Keep a small set of always-run `validation_commands`.
4. Add stricter `success_commands` that represent real completion, not shallow progress.
5. Start with one strong model and one persistent workspace.

The framework is intentionally not a swarm. Add more models only after the single-agent loop has a sharp metric and useful research state.

## Hypothesis Ledger

The runner creates:

```text
<state_dir>/hypotheses.json
```

The worker can update it through `hypothesis_updates` in its JSON response. Use it for durable, machine-readable state:

- `active`: still plausible.
- `weak`: possible, but current evidence is ambiguous or low-value.
- `ruled_out`: command-backed evidence refuted it.
- `proven`: command-backed evidence localized or fixed it.
- `stale`: older evidence may no longer apply after a meaningful code/data change.

Every update should include evidence, shared assumptions, and the next discriminating test. Do not mark a hypothesis ruled out when the diagnostic shares the same converter/runtime/preprocessing assumption as the bug being tested.

## Independent Oracles

A recurring failure mode is using an "oracle" that reuses the same broken assumption as the implementation under test. RelentlessResearch prompts now ask the worker to list `shared_assumptions` for each decisive diagnostic.

If the oracle shares the suspected layout, loader, quantization, preprocessing, prompt, or cache path, its result is useful but not decisive. Escalate to a genuinely independent reference: upstream code, another runtime, a model card, a paper, a converter diff, or a small hand-derived mathematical probe.

## External Reference Phase

Configure optional external-reference commands:

```json
"external_reference_commands": {
  "every_iterations": 0,
  "commands": []
}
```

Set `every_iterations` when you want scheduled surveys, or let the worker set `external_reference_request=true` when local diagnostics stall. Good external-reference commands inspect vendored upstream code, downloaded diffs, model cards, issue summaries, paper excerpts, or local reference implementations. Keep them read-only unless the evidence is already clear.

This phase exists because hard model/runtime bugs are often hidden in implementation-specific details that local self-consistency checks cannot reveal.

## Anti-Spin Requirement

RelentlessResearch must feed prior observations back into the next prompt. The runner does this by reading:

```text
<state_dir>/iterations/iteration-*/iteration.json
```

and injecting both recent iteration summaries and command log tails into the `Recent Command Observations` section. This is load-bearing. If that section says `None yet` after completed iterations exist, stop the loop before spending more model calls and fix the state reader.

The reusable template keeps `max_recent_iterations` set so future runs preserve enough short-term memory for the model to stop repeating diagnostics and move to the next hypothesis.

Long runs also need compaction. When `compaction.enabled` is true, the runner writes compact checkpoints under:

```text
<state_dir>/compacted/
```

and archives raw iteration folders under:

```text
<state_dir>/archives/
```

The prompt includes the latest compacted checkpoint plus only a few recent raw observations. This keeps the model from anchoring on stale failed diagnostics while preserving the raw data for audit and later manual review. You can force compaction with:

```bash
python3 scripts/relentless_research.py compact --config config/relentless.my-problem.json
```

## Crash Containment

Provider and model formatting failures are recorded as `invalid_response` iterations instead of crashing the runner. The raw response tail is fed back into the next prompt so the model can correct course without losing the research trail.

Command-policy failures are also preserved as iteration errors. The shared system prompt tells models that diagnostic commands already run from the target repo root and must not use `cd`, shell chaining, pipes, command substitution, or duplicate fixed validation gates.

Each command phase writes a progress file while it runs:

```text
<iteration_dir>/diagnostic-progress.json
<iteration_dir>/validation-progress.json
<iteration_dir>/canary-progress.json
<iteration_dir>/success-progress.json
```

Use these files to inspect long-running tests without interrupting them.

## Live Steering

The runner loads config once at process start. Restart the loop after changing config fields such as `context_files`, `problem.known_facts`, model settings, validation commands, or success commands.

Files already listed in `context_files` are read fresh each iteration. For long-running efforts, keep a stable target-repo guidance file such as `docs/relentless_problem_blueprint.md` in `context_files` and update it when a model starts repeating a false trail.

The supervisor's job is to keep the loop honest: intervene when evidence is being over-interpreted, when the worker repeats a ruled-out hypothesis, or when it spends multiple iterations repairing diagnostics without extracting new signal.

The runner also creates:

```text
<state_dir>/supervisor_notes.md
```

This file is injected late in the prompt after ordinary context. Edit it for fresh architect steering, especially when you do not want to restart the loop. If you change config, gates, or context-file lists, restart as usual.

## Canary And Sample Audits

Validators prove contracts; canaries prove behavior. Configure optional canaries:

```json
"canary_commands": {
  "every_iterations": 3,
  "commands": []
}
```

Use canaries for small unmistakable prompts, synthetic inputs, sample sanity checks, and smoke tests. They should catch:

- trivial fixtures that do not exercise the failing branch
- inputs downsampled or preprocessed into nonsense
- ambiguous or mislabeled evaluation samples
- behavior regressions that shape/numerical validators miss

If a canary fails, success checks are skipped for that iteration.

## Stop Signal

Success is defined by `success_commands`, not by model confidence. When the success commands pass and `loop.stop_on_success` is true, the runner writes a frozen diff under:

```text
<state_dir>/frozen/
```

and marks status as `succeeded`.

The frozen success directory also captures sanitized config, hypotheses, notebook, supervisor notes, and optional `provenance_commands` output. Use provenance commands to record source artifact hashes, current commit, model metadata, or benchmark summaries.

Optimization should be a separate phase with a new objective and new success gate. Do not keep a fix loop running after the success gate passes unless the config explicitly defines a post-success optimization phase.
