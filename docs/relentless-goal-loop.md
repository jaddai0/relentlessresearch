# The Goal Loop (v2)

The goal loop pursues open-ended research goals by running **real agent sessions**
in a loop, with the harness owning everything the agent must not be trusted with:
the goal ledger, gates, guardrail audits, supervision, and provenance.

```
┌─────────────────────────── outer loop (scripts/relentless.py) ───────────────────────────┐
│                                                                                          │
│  pick mission ─► build brief ─► WORKER SESSION ─► guardrail audit ─► harness gates ─►    │
│  (plan/work/      (small,        (full agency:      (revert out-of-    (validation,      │
│   synthesize)      pointers       tools, web,        scope edits,       canary, milestone│
│                    not dumps)     subagents)         reset commits)     verification)    │
│                                                                                          │
│  ─► SUPERVISOR SESSION ─► apply verdict ─► update goal state ─► repeat / freeze          │
│      (read-only critic       (milestones,                                                │
│       with the rulebook)      steering, action)                                          │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

## Division of labor

| Layer | Owns |
|---|---|
| Worker session (claude/codex) | Exploration, experiments, edits, evidence, notebook + hypothesis ledger updates, session report, outcome proposal |
| Supervisor session (read-only) | Evidence discipline, drift/repetition detection, milestone grading, plan health, steering notes, continue/steer/replan/fresh_session/halt |
| Harness (this repo) | Goal ledger, mission selection, briefs, guardrail audit, gates, verdict application, provenance freeze, crash containment, daemon |
| Human | Writes the goal config; reads reports; may steer any time via `supervisor_notes.md`; handles `halted` |

## Missions

- **plan** — no approved milestones (or a `replan` verdict): the worker explores
  and proposes 3–7 milestones with observable acceptance criteria. The supervisor
  approves or rejects them.
- **work** — drive the first active/pending milestone toward its acceptance
  criteria; propose `done` or `blocked` in the outcome file.
- **synthesize** — all milestones done/dropped: write `reports/final_report.md`
  judged against the goal's success criteria, then set `goal_complete`.

Milestone completion is graded: the supervisor judges acceptance criteria;
per-milestone `verification_commands` (when defined) are run by the harness and
are necessary but not sufficient. With the supervisor disabled, worker proposals
apply directly, still gated by verification commands.

## Guardrails (how the agent gets freedom safely)

The worker runs with full tools in the workspace (`cwd`) plus the state dir
(`--add-dir`). After every session the harness:

1. soft-resets any commits the agent made back to the baseline HEAD (work is
   preserved in the working tree);
2. reverts tracked edits outside `workspace.editable_globs` and quarantines
   untracked files outside them into the session dir;
3. restores `hypotheses.json` from backup if the agent corrupted it;
4. runs validation/canary gates itself — gate results come from the harness, so
   the worker cannot fake them. Keep gate scripts outside `editable_globs`.

Empty `editable_globs` means a read/analyze-only campaign: every workspace edit
is reverted.

## State dir anatomy

```
.relentless-<name>/
  goal_state.json        harness-owned: objective, milestones, findings, status
  research_notebook.md   agent-curated durable memory
  hypotheses.json        structured hypothesis ledger (active/weak/ruled_out/proven/stale)
  supervisor_notes.md    steering channel — supervisor appends, humans may too
  agent_session.json     backend resume id (continuity across sessions)
  sessions/session-NNNN/ brief.md, outcome.json, result.json, verdict.json,
                         audit.json, gates/, quarantine/
  reports/               session-NNNN.md per session, final_report.md at the end
  frozen/<timestamp>/    provenance freeze on completion
```

## Commands

```bash
python3 scripts/relentless.py dry-run --config config/my-goal.json   # brief + argv, no spend
python3 scripts/relentless.py once    --config config/my-goal.json   # one session, foreground
python3 scripts/relentless.py start   --config config/my-goal.json   # background loop
python3 scripts/relentless.py status  --config config/my-goal.json
python3 scripts/relentless.py report  --config config/my-goal.json   # latest/final report
python3 scripts/relentless.py stop    --config config/my-goal.json
```

## Live steering

- Edit `supervisor_notes.md` any time — the next brief includes its tail, and the
  worker treats it as the freshest guidance.
- Config changes (gates, models, globs) need a loop restart, same as v1.
- A `halted` status means the supervisor wants a human: read the last
  `verdict.json` and the supervisor notes, fix the campaign, restart.

## Session continuity

When `worker.resume_sessions` is true (default) the next worker session resumes
the previous backend session, keeping explored context warm. The supervisor's
`fresh_session` action — or any failed session — clears the resume id so the next
session starts cold from the durable state (notebook, ledger, reports). That
durable state is the real memory; resume is just an optimization.

## Choosing backends and models

- `worker.backend: "claude"` (default) — Claude Code headless; inherits the
  target project's CLAUDE.md, skills, and MCP servers. `model`: `opus` for hard
  research, `sonnet` for routine campaigns.
- `worker.backend: "codex"` — Codex CLI; useful as a second opinion harness or
  when a campaign should run on the Codex subscription.
- `worker.backend: "fake"` — scripted sessions for tests and rehearsal
  (`worker.fake_script`).
- The supervisor is read-only and cheaper per session; it can usually run one
  model tier below the worker, but never disable it for long campaigns — it is
  the anti-spin mechanism.

## The rulebook

`docs/lessons-learned.md` is wired into the supervisor as its rulebook
(`supervisor.rulebook`). It encodes the failure modes that cost real iterations:
shared-assumption oracles, trivial fixtures, optimistic diagnostics,
diagnostic-plumbing loops, dropped durable facts. Keep adding to it; every
campaign inherits it.

## Relationship to the legacy batch loop

`scripts/relentless_research.py` (see `docs/relentless-research.md`) remains the
right tool for one crisp correctness bug with a binary gate and a tightly
sandboxed model. The goal loop is for everything research-shaped: questions,
investigations, optimizations, multi-step campaigns where the output is
knowledge plus code, not just a passing gate.
