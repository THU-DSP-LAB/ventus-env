# Ventus Perf CLI UX Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `tools/ventus-perf.py` easier to use by adding better help output, friendlier CLI errors, and a driver-consistent default backend.

**Architecture:** Keep all behavior changes inside the Python CLI/runtime bootstrap layer. The wrapper/report modules continue to own perf execution and report generation; the CLI layer becomes responsible for presenting clearer defaults, usage text, and expected-error rendering.

**Tech Stack:** Python 3 stdlib (`argparse`, `pathlib`, `subprocess`, `unittest`)

---

### Task 1: Lock the desired CLI behavior with tests

**Files:**
- Modify: `tools/ventus_perf/tests/test_wrap.py`

- [ ] **Step 1: Write failing tests**

Add tests for:
- default runtime backend becomes `spike` when unset
- top-level `--help` includes user guidance
- missing report path returns a friendly CLI error without traceback
- unset backend now fails as `spike` and points at supported perf backends

- [ ] **Step 2: Run tests to verify they fail**

Run: `timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_wrap.py'`

Expected: new CLI UX assertions fail against the current implementation.

### Task 2: Implement the minimal CLI UX improvements

**Files:**
- Modify: `tools/ventus_perf/cli.py`
- Modify: `tools/ventus_perf/wrap.py`

- [ ] **Step 1: Implement runtime default backend**

Set `VENTUS_BACKEND=spike` during runtime env bootstrap when unset.

- [ ] **Step 2: Implement richer help text**

Add descriptions/help/epilog text for the main parser and both subcommands.

- [ ] **Step 3: Implement user-facing error rendering**

Catch expected usage/configuration failures in the CLI entrypoint and render them as concise `error:` lines instead of Python tracebacks.

- [ ] **Step 4: Implement success-path location hints**

Print experiment/report output paths on successful `run` and `report`.

- [ ] **Step 5: Run the targeted tests**

Run: `timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_wrap.py'`

Expected: PASS

### Task 3: Verify broader report behavior still holds

**Files:**
- Modify: none if prior tasks are correct

- [ ] **Step 1: Run the full tool test suite**

Run: `timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_*.py'`

Expected: PASS
