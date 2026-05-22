# Mac Training Constraints Profiler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build phase-0 tooling that maps an end-to-end training command into structured timings and names the current training bottleneck before optimization begins.

**Architecture:** Add a framework-neutral mapper script that wraps any training command, parses explicit profiling markers from output, and writes a JSON process map. Add a checker script that turns that map into a RelentlessResearch gate. Add docs and a template config so target projects can adopt the loop without changing the core runner.

**Tech Stack:** Python standard library, RelentlessResearch JSON configs, unittest.

---

### Task 1: Plan Artifact

**Files:**
- Create: `docs/superpowers/plans/2026-05-22-mac-training-constraints-profiler.md`

- [x] **Step 1: Write this implementation plan**

Save a concrete implementation plan before code changes.

- [x] **Step 2: Continue inline**

The user approved the design and asked to map this as the main project, so execute the plan in this session.

### Task 2: Mapper And Checker

**Files:**
- Create: `scripts/map_training_process.py`
- Create: `scripts/check_training_process_map.py`
- Test: `tests/test_training_process_tools.py`

- [ ] **Step 1: Add mapper tests**

Create unittest coverage for marker parsing, bottleneck selection, and JSON output shape.

- [ ] **Step 2: Implement `scripts/map_training_process.py`**

The script accepts `--command` or `--command-file`, optional `--cwd`, `--output`, `--log`, `--timeout-seconds`, and `--env KEY=VALUE`. It records command wall time and parses lines such as:

```text
[relentless-profile] step=1 phase=forward duration_ms=123.4
{"relentless_profile_event": "phase", "step": 1, "phase": "backward", "duration_ms": 456.7}
```

- [ ] **Step 3: Implement `scripts/check_training_process_map.py`**

The checker validates the map has enough step and phase evidence, then prints the current constraint. It fails when the artifact only proves coarse process wall time.

- [ ] **Step 4: Run mapper/checker tests**

Run:

```bash
python3 -m unittest tests.test_training_process_tools -v
```

Expected: all tests pass.

### Task 3: Relentless Project Docs

**Files:**
- Create: `docs/training_profile_schema.md`
- Create: `docs/mac_training_constraints_blueprint.md`
- Create: `config/mac-training-constraints.template.json`
- Modify: `README.md`

- [ ] **Step 1: Document the profile schema**

Define the required JSON fields, supported marker formats, and the meaning of `current_constraint`.

- [ ] **Step 2: Add the blueprint**

Write the Theory-of-Constraints operating notes: map first, optimize one bottleneck, re-measure, follow the moved constraint.

- [ ] **Step 3: Add the template config**

Create a copyable Relentless config that uses the mapper as validation and the checker as the phase-0 success gate.

- [ ] **Step 4: Link from README**

Add a short section pointing users at the training-constraints workflow.

### Task 4: Verification

**Files:**
- Verify all new and changed files.

- [ ] **Step 1: Compile Python scripts**

Run:

```bash
python3 -m py_compile scripts/map_training_process.py scripts/check_training_process_map.py tests/test_training_process_tools.py
```

Expected: exit code 0.

- [ ] **Step 2: Run unittest suite**

Run:

```bash
python3 -m unittest discover -s tests -v
```

Expected: exit code 0.

- [ ] **Step 3: Smoke-run mapper and checker**

Run mapper against a small inline Python command that emits profile markers, then run the checker against the generated map.

- [ ] **Step 4: Review diff**

Run:

```bash
git diff --stat
git diff --check
```

Expected: no whitespace errors.
