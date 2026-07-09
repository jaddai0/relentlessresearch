# RelentlessResearch Problem Blueprint

Copy this file into the target repo and tailor it to the current problem. Add it to `context_files` so every prompt sees it.

This file is for durable human guidance:

- What success really means.
- What has already been ruled out.
- What diagnostics are low-value repeats.
- What command patterns fail under policy.
- What technical facts are easy for models to misread.
- What the next highest-value experiments are.
- Where the supervising architect should intervene if the worker starts drifting.

The model may update the research notebook, but this blueprint should stay comparatively stable. If a running loop starts chasing a false trail, update this file and restart the loop so the loaded config includes it.

## Success Gate

Describe the real completion check. The run is not solved until the `success_commands` pass.

## Durable Facts

- Add paths, invariants, and current known-good behavior.
- Add facts that should override misleading observations unless directly disproven.
- Include any equations, data layouts, protocols, or API contracts that models are likely to misread.
- State which diagnostics are independent references and which reuse local implementation assumptions.

## Ruled Out

- List failed hypotheses and why they failed.
- List diagnostics that should not be repeated without a changed premise.
- List any diagnostic scripts that can produce false-positive summaries and require stricter pass/fail counters.
- Record the exact condition required before a ruled-out hypothesis may be retried.

## Current Best Hypotheses

1. Replace with the most likely root cause.
2. Replace with the second most likely root cause.
3. Replace with an escape-hatch hypothesis if the first two fail.

Mirror these in `<state_dir>/hypotheses.json` so the framework can carry status, evidence, shared assumptions, and next discriminating tests across compaction.

## Preferred Experiments

Use diagnostics that localize the failure before broad rewrites.

1. Replace with the next highest-information command or script.
2. Replace with a narrow component-level parity test.
3. Replace with a small reversible patch only after a diagnostic supports it.

## Reasoning State

Keep `<state_dir>/reasoning_state.json` sharp enough that a fresh worker can
resume without guessing:

- known facts: supportable facts, not impressions
- unknowns: questions that still change the next decision
- candidate hypotheses: claims with support and a discriminating test
- chosen test: the current highest-information action
- observation: what actually happened this session
- belief update: what changed because of that observation
- next discriminating test: the next move if the goal is not complete

## Independent Oracle Plan

- List independent implementations, forks, issue threads, papers, model cards, or reference scripts worth consulting.
- List local diagnostics that are useful but not independent because they share loader/layout/quantization/preprocessing assumptions.
- Define when to trigger `external_reference_request=true`.

## Canary And Sample Audit Plan

- Define tiny unmistakable behavior checks that should pass before success can be trusted.
- Include at least one synthetic/control sample for each input modality when the task is multimodal.
- Record preprocessing settings and sample provenance. Avoid tiny, all-zero, ambiguous, mislabeled, or over-compressed fixtures as sole proof.

## Role Split

- Worker: implement or test one bounded hypothesis.
- Architect: periodically review the ledger and decide whether to steer, compact, or trigger external-reference work.
- Verifier: inspect final evidence and make sure canaries, validators, and success gates all exercise the production path.

## Supervisor Steering Notes

- Add notes here when the worker model is repeating itself, misreading evidence, or spending too long on diagnostic plumbing.
- Include interpretation warnings for ambiguous metrics.
- Note when a diagnostic input is too trivial to exercise the suspected bug.
- Note when a component should be deprioritized because outputs match on a stronger behavioral metric.
- Keep these notes short, concrete, and tied to command output.

## Loop Operations

- The runner loads config once when started. If you change `context_files`, `problem.known_facts`, `worker_api`, validation gates, or success gates, restart the loop.
- If you only edit files already listed in `context_files`, the next prompt will see the updated file content.
- Long runs should use compaction. Keep `compaction.enabled` on unless there is a specific reason not to; compacted checkpoints preserve raw evidence while keeping future prompts focused.
- If the model starts anchoring on stale failed diagnostics, force a checkpoint with `python3 scripts/relentless_research.py compact --config config/relentless.my-problem.json`, then restart with a smaller recent-iteration window and a focused blueprint.
- If the notebook was overwritten with a narrower or misleading summary, restore the durable facts here and in the notebook before continuing.
- Comparison diagnostics should report attempted, successful, failed, and skipped comparisons. They should fail when no useful comparison actually ran.
