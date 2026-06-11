# RelentlessResearch

RelentlessResearch pursues complex, research-oriented goals by running **real
agent sessions in a persistent loop** — the "loops" idea: an outer loop that owns
the goal, gates, and memory; an inner loop that is a full agentic harness session
with tools, web access, and self-directed context.

The framework supplies what the agent must not be trusted with: a harness-owned
goal ledger with milestones, guardrail audits that revert out-of-scope edits,
validation gates the agent cannot weaken, a model supervisor that grades evidence
and steers between sessions, durable memory (research notebook + structured
hypothesis ledger), crash containment, and provenance freezing.

Two loops ship in this pack:

| Loop | Entry | Use for |
|---|---|---|
| **Goal loop (v2, default)** | `scripts/relentless.py` | Open-ended research goals: investigations, optimizations, multi-step campaigns. Output is knowledge (reports) plus code. |
| Legacy batch loop (v1) | `scripts/relentless_research.py` | One crisp correctness bug, a binary success gate, a tightly sandboxed single-completion model. See `docs/relentless-research.md`. |

## How the goal loop works

Each iteration dispatches one full agent session (Claude Code headless or Codex
CLI — subscription-billed local harnesses) on a **mission**:

- **plan** — decompose the goal into 3–7 milestones with observable acceptance
  criteria (runs first when no milestones exist, or after a `replan` verdict);
- **work** — drive the active milestone toward its acceptance criteria;
- **synthesize** — all milestones done: write `reports/final_report.md` judged
  against the goal's success criteria.

After every worker session the harness audits the workspace (reverts edits
outside `editable_globs`, resets any commits), runs the gates itself, then runs a
**read-only supervisor session** that applies the rulebook
(`docs/lessons-learned.md`): evidence discipline, drift and repetition detection,
milestone grading, and a verdict — `continue | steer | replan | fresh_session |
halt`. Steering accumulates in `supervisor_notes.md`, which both the supervisor
and humans can write and every brief includes.

Memory is durable and agent-curated: `research_notebook.md`, `hypotheses.json`
(active / weak / ruled_out / proven / stale, with shared-assumption tracking),
per-session reports, and backend session resume for warm context.

Full architecture and operating notes: `docs/relentless-goal-loop.md`.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install -r requirements.txt   # only the legacy loop needs requests

cp config/goal.template.json config/my-goal.json
# edit: workspace.path, editable_globs, goal.{objective,success_criteria,context}

python3 scripts/relentless.py dry-run --config config/my-goal.json  # brief + argv, no spend
python3 scripts/relentless.py once    --config config/my-goal.json  # one session, foreground
python3 scripts/relentless.py start   --config config/my-goal.json  # background loop
python3 scripts/relentless.py status  --config config/my-goal.json
python3 scripts/relentless.py report  --config config/my-goal.json
python3 scripts/relentless.py stop    --config config/my-goal.json
```

The worker backend is the `claude` CLI by default (uses your existing login and
the target project's CLAUDE.md/skills/MCP); `codex` and `fake` (scripted, for
tests) are also available. No API keys are needed for the default setup.

## Configuring a goal

The config is small because the agent finds its own context:

- `goal` — objective, durable context facts, graded `success_criteria`, non-goals.
- `milestones` — optional seed; leave empty to let session 1 plan.
- `workspace` — repo path + `editable_globs` (empty = read/analyze-only campaign).
- `gates` — `validation_commands` (every session), `canary` (cadence),
  `completion_commands` (before freezing). Keep gate scripts outside
  `editable_globs` so the worker cannot weaken them.
- `worker` / `supervisor` — backend, model, session timeout. Keep the supervisor
  enabled for long campaigns; it is the anti-spin mechanism.

Per-milestone `verification_commands` are for genuinely binary checks; everything
else is graded by the supervisor against acceptance criteria.

## Live steering

Edit `<state_dir>/supervisor_notes.md` while the loop runs — the next session
reads it. A `halted` status means the supervisor wants a human: read the last
`sessions/session-NNNN/verdict.json`, fix the campaign, restart.

## Design rules (unchanged from day one)

- The harness owns the gates; the model can never weaken validators or fake success.
- Evidence discipline over confidence: hypothesis statuses change only with
  command-backed evidence, and a diagnostic that shares assumptions with the
  thing under test cannot rule it out.
- Durable memory survives crashes, context resets, and model swaps.
- Stop conditions are explicit: complete (frozen with provenance), halted (human
  needed), or max sessions.

## Tests

```bash
python3 -m pytest tests/ -v
```

The goal-loop suite includes end-to-end campaigns driven by the `fake` backend —
planning → work → verification → synthesis → freeze — with zero token spend.

## Repo map

- `scripts/relentless.py` — goal loop orchestrator (v2)
- `scripts/relentless_backends.py` — agent session backends: claude, codex, fake
- `scripts/relentless_research.py` + `scripts/relentless_common.py` — legacy batch loop
- `prompts/mission_system.md` / `prompts/supervisor_system.md` — v2 contracts
- `prompts/relentless_system.md` — legacy v1 contract
- `config/goal.template.json` — v2 template (`examples/goal-demo.json` is runnable)
- `config/relentless.template.json` — legacy v1 template
- `docs/relentless-goal-loop.md` — v2 architecture and operating notes
- `docs/relentless-research.md` — legacy v1 operating notes
- `docs/lessons-learned.md` — the rulebook; wired into the supervisor
- `docs/superpowers/specs/2026-06-11-goal-loop-design.md` — v2 design rationale
- Mac training constraints workflow (profiling-first): `docs/mac_training_constraints_blueprint.md`,
  `scripts/map_training_process.py`, `scripts/check_training_process_map.py`
