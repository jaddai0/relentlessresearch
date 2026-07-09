# RelentlessResearch v2: Goal Loop — Design

Date: 2026-06-11
Status: approved direction ("fix all of the issues you identified"); design decisions made autonomously and recorded here.

## Problem

RelentlessResearch v1 is a batch debugging harness: one blind JSON completion per
iteration, human-curated context globs, a single flat problem statement, a binary
`success_commands` finish line, and a human supervisor. It was forged on MLX
quantization-parity debugging and is good at that. It is not a research tool.

Six issues were identified against the intended purpose (pursuing complex,
research-oriented goals — the Cherny/openclaw/Codex "loops" idea):

1. The model is blind during the only part that matters — it proposes commands
   without seeing their output until the next iteration, through char-truncated tails.
2. Context is human-curated (`context_files`/`context_globs`), not model-driven.
3. There is no goal layer — no decomposition, milestones, re-planning, or task graph.
4. The supervisor is a human editing markdown, so the loop is not actually autonomous.
5. State persists but agency does not — every iteration is a cold, stateless API call.
6. Success is binary and terminal — research output (knowledge) has no `exit 0`.

## Decision: invert the architecture

Keep v1's real IP — the **outer loop** (goal persistence, gates the agent cannot
weaken, hypothesis ledger, notebook, supervisor steering, provenance freezing,
crash containment). Replace the **inner loop**: each iteration dispatches a full
agentic *session* in a real harness instead of one chat completion.

- **Inner loop = agent session.** Backends: `claude` (Claude Code headless,
  `claude -p --output-format json`, resumable via `--resume`), `codex`
  (`codex exec`, resumable via `codex exec resume`), and `fake` (scripted, for
  tests). Both real backends are subscription-billed local CLIs. The agent gets
  real tool use: it reads what it needs, reacts to command output mid-session,
  and can search the web. Fixes issues 1, 2, 5.
- **Goal layer.** `goal_state.json` holds the objective, graded success criteria,
  milestones (pending/active/done/blocked/dropped), open questions, and a findings
  index. If the config seeds no milestones, session 1 is a *planning mission* that
  proposes them. A supervisor `replan` action re-opens planning at any time.
  Fixes issue 3.
- **Model supervisor.** After every worker session, a read-only supervisor session
  grades the outcome against the rulebook (`docs/lessons-learned.md`), checks for
  drift / weak-evidence over-interpretation / repeated hypotheses / plumbing
  loops, updates milestone statuses, appends steering to `supervisor_notes.md`,
  and returns an action: `continue | steer | replan | fresh_session | halt`.
  Humans can still steer through the same files. Fixes issue 4.
- **Research output as artifact.** Every session writes a findings report to
  `reports/`. When all milestones are done, a final *synthesis mission* writes
  `reports/final_report.md`; then provenance is frozen. Milestone completion is
  graded by the supervisor, with optional per-milestone `verification_commands`
  for the parts that genuinely are binary. Fixes issue 6.

v1 (`scripts/relentless_research.py`) is retained unchanged as the legacy batch
mode — still the right tool for strict parity debugging with hard gates.

## Components

```
scripts/relentless.py           goal-loop orchestrator CLI (new main entry)
scripts/relentless_backends.py  agent session backends: claude, codex, fake
scripts/relentless_common.py    shared helpers (reused from v1)
prompts/mission_system.md       worker session epistemic contract (system prompt)
prompts/supervisor_system.md    supervisor critic contract (system prompt)
config/goal.template.json       copy-and-edit goal config
docs/relentless-goal-loop.md    operating notes
tests/test_goal_loop.py         unit tests + fake-backend end-to-end test
```

### Orchestrator flow (per session)

1. Pick mission: `planning` (no approved milestones), `work` (first
   active/pending milestone), or `synthesis` (all milestones done).
2. Build a small mission brief: goal block, milestone block, recent-session
   rollup (anti-spin), supervisor notes, last verdict steering, state-file
   *pointers* (not contents — the agent reads them itself), outcome contract,
   guardrail notice.
3. Snapshot guardrail baseline (workspace HEAD, hypotheses backup).
4. Dispatch worker session (cwd=workspace, `--add-dir` state dir, wall-clock
   timeout; resume previous backend session when allowed).
5. Post-session guardrails: if HEAD moved, soft-reset to baseline; revert tracked
   edits outside `editable_globs`; quarantine untracked files outside the globs
   into the session dir; restore `hypotheses.json` from backup if corrupted.
6. Read agent-written `sessions/session-NNNN/outcome.json` (tolerant — a missing
   or malformed outcome is recorded and the loop continues).
7. Run harness-owned gates: `validation_commands` every session; canary commands
   on cadence; the milestone's `verification_commands` when the worker proposes
   completion.
8. Supervisor session (read-only tools) → verdict JSON parsed from its final
   message; harness applies milestone updates, appends steering, honors action.
   With supervisor disabled, worker proposals apply directly when gates pass.
9. Update `goal_state.json` (harness-owned; agent edits to it are clobbered),
   `status.json`, sleep, repeat. On synthesis completion + `completion_commands`
   passing: freeze provenance, mark complete, exit 0.

### State layout (per goal)

```
<state_dir>/
  goal_state.json          harness-owned goal ledger
  research_notebook.md     agent-curated durable memory
  hypotheses.json          structured hypothesis ledger (v1 schema)
  reasoning_state.json     structured problem-state ledger: known facts,
                           unknowns, hypotheses, tests, observations, updates
  supervisor_notes.md      steering channel (supervisor + human)
  agent_session.json       backend resume id
  status.json / relentless.pid / relentless.log
  sessions/session-NNNN/   brief.md, outcome.json, result.json, verdict.json,
                           gates/, audit.json, quarantine/
  reports/                 session-NNNN.md, final_report.md
  frozen/<timestamp>/      provenance freeze on completion
```

### Contracts

`outcome.json` (worker-written): `schema, mission, summary, milestone_id,
milestone_status_proposal, proposed_milestones[], findings[], open_questions[],
reasoning_state{known_facts[], unknowns[], candidate_hypotheses[], chosen_test,
expected_observation, actual_observation, belief_update,
next_discriminating_test}, report_path, goal_complete, notes_for_next_session`.

Verdict (supervisor final message, JSON): `schema, assessment, drift_detected,
rule_violations[], milestone_updates[{id,status,reason}],
approve_proposed_milestones, steering_notes, action, halt_reason`.

### Config shape

`name, state_dir, env_files, workspace{path, editable_globs}, goal{title,
objective, context[], success_criteria[], non_goals[]}, milestones[] (optional
seed), worker{backend, model, permission_mode, allowed_tools[],
session_timeout_seconds, resume_sessions, extra_args[]}, supervisor{enabled,
backend, model, session_timeout_seconds, rulebook}, gates{validation_commands[],
canary{every_sessions, commands[]}, completion_commands[]},
loop{max_sessions, sleep_seconds, recent_session_rollup}`.

## Error handling

- Backend failure / timeout / unparseable result → recorded as a failed session;
  resume id cleared; loop continues (crash containment, as v1).
- Supervisor failure → recorded; treated as `continue` with a warning in notes.
- Gate scripts should live outside `editable_globs` so the worker cannot weaken
  them; the audit reverts attempts.

## Testing

Unit tests with no network: config defaults, goal-state transitions, mission
selection, brief content, guardrail audit on a temp git repo, outcome-parse
tolerance, backend argv construction, verdict application. One end-to-end test
drives the full loop with the `fake` backend through planning → work →
verification → synthesis → frozen completion.

## Non-goals

- No swarm orchestration in the harness itself (the worker harness can spawn its
  own subagents — that capability comes free with real agent backends).
- No web dashboard; `status` and `report` CLI subcommands suffice.
- v1 is not deleted or refactored.
