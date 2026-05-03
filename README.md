# RelentlessResearch

RelentlessResearch is a copyable, single-workspace autonomous debugging loop for hard research problems.

It is built for cases where a strong model should keep investigating until a real success gate passes, while the framework supplies memory, guardrails, repeatable validation, crash containment, and a clean stop signal.

## What You Get

- `scripts/relentless_research.py`: the runner.
- `scripts/relentless_common.py`: the small dependency shim used by the runner.
- `config/relentless.template.json`: copy-and-edit configuration.
- `prompts/relentless_system.md`: model contract and JSON schema.
- `docs/relentless-research.md`: operating notes and anti-spin rules.
- `docs/relentless_problem_blueprint.md`: copy into the target repo for stable human guidance.
- `docs/lessons-learned.md`: framework lessons from real runs.
- `examples/replace_with_real_success_gate.py`: tiny example success gate.
- `<state_dir>/hypotheses.json`: a structured hypothesis ledger created by the runner.
- `<state_dir>/supervisor_notes.md`: fresh architect steering injected late in every prompt.

## Install

Copy this directory anywhere. From the copied directory:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
cp config/relentless.template.json config/relentless.my-problem.json
```

Put your model API key in an env file, for example:

```text
OPENROUTER_API_KEY=replace-with-your-key
```

Then edit `config/relentless.my-problem.json`:

- `env_files`: absolute path to your env file.
- `target_repo.path`: absolute path to the repo the model may inspect and edit.
- `problem`: the durable facts, objective, and primary strategy.
- `editable_globs`: the only files the model may edit.
- `context_files` and `context_globs`: the context included in each prompt.
- `validation_commands`: cheap checks that run every iteration.
- `canary_commands`: optional behavior/sample checks that run periodically and before success checks.
- `success_commands`: the real finish line.
- `external_reference_commands`: optional read-only surveys of upstream implementations, forks, papers, model cards, issue threads, or vendored reference code.
- `provenance_commands`: optional commands captured when a success artifact is frozen.

Put a tailored `docs/relentless_problem_blueprint.md` in the target repo and keep it in `context_files`. Use it for durable facts, ruled-out hypotheses, and traps the model keeps falling into.

## Commands

Dry-run without spending model credits:

```bash
python3 scripts/relentless_research.py dry-run --config config/relentless.my-problem.json
```

Run one iteration in the foreground:

```bash
python3 scripts/relentless_research.py once --config config/relentless.my-problem.json
```

Start a background loop:

```bash
python3 scripts/relentless_research.py start --config config/relentless.my-problem.json
```

Check or stop it:

```bash
python3 scripts/relentless_research.py status --config config/relentless.my-problem.json
python3 scripts/relentless_research.py stop --config config/relentless.my-problem.json
```

Manually compact raw iteration history into a checkpoint:

```bash
python3 scripts/relentless_research.py compact --config config/relentless.my-problem.json
```

Watch logs:

```bash
tail -f .relentless-TEMPLATE/relentless.log
```

## Design Rules

- Keep one persistent workspace so useful partial work accumulates.
- Put the real quality bar in `success_commands`, not in prose.
- Put quick behavior canaries in `canary_commands` so shallow validators cannot declare victory alone.
- Keep validation strict. Never let the model weaken validators or fake success.
- Feed command observations back into the next prompt.
- Use the hypothesis ledger to mark hypotheses active, weak, stale, ruled out, or proven. A hypothesis is not ruled out if the diagnostic shared the same assumption as the suspected bug.
- When the loop stalls, trigger an external-reference survey instead of repeatedly testing the local implementation against itself.
- Compact long runs regularly so the model sees a distilled checkpoint plus a few fresh observations instead of a giant pile of stale diagnostics.
- Record invalid model responses instead of crashing the loop.
- Stop on success, freeze the artifact plus provenance, then optimize in a separate phase.

## Copy Checklist

1. Copy this pack.
2. Create a project-specific config.
3. Set a real success gate.
4. Run `dry-run`.
5. Run `once`.
6. Inspect the first iteration.
7. Start the background loop only after the first iteration looks sane.

## Live Steering

The runner loads config once at process start. Restart the loop after changing `context_files`, `problem.known_facts`, model settings, validation commands, or success commands.

You can edit files that are already listed in `context_files` while the loop runs; the next prompt will read their latest contents. This is the safest way to add cheat sheets and blueprints mid-run.

You can also edit `<state_dir>/supervisor_notes.md` while the loop runs. It is injected after ordinary context, making it the intended place for the supervising architect to steer the worker without restarting.

The intended human/Codex role is supervising architect: watch for drift, weak evidence being over-interpreted, repeated failed hypotheses, and diagnostic-plumbing loops. When that happens, update the problem blueprint with concrete guidance instead of letting the worker burn iterations.

## Hindsight Features

These are built into the template because they prevented real wasted motion:

- Independent-oracle checks: the worker must state shared assumptions for decisive diagnostics.
- External-reference phase: configure read-only commands that inspect independent implementations when local evidence stalls.
- Structured hypothesis ledger: preserve status, evidence, shared assumptions, and next discriminating tests.
- Stronger fixtures/canaries: require nontrivial behavior checks outside narrow validators.
- Sample audits: verify that evaluation examples are not too small, ambiguous, mislabeled, or preprocessed into nonsense.
- Streaming command progress: each phase writes `<phase>-progress.json` while commands run.
- Frozen provenance: success freezes config, notebook, supervisor notes, hypotheses, diff, and optional provenance command output.
