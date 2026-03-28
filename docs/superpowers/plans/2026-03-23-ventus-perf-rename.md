# Ventus Perf Rename Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the user-facing Ventus perf script to `tools/ventus-perf.py`, update user-visible references, and remove the unused repo-root import shim.

**Architecture:** Keep the Python implementation package stable as `ventus_perf` under `tools/ventus_perf/`, and change only the user-facing script path plus references that surface to users or tests. Remove the root shim once the new direct execution path and tests prove it is unnecessary.

**Tech Stack:** Python 3 stdlib (`unittest`, `subprocess`, `pathlib`), Markdown docs

---

### Task 1: Lock rename behavior with tests

**Files:**
- Modify: `tools/ventus_perf/tests/test_wrap.py`
- Modify: `tools/ventus_perf/tests/test_report.py`

- [ ] **Step 1: Write failing tests**

Change CLI subprocess invocations to `tools/ventus-perf.py` and remove expectations that rely on the repo-root shim.

- [ ] **Step 2: Run tests to verify they fail**

Run: `timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_*.py'`

Expected: failures because the new entrypoint path does not exist yet and repo-root import assumptions still hold.

### Task 2: Implement the rename

**Files:**
- Move: `tools/ventus_perf.py` -> `tools/ventus-perf.py`
- Modify: `tools/ventus-perf.py`
- Modify: `tools/ventus_perf/cli.py`
- Delete: `ventus_perf/__init__.py`

- [ ] **Step 1: Create the new entrypoint path**

Rename the script and update its maintenance comments to point at the new user-facing name.

- [ ] **Step 2: Update CLI help text**

Change examples, `prog`, and error prefixes to use `ventus-perf.py`.

- [ ] **Step 3: Remove the unused root shim**

Delete `ventus_perf/__init__.py` once tests prove direct execution and package imports still work from the repository root.

### Task 3: Sync docs and verify

**Files:**
- Modify: `README.md`
- Modify: recent spec/plan docs that mention the old path

- [ ] **Step 1: Update user-visible docs**

Replace `tools/ventus_perf.py` with `tools/ventus-perf.py` where it describes user invocation.

- [ ] **Step 2: Run verification**

Run: `timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_*.py'`

Expected: PASS
