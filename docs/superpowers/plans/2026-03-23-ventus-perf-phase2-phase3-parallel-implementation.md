# Ventus Perf Phase 2 and Phase 3 Parallel Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `ventus_perf` beyond the current Phase 1 PTX-only baseline by adding multi-backend attribution alignment for `cyclesim` / `rtlsim` / `spike` and by adding explicit `nsys` / `ncu` profiler-pass orchestration and reporting, while keeping one stable user-facing report contract.

**Architecture:** Start by freezing the shared wrapper/report contract so Phase 2 and Phase 3 can proceed in parallel without fighting over `manifest` or report output structure. Phase 2 expands canonical runtime facts and normalizes same-meaning backend events into shared attribution buckets instead of exposing raw backend function names. Phase 3 stays focused on profiler-pass orchestration, artifact capture, and `profiler` report outputs for the `ptx` path, without weakening Phase 1’s explicit-failure behavior.

**Tech Stack:** C++17/20, Python 3 stdlib (`argparse`, `json`, `pathlib`, `subprocess`, `unittest`), CMake/CTest, existing Ventus driver backends, existing `tools/ventus_perf` test suite.

---

## Scope and sequencing

- This plan covers **two implementation tracks** that may run in parallel after the shared contract is locked.
- **Phase 2 constraint from user:** do **not** create extra user-facing time buckets just because backend function names differ. Treat same-meaning events such as `vmemcpy_*`, `pmemcpy_*`, and `copy_*` as shared `memcpy_h2d` / `memcpy_d2h` semantics unless a backend exposes genuinely new information.
- **Phase 2 output rule:** top-level wall-time attribution remains backend-agnostic. Backend-specific simulator facts such as `sim_time_ns`, `step_count`, or `flush_tail_steps` may be retained only as auxiliary facts, not as new top-level buckets.
- **Phase 3 backend rule:** profiler passes remain `ptx` / `sbtsim` only. Extending profiler support to other backends is out of scope.
- Do **not** silently skip unsupported tools or unsupported profiler modes. Fail explicitly in the wrapper and persist that failure in pass manifests.

## Parallel ownership model

Shared single-owner files:

- `tools/ventus_perf/model.py`
- `tools/ventus_perf/report.py`
- `tools/ventus_perf/wrap.py`
- `tools/ventus_perf/cli.py`

Recommended ownership split after Task 1:

- **Track A: Phase 2**
  - `driver/driver/cyclesim_device/ventus.cpp`
  - `driver/driver/rtlsim_device/ventus.cpp`
  - `driver/driver/spike_device/ventus.cpp`
  - `tools/ventus_perf/report.py`
  - `tools/ventus_perf/tests/test_report.py`
  - `tools/ventus_perf/tests/fixtures/multi_backend_*`
- **Track B: Phase 3**
  - `tools/ventus_perf/cli.py`
  - `tools/ventus_perf/wrap.py`
  - `tools/ventus_perf/model.py`
  - `tools/ventus_perf/report.py`
  - `tools/ventus_perf/tests/test_wrap.py`
  - `tools/ventus_perf/tests/test_report.py`
  - `tools/ventus_perf/tests/fixtures/profiler_*`

Merge rule:

- No track edits shared files before Task 1 lands.
- After Task 1, one engineer remains owner of shared Python files and integrates both tracks’ changes.

## File structure

### Shared Python contract and tests

- Modify: `tools/ventus_perf/cli.py`
- Modify: `tools/ventus_perf/model.py`
- Modify: `tools/ventus_perf/report.py`
- Modify: `tools/ventus_perf/wrap.py`
- Modify: `tools/ventus_perf/tests/test_report.py`
- Modify: `tools/ventus_perf/tests/test_wrap.py`
- Create: `tools/ventus_perf/tests/fixtures/multi_backend_experiment/experiment.json`
- Create: `tools/ventus_perf/tests/fixtures/multi_backend_experiment/passes/measure-0001/pass.json`
- Create: `tools/ventus_perf/tests/fixtures/multi_backend_experiment/passes/measure-0001/events.vt.jsonl`
- Create: `tools/ventus_perf/tests/fixtures/multi_backend_experiment/passes/measure-0002/pass.json`
- Create: `tools/ventus_perf/tests/fixtures/multi_backend_experiment/passes/measure-0002/events.vt.jsonl`
- Create: `tools/ventus_perf/tests/fixtures/multi_backend_experiment/passes/measure-0003/pass.json`
- Create: `tools/ventus_perf/tests/fixtures/multi_backend_experiment/passes/measure-0003/events.vt.jsonl`
- Create: `tools/ventus_perf/tests/fixtures/profiler_experiment/experiment.json`
- Create: `tools/ventus_perf/tests/fixtures/profiler_experiment/passes/measure-0001/pass.json`
- Create: `tools/ventus_perf/tests/fixtures/profiler_experiment/passes/measure-0001/events.vt.jsonl`
- Create: `tools/ventus_perf/tests/fixtures/profiler_experiment/passes/nsys-0001/pass.json`
- Create: `tools/ventus_perf/tests/fixtures/profiler_experiment/passes/nsys-0001/artifacts/nsys/summary.json`
- Create: `tools/ventus_perf/tests/fixtures/profiler_experiment/passes/ncu-0001/pass.json`
- Create: `tools/ventus_perf/tests/fixtures/profiler_experiment/passes/ncu-0001/artifacts/ncu/summary.json`

Responsibility split:

- `cli.py`: stable CLI surface for `run` and `report`, including profiler options.
- `model.py`: loader/validator for experiment, pass, artifact, and profiler-target metadata.
- `report.py`: normalized bucket mapping, multi-backend baseline aggregation, profiler supplement aggregation, and output rendering.
- `wrap.py`: authoritative pass orchestration, tool detection, explicit failure manifests, and artifact collection.
- `test_report.py` / `test_wrap.py`: fixture-driven contract tests that lock behavior before implementation.

### Phase 2 native backend instrumentation

- Modify: `driver/driver/cyclesim_device/ventus.cpp`
- Modify: `driver/driver/rtlsim_device/ventus.cpp`
- Modify: `driver/driver/spike_device/ventus.cpp`
- Modify: `driver/driver/auto_select/ventus.cpp`
- Modify: `driver/include/ventus.h`
- Modify: `README.md`

Responsibility split:

- `cyclesim_device/ventus.cpp`: emit canonical memcpy/kernel events plus simulator-only facts such as `sim_time_ns` and `step_count`.
- `rtlsim_device/ventus.cpp`: emit canonical memcpy/kernel events plus `flush_tail_steps`, `sim_time`, and `step_count` where available.
- `spike_device/ventus.cpp`: emit canonical memcpy events and `run_total`, while preserving the limitation that `kernel_wait` may not naturally exist.
- `auto_select/ventus.cpp` / `ventus.h`: keep sideband perf APIs and runtime behavior consistent when unsupported backends no-op certain details.

### Phase 3 profiler orchestration

- Modify: `tools/ventus_perf/cli.py`
- Modify: `tools/ventus_perf/model.py`
- Modify: `tools/ventus_perf/report.py`
- Modify: `tools/ventus_perf/wrap.py`
- Modify: `README.md`

Responsibility split:

- `cli.py`: parse `--profile`, `--ncu-kernel`, and any explicit profiler-pass options.
- `wrap.py`: schedule `measure`, `nsys`, and `ncu` passes; capture artifacts; persist failures explicitly.
- `model.py`: represent profiler passes and artifacts without weakening existing experiment/pass loading.
- `report.py`: emit `reports/profiler.json` only when profiler passes exist and keep baseline `summary` semantics stable.

### Task 1: Lock the shared contract before parallel work

**Files:**
- Modify: `tools/ventus_perf/tests/test_report.py`
- Modify: `tools/ventus_perf/tests/test_wrap.py`
- Create: `tools/ventus_perf/tests/fixtures/multi_backend_experiment/...`
- Create: `tools/ventus_perf/tests/fixtures/profiler_experiment/...`
- Modify: `tools/ventus_perf/model.py`
- Modify: `tools/ventus_perf/report.py`
- Modify: `tools/ventus_perf/wrap.py`

- [ ] **Step 1: Write failing contract tests for normalized multi-backend buckets**

Add tests that assert:

- `vmemcpy_h2d`, `pmemcpy_h2d`, and `copy_to_dev` all roll into `memcpy_h2d`
- `vmemcpy_d2h`, `pmemcpy_d2h`, and `copy_from_dev` all roll into `memcpy_d2h`
- backend-specific facts such as `sim_time_ns` and `step_count` do **not** create new top-level wall-time buckets
- `spike` inputs can render a valid summary even when no natural `kernel_wait` event exists

- [ ] **Step 2: Write failing contract tests for profiler-pass schema**

Add tests that assert:

- `pass_type` may now include `warmup`, `measure`, `nsys`, and `ncu`
- profiler passes may carry `profile_target` and artifact metadata
- `reports/profiler.json` is written only when profiler passes exist
- missing profiler artifacts or failed profiler passes remain explicit in loaded model/report state

- [ ] **Step 3: Run the Python tests and verify the new tests fail**

Run:

```bash
timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_*.py'
```

Expected: FAIL due to missing fixture data and missing contract support in `model.py`, `report.py`, or `wrap.py`.

- [ ] **Step 4: Implement the minimal shared contract**

Implementation requirements:

- Keep existing Phase 1 outputs stable: `summary.txt`, `summary.json`, `timeline.json`, `kernels.json`, and `perfetto.json`
- Add `profiler.json` only when profiler passes are present
- Extend pass loading without breaking single-pass Phase 1 report inputs
- Normalize same-meaning backend events to shared attribution buckets in one central mapping layer inside `report.py`
- Preserve explicit failure semantics for failed and incomplete profiler passes

- [ ] **Step 5: Re-run the Python tests until the shared contract passes**

Run:

```bash
timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_*.py'
```

Expected: PASS for all shared-contract tests.

- [ ] **Step 6: Commit the contract freeze**

```bash
git add tools/ventus_perf/model.py tools/ventus_perf/report.py tools/ventus_perf/wrap.py tools/ventus_perf/tests/test_report.py tools/ventus_perf/tests/test_wrap.py tools/ventus_perf/tests/fixtures
git commit -m "test: lock ventus perf phase2/phase3 shared contract"
```

### Task 2: Implement Phase 2 report normalization for multi-backend attribution

**Files:**
- Modify: `tools/ventus_perf/report.py`
- Modify: `tools/ventus_perf/tests/test_report.py`
- Modify: `tools/ventus_perf/tests/fixtures/multi_backend_experiment/...`

- [ ] **Step 1: Write failing tests for backend event normalization and auxiliary simulator facts**

Add tests that assert:

- the report computes one shared `memcpy_h2d` bucket across PTX, `cyclesim`, `rtlsim`, and `spike` event names
- the report computes one shared `memcpy_d2h` bucket across those backends
- simulator-only facts are preserved under a separate machine-readable section such as `backend_stats` or `auxiliary_facts`
- `concurrency_detected` and `uncategorized` semantics remain unchanged for baseline measured passes

- [ ] **Step 2: Run the focused report tests and verify they fail**

Run:

```bash
timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_report.py'
```

Expected: FAIL because normalization and auxiliary-fact rendering are not implemented yet.

- [ ] **Step 3: Implement the minimal normalization layer in `report.py`**

Implementation requirements:

- Prefer canonical semantic event names whenever native instrumentation can emit them directly
- Where existing backends already emit backend-specific names, normalize them in one place in `report.py`
- Do not create top-level buckets named after backend functions
- Keep backend-specific simulator metrics outside the wall-time bucket closure

- [ ] **Step 4: Re-run the focused report tests until they pass**

Run:

```bash
timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_report.py'
```

Expected: PASS for normalization and auxiliary-fact tests.

- [ ] **Step 5: Commit the report normalization**

```bash
git add tools/ventus_perf/report.py tools/ventus_perf/tests/test_report.py tools/ventus_perf/tests/fixtures/multi_backend_experiment
git commit -m "feat: normalize ventus perf multi-backend attribution"
```

### Task 3: Implement Phase 2 native instrumentation for `cyclesim`, `rtlsim`, and `spike`

**Files:**
- Modify: `driver/driver/cyclesim_device/ventus.cpp`
- Modify: `driver/driver/rtlsim_device/ventus.cpp`
- Modify: `driver/driver/spike_device/ventus.cpp`
- Modify: `driver/driver/auto_select/ventus.cpp`
- Modify: `driver/include/ventus.h`

- [ ] **Step 1: Inspect backend call sites and identify the minimal canonical event boundaries**

For each backend, identify where to record:

- host-to-device memcpy
- device-to-host memcpy
- kernel launch/submit boundary
- kernel wait boundary if it exists naturally
- simulator-only counters or timings that should be preserved as auxiliary facts

- [ ] **Step 2: Write a failing smoke-oriented test or verification note for each backend**

If a backend-local unit test is practical, add it. If not, document and use a runtime smoke command that must produce canonical `events.*.jsonl` lines for that backend.

Preferred verification commands after implementation:

```bash
source env.sh
VENTUS_BACKEND=cyclesim timeout 60s python3 tools/ventus-perf.py run --warmup 0 --repeat 1 -- ./pocl/build/examples/matadd/matadd
VENTUS_BACKEND=rtlsim timeout 60s python3 tools/ventus-perf.py run --warmup 0 --repeat 1 -- ./pocl/build/examples/matadd/matadd
VENTUS_BACKEND=spike timeout 60s python3 tools/ventus-perf.py run --warmup 0 --repeat 1 -- ./pocl/build/examples/matadd/matadd
```

Expected before implementation: unsupported-backend rejection or missing canonical events.

- [ ] **Step 3: Implement canonical event emission in the backend drivers**

Implementation requirements:

- Emit canonical semantic event names where practical instead of preserving backend-local function names
- If a backend can only expose a simulator-specific fact, write it as an auxiliary event or structured attribute, not as a new top-level wall-time bucket
- Preserve explicit sideband perf-context behavior and do not break non-perf execution
- Do not fabricate `kernel_wait` for `spike` if the backend does not naturally expose it

- [ ] **Step 4: Expand wrapper backend validation for Phase 2**

Update `wrap.py` so baseline perf runs may target `ptx`, `sbtsim`, `cyclesim`, `rtlsim`, and `spike`, while profiler passes remain PTX-only.

- [ ] **Step 5: Run native and wrapper verification**

Run:

```bash
cmake --build build/driver-perf -j4
timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_*.py'
```

If the environment is already built, also run the backend smokes from Step 2.

Expected: Python tests pass; each available backend smoke generates a report with shared memcpy buckets.

- [ ] **Step 6: Commit the Phase 2 native backend support**

```bash
git add driver/driver/cyclesim_device/ventus.cpp driver/driver/rtlsim_device/ventus.cpp driver/driver/spike_device/ventus.cpp driver/driver/auto_select/ventus.cpp driver/include/ventus.h tools/ventus_perf/wrap.py
git commit -m "feat: add ventus perf baseline support for simulator backends"
```

### Task 4: Implement Phase 3 CLI and wrapper orchestration for profiler passes

**Files:**
- Modify: `tools/ventus-perf.py`
- Modify: `tools/ventus_perf/cli.py`
- Modify: `tools/ventus_perf/model.py`
- Modify: `tools/ventus_perf/wrap.py`
- Modify: `tools/ventus_perf/tests/test_wrap.py`
- Modify: `tools/ventus_perf/tests/fixtures/profiler_experiment/...`

- [ ] **Step 1: Write failing CLI and wrapper tests for profiler-pass scheduling**

Add tests that assert:

- `run` accepts repeated `--profile` flags
- `--ncu-kernel` is rejected unless `--profile ncu` is present
- profiler passes are appended after baseline measured passes in `actual_passes`
- unsupported profiler/backend combinations fail explicitly before spawn
- missing `nsys` or `ncu` binaries produce failed pass manifests instead of silent skip

- [ ] **Step 2: Run the focused wrapper tests and verify they fail**

Run:

```bash
timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_wrap.py'
```

Expected: FAIL because CLI parsing and wrapper orchestration do not yet support profiler passes.

- [ ] **Step 3: Implement the minimal profiler orchestration**

Implementation requirements:

- Extend `cli.py` with `--profile` and `--ncu-kernel`
- Keep `tools/ventus-perf.py` as the only user-facing entry script
- In `wrap.py`, schedule extra `nsys` / `ncu` passes as separate pass directories with authoritative `pass.begin.json` / `pass.json`
- Record tool-not-found and non-zero-exit cases as explicit failed passes
- Restrict profiler passes to PTX-family backends for this phase

- [ ] **Step 4: Re-run the focused wrapper tests until they pass**

Run:

```bash
timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_wrap.py'
```

Expected: PASS for profiler scheduling and explicit-failure tests.

- [ ] **Step 5: Commit the profiler orchestration**

```bash
git add tools/ventus-perf.py tools/ventus_perf/cli.py tools/ventus_perf/model.py tools/ventus_perf/wrap.py tools/ventus_perf/tests/test_wrap.py tools/ventus_perf/tests/fixtures/profiler_experiment
git commit -m "feat: orchestrate ventus perf profiler passes"
```

### Task 5: Implement Phase 3 profiler artifact loading and report outputs

**Files:**
- Modify: `tools/ventus_perf/model.py`
- Modify: `tools/ventus_perf/report.py`
- Modify: `tools/ventus_perf/tests/test_report.py`
- Modify: `tools/ventus_perf/tests/fixtures/profiler_experiment/...`

- [ ] **Step 1: Write failing report tests for profiler supplements**

Add tests that assert:

- `reports/profiler.json` is written for an experiment containing profiler passes
- profiler summary includes `nsys` and `ncu` sections when their passes succeed
- failed profiler passes remain visible in the report instead of disappearing
- baseline `summary` remains based on measured passes only
- kernel aggregation across measured and profiler passes follows the conservative rules from the design doc

- [ ] **Step 2: Run the focused report tests and verify they fail**

Run:

```bash
timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_report.py'
```

Expected: FAIL because profiler artifact parsing and output rendering are not implemented yet.

- [ ] **Step 3: Implement minimal profiler artifact loading and rendering**

Implementation requirements:

- Load profiler artifacts from pass-local `artifacts/nsys/` and `artifacts/ncu/`
- Render a machine-readable `profiler` section and write `reports/profiler.json` only when profiler passes exist
- Keep `summary.txt` focused on baseline attribution; do not bury baseline output under profiler detail
- Use conservative alignment: aggregate by `kernel_name + kernel_signature_hash` unless the data explicitly supports something stronger

- [ ] **Step 4: Re-run the focused report tests until they pass**

Run:

```bash
timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_report.py'
```

Expected: PASS for profiler supplement tests.

- [ ] **Step 5: Commit the profiler reporting**

```bash
git add tools/ventus_perf/model.py tools/ventus_perf/report.py tools/ventus_perf/tests/test_report.py tools/ventus_perf/tests/fixtures/profiler_experiment
git commit -m "feat: report ventus perf profiler supplements"
```

### Task 6: Final verification and documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update the user documentation**

Document:

- which backends support baseline perf runs after Phase 2
- which backends support profiler passes after Phase 3
- example commands for baseline multi-backend runs
- example commands for `nsys` / `ncu` runs
- report output files, including conditional `reports/profiler.json`

- [ ] **Step 2: Run automated verification**

Run:

```bash
timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_*.py'
cmake --build build/driver-perf --target test_perf_recorder -j4
timeout 60s ctest --test-dir build/driver-perf -R perf_recorder --output-on-failure
```

Expected: all listed tests pass.

- [ ] **Step 3: Run real smokes where the environment is available**

Baseline smokes:

```bash
source env.sh
VENTUS_BACKEND=ptx timeout 60s python3 tools/ventus-perf.py run --warmup 0 --repeat 1 -- ./pocl/build/examples/matadd/matadd
VENTUS_BACKEND=cyclesim timeout 60s python3 tools/ventus-perf.py run --warmup 0 --repeat 1 -- ./pocl/build/examples/matadd/matadd
VENTUS_BACKEND=rtlsim timeout 60s python3 tools/ventus-perf.py run --warmup 0 --repeat 1 -- ./pocl/build/examples/matadd/matadd
VENTUS_BACKEND=spike timeout 60s python3 tools/ventus-perf.py run --warmup 0 --repeat 1 -- ./pocl/build/examples/matadd/matadd
```

Profiler smokes when tools exist:

```bash
source env.sh
VENTUS_BACKEND=ptx timeout 60s python3 tools/ventus-perf.py run --warmup 0 --repeat 1 --profile nsys -- ./pocl/build/examples/matadd/matadd
VENTUS_BACKEND=ptx timeout 60s python3 tools/ventus-perf.py run --warmup 0 --repeat 1 --profile ncu --ncu-kernel matadd -- ./pocl/build/examples/matadd/matadd
```

Expected:

- baseline runs generate shared top-level buckets regardless of backend
- profiler runs generate explicit profiler pass manifests and `reports/profiler.json`
- missing profiler tools produce failed profiler passes, not silent omission

- [ ] **Step 4: Commit the final docs and verification updates**

```bash
git add README.md
git commit -m "docs: describe ventus perf multi-backend and profiler phases"
```

## Acceptance criteria

- [ ] Phase 2 baseline runs support `ptx`, `sbtsim`, `cyclesim`, `rtlsim`, and `spike` through the wrapper.
- [ ] Same-meaning backend events are normalized into shared top-level buckets instead of becoming new user-facing bucket names.
- [ ] Backend-specific simulator metrics are preserved only as auxiliary facts, not as extra wall-time buckets.
- [ ] `spike` reports remain valid even when a natural `kernel_wait` event is absent.
- [ ] Phase 3 profiler passes support `nsys` and `ncu` on the PTX path only.
- [ ] Missing or failed profiler tools are recorded explicitly in pass manifests and report outputs.
- [ ] `reports/profiler.json` exists only when profiler passes are present.
- [ ] Existing Phase 1 baseline outputs remain stable for PTX-only runs.
- [ ] `tools/ventus_perf` Python tests pass within 60 seconds.
- [ ] `driver` `perf_recorder` verification passes within 60 seconds.
