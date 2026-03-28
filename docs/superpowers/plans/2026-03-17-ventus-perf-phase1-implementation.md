# Ventus Perf Attribution Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Phase 1 baseline performance attribution stack for the current Ventus `ptx` / `sbtsim` path: canonical per-pass recorder, key PoCL / `vt_*` / `ptx_device` events, parent-side wall-time measurement for the `sbt_ptx` subprocess as one `generate_ptx_via_sbt` span, and an offline wrapper/reporter that emits `summary`, `timeline`, and `kernel` views.

**Architecture:** Keep runtime recording and offline reporting separate. Native C++ producers write canonical event JSONL inside a wrapper-assigned per-pass directory; Python wrapper code is the authoritative owner of `experiment.begin.json`, `experiment.json`, `pass.begin.json`, and `pass.json`, with `pass.begin.json` written immediately after a subprocess is successfully spawned so `pid` is factual. Phase 1 supports only wrapper-managed experiments for the synchronous `ptx` / `sbtsim` execution model from the design doc, and defers `cyclesim` / `rtlsim` / `spike` parity plus `nsys` / `ncu` integration to later plans. Reporter-owned derived facts such as `concurrency_detected` remain offline computations and are not treated as wrapper-owned lifecycle truth. Cross-module PoCL -> driver correlation uses a dedicated sideband perf context API instead of assuming shared `thread_local` state or overloading existing `vt_*` primary arguments.

**Tech Stack:** C++17/20, Python 3 stdlib (`argparse`, `json`, `pathlib`, `subprocess`, `unittest`), CMake/CTest, existing PoCL / driver / sbtsim build targets.

---

## Scope Notes

- This plan intentionally implements only **Phase 1** from [`docs/superpowers/specs/2026-03-17-ventus-perf-attribution-design.md`](/work/ventus-env/docs/superpowers/specs/2026-03-17-ventus-perf-attribution-design.md).
- Do **not** add `nsys` / `ncu`, `cyclesim` / `rtlsim` / `spike` backend-specific counters, protobuf export, TUI/HTML, or silent fallback paths in this plan.
- Do **not** require a new worktree just to write or review this plan. When executing the implementation, prefer a worktree only if the engineer wants branch isolation; otherwise execute in-place with care because the current repository is already dirty.
- This repository currently has no top-level `tools/` directory; this plan intentionally creates `tools/` and `tools/ventus_perf/` as the canonical home for the new user-facing perf tool.
- Do **not** rely on `PYTHONPATH` or other ad-hoc environment hacks for the new Python tool. Package the multi-file implementation under a dedicated `tools/ventus_perf/` directory and keep `tools/ventus-perf.py` as the only user-facing entry script.
- For files placed directly under `tools/`, add a short top-level comment that explains the background need, implementation flow, usage, and any key maintenance notes. If a tool does not have separate documentation, the top-level script must remain maintainable as a single file.
- Do **not** create `tools/__init__.py`; the `tools/` root must contain only user-facing entry scripts, per project rules.
- Treat `tools/ventus_perf/wrap.py` as the authoritative writer of `pass.begin.json` and `pass.json`; `pass.begin.json` is written only after successful spawn so `pid` is real, and runtime code must not write these authoritative manifests.
- Do **not** assume PoCL and driver automatically share one DSO-global `thread_local` perf context. Cross-module correlation in Phase 1 must be based on explicit propagated fields such as `launch_seq`, `kernel_name`, `kernel_occurrence`, and `kernel_signature_hash`, with optional scope identifiers when they can be carried safely.
- Because the default runtime path may go through `libventus_driver.so` and `auto_select_driver`, Phase 1 must also add sideband perf-context forwarding there; updating only `ptx_driver` is insufficient.
- Phase 1 does **not** require `cuda_trace.cpp` instrumentation or `reports/profiler.json`; PTX-path CUDA-facing facts come from `ptx_device` direct `cu*` events in this plan, and profiler supplement outputs are deferred to the later profiler phase.
- Phase 1 perf support is limited to the `ptx` / `sbtsim` backend path. `tools/ventus-perf.py run` must reject unsupported backends explicitly instead of attempting a degraded run.
- Adding sideband perf-context APIs must not break existing non-perf or non-`ptx` execution. `auto_select` must preserve legacy behavior for unsupported backends, and unsupported backends may treat the new perf sideband calls as no-op.

## File Structure

### Native recorder and shared schema

- Create: `driver/common/ventus_perf_schema.hpp`
- Create: `driver/common/ventus_perf_recorder.hpp`
- Create: `driver/common/ventus_perf_recorder.cpp`
- Create: `driver/common/ventus_perf_scope.hpp`
- Create: `driver/common/ventus_perf_context.hpp`
- Modify: `driver/CMakeLists.txt`
- Modify: `driver/common/CMakeLists.txt`
- Modify: `driver/include/ventus.h`

Responsibility split:

- `ventus_perf_schema.hpp`: canonical constants, enums, and helper serializers for `experiment`, `pass`, `event`, `artifact`, and top-level attribution buckets.
- `ventus_perf_recorder.hpp/.cpp`: per-process recorder lifecycle, output directory checks, event JSONL append, environment parsing, and explicit error surfacing.
- `ventus_perf_scope.hpp`: thread-local scope stack, launch-sequence tracking, explicit context snapshot helpers, and RAII guard used inside one linked module.
- `ventus_perf_context.hpp`: C-compatible sideband perf context payload and helpers shared across PoCL and the driver.
- `driver/include/ventus.h`: expose sideband perf context API declarations alongside the existing `vt_*` ABI.

### Runtime producer integration

- Modify: `pocl/lib/CL/devices/ventus/pocl_ventus.h`
- Modify: `pocl/lib/CL/devices/ventus/pocl_ventus.cc`
- Modify: `pocl/lib/CL/devices/ventus/CMakeLists.txt`
- Modify: `driver/driver/auto_select/ventus.cpp`
- Modify: `driver/driver/ptx_device/ventus.cpp`

Responsibility split:

- `pocl_ventus.*`: OpenCL semantic scopes, sideband perf context API calls, and high-level `pocl` event emission.
- `pocl/lib/CL/devices/ventus/CMakeLists.txt`: compile/link the shared recorder sources into `pocl-devices-ventus` and add the required include paths / dependent libraries for a standalone PoCL build.
- `driver/driver/auto_select/ventus.cpp`: export and forward `vt_set_perf_context` / `vt_clear_perf_context` through the dynamic backend loader so the default `libventus_driver.so` path preserves perf context for `ptx`, while unsupported backends preserve legacy behavior and may no-op these calls.
- `ptx_device/ventus.cpp`: `vt_*` baseline events, sideband perf context consumption, PTX JIT details, and parent-side wall-time measurement for the `sbt_ptx` subprocess.

### Wrapper and reporter

- Create: `tools/ventus-perf.py`
- Create: `tools/ventus_perf/__init__.py`
- Create: `tools/ventus_perf/cli.py`
- Create: `tools/ventus_perf/model.py`
- Create: `tools/ventus_perf/report.py`
- Create: `tools/ventus_perf/wrap.py`
- Create: `tools/ventus_perf/tests/__init__.py`
- Create: `tools/ventus_perf/tests/test_report.py`
- Create: `tools/ventus_perf/tests/test_wrap.py`
- Create: `tools/ventus_perf/tests/fixtures/minimal_experiment/experiment.json`
- Create: `tools/ventus_perf/tests/fixtures/minimal_experiment/passes/measure-0001/pass.json`
- Create: `tools/ventus_perf/tests/fixtures/minimal_experiment/passes/measure-0001/events.pocl.jsonl`
- Create: `tools/ventus_perf/tests/fixtures/minimal_experiment/passes/measure-0001/events.vt.jsonl`
- Create: `tools/ventus_perf/tests/fixtures/incomplete_experiment/experiment.begin.json`
- Create: `tools/ventus_perf/tests/fixtures/incomplete_experiment/passes/measure-0001/pass.begin.json`
- Create: `tools/ventus_perf/tests/fixtures/incomplete_experiment/passes/measure-0001/events.vt.jsonl`
- Create: `tools/ventus_perf/tests/fixtures/overlap_case/experiment.json`
- Create: `tools/ventus_perf/tests/fixtures/overlap_case/passes/measure-0001/pass.json`
- Create: `tools/ventus_perf/tests/fixtures/overlap_case/passes/measure-0001/events.vt.jsonl`

Responsibility split:

- `tools/ventus-perf.py`: top-level executable entry script with concise maintenance comments and zero external env hacks.
- `tools/ventus_perf/cli.py`: argument parsing and command dispatch.
- `tools/ventus_perf/model.py`: schema validation/loading helpers for wrapper-managed `experiment`, `pass`, and events, including incomplete experiments and incomplete passes.
- `tools/ventus_perf/report.py`: attribution closure, concurrency detection, `summary.sub_buckets`, and `summary` / `timeline` / `kernel` rendering for experiment inputs and wrapper-generated single-pass inputs.
- `tools/ventus_perf/wrap.py`: experiment directory allocation, per-pass env injection, subprocess execution, authoritative pass manifest finalization, and stdout/stderr capture.

### Targeted tests

- Create: `driver/codetests/test_perf_recorder.cpp`
- Modify: `driver/CMakeLists.txt`
- Modify: `driver/codetests/CMakeLists.txt`

Testing split:

- Native recorder logic: C++ unit/integration test under `driver/codetests`, with `enable_testing()` and `add_test()` wired from the top-level `driver/` CMake entrypoint.
- Report and wrapper logic: Python fixture tests under `tools/ventus_perf/tests`.
- One real runtime smoke: wrapper on a prebuilt small OpenCL binary if the local environment is already built.

### Task 1: Build the shared native recorder core

**Files:**
- Create: `driver/common/ventus_perf_schema.hpp`
- Create: `driver/common/ventus_perf_recorder.hpp`
- Create: `driver/common/ventus_perf_recorder.cpp`
- Create: `driver/common/ventus_perf_scope.hpp`
- Create: `driver/common/ventus_perf_context.hpp`
- Modify: `driver/CMakeLists.txt`
- Modify: `driver/common/CMakeLists.txt`
- Modify: `driver/include/ventus.h`
- Create: `driver/codetests/test_perf_recorder.cpp`
- Modify: `driver/codetests/CMakeLists.txt`

- [ ] **Step 1: Write the failing native test for recorder lifecycle**

```cpp
#include "ventus_perf_recorder.hpp"

#include <filesystem>
#include <fstream>

static void test_recorder_writes_event_files() {
  namespace fs = std::filesystem;
  const fs::path out_dir = fs::temp_directory_path() / "ventus-perf-recorder-test";
  fs::remove_all(out_dir);

  vtperf::RecorderConfig config;
  config.enabled = true;
  config.experiment_id = "exp-test";
  config.pass_id = "measure-0001";
  config.pass_type = "measure";
  config.backend = "ptx";
  config.out_dir = out_dir;

  vtperf::Recorder recorder(config);

  vtperf::CompleteEvent event;
  event.stream = "vt";
  event.event_type = "vt_start";
  event.ts_start_ns = 10;
  event.ts_end_ns = 20;
  recorder.write_event(event);
  if (!fs::exists(out_dir / "events.vt.jsonl")) std::abort();
}

int main() {
  test_recorder_writes_event_files();
  return 0;
}
```

- [ ] **Step 2: Run the recorder test and verify it fails because the recorder does not exist yet**

Run:

```bash
cmake -S driver -B build/driver-perf -DDRIVER_ENABLE_PTX=ON
cmake --build build/driver-perf --target test_perf_recorder -j4
timeout 60s ctest --test-dir build/driver-perf -R perf_recorder --output-on-failure
```

Expected: compile or link failure because the recorder files, test target wiring, or symbols do not exist yet.

- [ ] **Step 3: Implement the minimal recorder and scope primitives**

```cpp
namespace vtperf {

struct RecorderConfig final {
  bool enabled = false;
  std::string experiment_id;
  std::string pass_id;
  std::string pass_type;
  std::string backend;
  std::filesystem::path out_dir;
};

class Recorder final {
public:
  explicit Recorder(RecorderConfig config);

  void write_event(const CompleteEvent& event);

private:
  RecorderConfig config_;
  std::unordered_map<std::string, std::filesystem::path> event_paths_;
};

}  // namespace vtperf
```

Implementation requirements:

- Reject `VENTUS_PERF_OUT_DIR` only when the target directory already exists and is non-empty.
- Append canonical `events.*.jsonl` files and keep the event stream append-friendly.
- Require wrapper-injected `experiment_id`, `pass_id`, `pass_type`, and `VENTUS_PERF_OUT_DIR` when `VENTUS_PERF=1`; missing required fields must raise an explicit init error.
- Do not write `pass.begin.json` / `pass.json` from the runtime recorder.
- Do not add runtime-recorder APIs whose names imply wrapper-owned lifecycle authority such as `write_pass_begin()` or `finalize_*()`.
- Add `enable_testing()` in `driver/CMakeLists.txt`.
- Replace the placeholder `codetests` executable in `driver/codetests/CMakeLists.txt` with `add_executable(test_perf_recorder test_perf_recorder.cpp)` and register `add_test(NAME perf_recorder COMMAND test_perf_recorder)` so the documented `ctest` loop is real.
- Store canonical timestamps as `CLOCK_MONOTONIC` absolute nanoseconds only.
- Keep `event_id`, `parent_event_id`, `scope_id`, `pid`, `tid`, `queue_id`, `launch_seq`, `kernel_occurrence`, and `kernel_signature_hash` as formal top-level fields.
- Generate `event_id` / `scope_id` using a pass-unique scheme that embeds producer/process identity, not a plain local counter that can collide across DSOs or child processes.
- Surface recorder init/write failures directly; do not silently disable recording.

- [ ] **Step 4: Re-run the native recorder test until it passes**

Run:

```bash
cmake --build build/driver-perf --target test_perf_recorder -j4
timeout 60s ctest --test-dir build/driver-perf -R perf_recorder --output-on-failure
```

Expected: `perf_recorder` passes with `0` failures.

- [ ] **Step 5: Commit the recorder core**

```bash
git add driver/common/ventus_perf_schema.hpp driver/common/ventus_perf_recorder.hpp driver/common/ventus_perf_recorder.cpp driver/common/ventus_perf_scope.hpp driver/common/ventus_perf_context.hpp driver/include/ventus.h driver/CMakeLists.txt driver/common/CMakeLists.txt driver/codetests/test_perf_recorder.cpp driver/codetests/CMakeLists.txt
git commit -m "feat: add ventus perf recorder core"
```

### Task 2: Lock the offline report contract with fixture-driven Python tests

**Files:**
- Create: `tools/ventus_perf/__init__.py`
- Create: `tools/ventus_perf/model.py`
- Create: `tools/ventus_perf/report.py`
- Create: `tools/ventus_perf/tests/__init__.py`
- Create: `tools/ventus_perf/tests/test_report.py`
- Create: `tools/ventus_perf/tests/fixtures/minimal_experiment/experiment.json`
- Create: `tools/ventus_perf/tests/fixtures/minimal_experiment/passes/measure-0001/pass.json`
- Create: `tools/ventus_perf/tests/fixtures/minimal_experiment/passes/measure-0001/events.pocl.jsonl`
- Create: `tools/ventus_perf/tests/fixtures/minimal_experiment/passes/measure-0001/events.vt.jsonl`
- Create: `tools/ventus_perf/tests/fixtures/incomplete_experiment/experiment.begin.json`
- Create: `tools/ventus_perf/tests/fixtures/incomplete_experiment/passes/measure-0001/pass.begin.json`
- Create: `tools/ventus_perf/tests/fixtures/incomplete_experiment/passes/measure-0001/events.vt.jsonl`
- Create: `tools/ventus_perf/tests/fixtures/overlap_case/experiment.json`
- Create: `tools/ventus_perf/tests/fixtures/overlap_case/passes/measure-0001/pass.json`
- Create: `tools/ventus_perf/tests/fixtures/overlap_case/passes/measure-0001/events.vt.jsonl`

- [ ] **Step 1: Write failing Python tests for summary, timeline, kernel aggregation, and conservative concurrency handling**

```python
import pathlib
import unittest

from ventus_perf import report as ventus_perf_report


FIXTURE_ROOT = pathlib.Path(__file__).resolve().parent / "fixtures"
MINIMAL_FIXTURE = FIXTURE_ROOT / "minimal_experiment"
INCOMPLETE_FIXTURE = FIXTURE_ROOT / "incomplete_experiment"
OVERLAP_FIXTURE = FIXTURE_ROOT / "overlap_case"


class SummaryViewTests(unittest.TestCase):
    def test_summary_closes_top_level_buckets_for_single_measure_pass(self) -> None:
        report = ventus_perf_report.load_input_report(MINIMAL_FIXTURE)
        self.assertFalse(report["concurrency_detected"])
        self.assertEqual(report["measured_pass_count"], 1)
        self.assertAlmostEqual(
            report["summary"]["top_level_total_ns"],
            report["summary"]["wall_time_ns"],
            delta=1_000_000,
        )
        self.assertIn("kernel_exec_wait", report["summary"]["buckets"])
        self.assertIn("kernel_prepare", report["summary"]["sub_buckets"])
        self.assertEqual(
            report["summary"]["wall_time_stats"]["mean_ns"],
            report["summary"]["wall_time_ns"],
        )
        self.assertEqual(
            report["summary"]["wall_time_stats"]["min_ns"],
            report["summary"]["wall_time_ns"],
        )
        self.assertEqual(
            report["summary"]["wall_time_stats"]["max_ns"],
            report["summary"]["wall_time_ns"],
        )

    def test_report_expands_uncategorized_when_overlap_detected(self) -> None:
        report = ventus_perf_report.load_input_report(OVERLAP_FIXTURE)
        self.assertTrue(report["concurrency_detected"])
        self.assertGreater(report["summary"]["buckets"]["uncategorized"], 0)

    def test_incomplete_experiment_is_still_reportable(self) -> None:
        report = ventus_perf_report.load_input_report(INCOMPLETE_FIXTURE)
        self.assertTrue(report["best_effort"])
        self.assertEqual(report["state"], "incomplete")

    def test_failed_or_incomplete_measured_passes_are_not_silently_dropped(self) -> None:
        report = ventus_perf_report.load_input_report(INCOMPLETE_FIXTURE)
        self.assertGreaterEqual(len(report["passes"]), 1)
```

- [ ] **Step 2: Run the Python tests and verify they fail because the loader/report module does not exist yet**

Run:

```bash
timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_report.py'
```

Expected: import failure for `ventus_perf.report` or missing `load_input_report`.

- [ ] **Step 3: Implement the minimal report/model layer against fixture data**

```python
TOP_LEVEL_BUCKETS = (
    "host_overhead",
    "h2d",
    "kernel_prepare",
    "kernel_exec_wait",
    "d2h",
    "teardown",
    "uncategorized",
)


def load_input_report(input_dir: pathlib.Path) -> dict:
    experiment, passes = load_input_manifests(input_dir)
    measured_completed = [
        p for p in passes if p["pass_type"] == "measure" and p["state"] == "completed"
    ]
    all_timeline = load_events_for_passes(passes)
    attribution = build_attribution(measured_completed, all_timeline)
    return render_report(experiment, passes, all_timeline, attribution)
```

Implementation requirements:

- Treat `experiment.begin.json` without `experiment.json` as `incomplete`.
- Trust `actual_passes` ordering from `experiment.json`; do not infer by scanning directory names for wrapper-managed experiments.
- Accept an `experiment-dir` input and a wrapper-generated single `pass-dir` input; do not invent a direct-run lifecycle.
- For single `pass-dir`, require wrapper-written `pass.begin.json` or `pass.json` and write reports to `<pass-dir>/reports/`.
- Detect overlapping `kernel_prepare` / `kernel_exec_wait` windows and switch to conservative mode.
- Compute `concurrency_detected` in the reporter from canonical events instead of trusting a pass-manifest cache.
- Expose PTX-path drill-downs in `summary.sub_buckets`, at least for `kernel_prepare` and `kernel_exec_wait`.
- Emit `summary.wall_time_stats` with `mean_ns`, `min_ns`, `max_ns`, and `stddev_ns` over completed measured passes.
- Build `kernel_prepare` / `kernel_exec_wait` spans from canonical events using `launch_seq` as the primary key, with fallback to explicit `scope_id` / `parent_event_id` only when necessary.
- Treat `vt_start`, `generate_ptx_via_sbt`, `read_generated_ptx`, `cuModuleLoadDataEx`, `cuModuleGetFunction`, and `cuLaunchKernel` as `kernel_prepare` candidates.
- Treat `kernel_wait`, `vt_ready_wait`, and `cuCtxSynchronize` as `kernel_exec_wait` candidates.
- When both coarse and fine events exist for one invocation, count only one top-level mutual-exclusive span and use the finer events only for `summary.sub_buckets`.
- Produce `summary.json`, `timeline.json`, and `kernels.json` structures even when some passes are failed/incomplete.
- Keep failed/incomplete measured passes visible in report metadata even when attribution totals are computed only from completed measured passes.

- [ ] **Step 4: Re-run the Python report tests until they pass**

Run:

```bash
timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_report.py'
```

Expected: all `test_report.py` cases pass.

- [ ] **Step 5: Commit the report contract**

```bash
git add tools/ventus_perf/__init__.py tools/ventus_perf/model.py tools/ventus_perf/report.py tools/ventus_perf/tests/__init__.py tools/ventus_perf/tests/test_report.py tools/ventus_perf/tests/fixtures
git commit -m "test: lock ventus perf report contract"
```

### Task 3: Add wrapper-managed PoCL semantic scopes and sideband propagation

**Files:**
- Modify: `pocl/lib/CL/devices/ventus/pocl_ventus.h`
- Modify: `pocl/lib/CL/devices/ventus/pocl_ventus.cc`
- Modify: `pocl/lib/CL/devices/ventus/CMakeLists.txt`
- Modify: `driver/common/ventus_perf_scope.hpp`
- Modify: `driver/common/ventus_perf_context.hpp`
- Modify: `driver/include/ventus.h`
- Modify: `driver/driver/auto_select/ventus.cpp`
- Modify: `driver/driver/ptx_device/ventus.cpp`
- Modify: `driver/codetests/test_perf_recorder.cpp`

- [ ] **Step 1: Extend the failing native test to require semantic scopes and pass finalization fields**

```cpp
static void test_scope_parentage_and_launch_sequence() {
  namespace fs = std::filesystem;
  vtperf::RecorderConfig config;
  config.enabled = true;
  config.experiment_id = "exp-test";
  config.pass_id = "measure-0001";
  config.pass_type = "measure";
  config.backend = "ptx";
  config.out_dir = fs::temp_directory_path() / "ventus-perf-scope-test";
  vtperf::Recorder recorder(config);
  vtperf::ScopedEvent outer(recorder, "pocl", "kernel_submit");
  const auto snapshot = vtperf::current_scope_snapshot();
  if (snapshot.scope_id.empty()) std::abort();
  if (snapshot.event_id.empty()) std::abort();
  if (vtperf::next_launch_sequence() != 1) std::abort();
}
```

- [ ] **Step 2: Run the native test and verify it fails for missing scope/launch helpers**

Run:

```bash
cmake --build build/driver-perf --target test_perf_recorder -j4
timeout 60s ctest --test-dir build/driver-perf -R perf_recorder --output-on-failure
```

Expected: compile or assertion failure around scope helpers.

- [ ] **Step 3: Implement PoCL-side pass/bootstrap logic and semantic scope emission**

```cpp
static vtperf::Recorder& perf_recorder_for_device(vt_device_data_t* device_data);

void pocl_ventus_run(void* data, _cl_command_node* cmd) {
  auto& recorder = perf_recorder_for_device(static_cast<vt_device_data_t*>(data));
  vtperf::ScopedEvent submit_scope(recorder, "pocl", "kernel_submit");
  vtperf::ScopedEvent wait_scope(recorder, "pocl", "kernel_wait");
  // existing kernel execution path stays in place
}
```

Implementation requirements:

- Initialize the recorder only when `VENTUS_PERF=1`.
- Require wrapper-injected `VENTUS_PERF_EXPERIMENT_ID`, `VENTUS_PERF_PASS_ID`, `VENTUS_PERF_PASS_TYPE`, and `VENTUS_PERF_OUT_DIR`; if any are missing under `VENTUS_PERF=1`, fail explicitly.
- Emit `buffer_write`, `buffer_read`, `buffer_copy`, `buffer_fill`, `map_mem`, `unmap_mem`, `kernel_arg_pack`, `kernel_arg_upload`, `kernel_elf_upload`, `kernel_metadata_upload`, `kernel_submit`, and `kernel_wait` events where the existing PoCL code already has those boundaries.
- Record `kernel_occurrence`, `kernel_name`, `kernel_signature_hash`, and `launch_seq` at the top level, not buried in `attrs`.
- Do **not** assume PoCL and `ptx_driver` share one in-memory `thread_local` scope stack.
- Add a C-compatible sideband API in `driver/include/ventus.h`:
  - `vt_perf_context_t`
  - `vt_set_perf_context(vt_device_h, const vt_perf_context_t*)`
  - `vt_clear_perf_context(vt_device_h)`
- At the PoCL -> `vt_*` boundary, populate sideband perf context with:
  - `launch_seq`
  - `kernel_name`
  - `kernel_occurrence`
  - `kernel_signature_hash`
  - optional `scope_id` / `parent_event_id`
- Update `driver/driver/auto_select/ventus.cpp` to load, export, and forward:
  - `vt_set_perf_context`
  - `vt_clear_perf_context`
- Let driver-side `vt_*` events consume the sideband context and re-emit those fields at the event top level.
- In wrapper-managed runs, `tools/ventus_perf/wrap.py` must write `pass.begin.json` immediately after successful spawn and finalize `pass.json` after subprocess exit, including `state`, `exit_code`, `term_signal`, `stdout_path`, `stderr_path`, `event_files`, and `recorder_errors`.
- Update `pocl/lib/CL/devices/ventus/CMakeLists.txt` so `pocl-devices-ventus` can actually build with the recorder in a standalone PoCL configure:
  - compute a repo-sibling path such as `set(VENTUS_DRIVER_COMMON_DIR "${CMAKE_SOURCE_DIR}/../driver/common")`
  - compile `ventus_perf_recorder.cpp` directly into `pocl-devices-ventus` from `${VENTUS_DRIVER_COMMON_DIR}` instead of assuming a pre-existing driver-side CMake target
  - add `${VENTUS_DRIVER_COMMON_DIR}` to `target_include_directories(...)` so `ventus_perf_recorder.hpp`, `ventus_perf_scope.hpp`, and `ventus_perf_schema.hpp` are visible
  - wire any recorder implementation dependencies explicitly inside the PoCL build if the shared source needs them
  - do not rely on duplicated source compilation alone to provide cross-module context sharing; sideband perf context propagation remains required

- [ ] **Step 4: Re-run the native recorder tests**

Run:

```bash
cmake --build build/driver-perf --target test_perf_recorder -j4
timeout 60s ctest --test-dir build/driver-perf -R perf_recorder --output-on-failure
```

Expected: recorder tests stay green with new scope assertions.

- [ ] **Step 5: Commit the PoCL semantic scope integration**

```bash
git add pocl/lib/CL/devices/ventus/pocl_ventus.h pocl/lib/CL/devices/ventus/pocl_ventus.cc pocl/lib/CL/devices/ventus/CMakeLists.txt driver/common/ventus_perf_scope.hpp driver/common/ventus_perf_context.hpp driver/include/ventus.h driver/driver/auto_select/ventus.cpp driver/driver/ptx_device/ventus.cpp driver/codetests/test_perf_recorder.cpp
git commit -m "feat: add pocl semantic perf scopes"
```

### Task 4: Instrument the PTX driver path and wrapper-managed pass execution

**Files:**
- Modify: `driver/driver/ptx_device/ventus.cpp`
- Create: `tools/ventus_perf/wrap.py`
- Create: `tools/ventus_perf/tests/test_wrap.py`

- [ ] **Step 1: Write failing wrapper-side tests for backend gating and pass lifecycle finalization**

```python
import unittest

from ventus_perf import wrap as ventus_perf_wrap


class ChildContextTests(unittest.TestCase):
    def test_phase1_rejects_unsupported_backend(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported backend"):
            ventus_perf_wrap.validate_phase1_backend("spike")

    def test_finalize_pass_classifies_signaled_exit(self) -> None:
        manifest = ventus_perf_wrap.finalize_pass_manifest(
            pass_dir="/tmp/ventus-pass",
            pass_id="measure-0001",
            pass_type="measure",
            returncode=-9,
            stdout_path="stdout.log",
            stderr_path="stderr.log",
            event_files=["events.vt.jsonl"],
        )
        self.assertEqual(manifest["state"], "signaled")
        self.assertEqual(manifest["term_signal"], 9)
        self.assertEqual(manifest["stdout_path"], "stdout.log")
```

- [ ] **Step 2: Run the Python wrapper test and verify it fails before the wrapper env builder exists**

Run:

```bash
timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_wrap.py'
```

Expected: import failure or missing `validate_phase1_backend` / `finalize_pass_manifest`.

- [ ] **Step 3: Add parent-side PTX driver event emission and wrapper pass execution**

```cpp
static bool generate_ptx_via_sbt(
    const fs::path& elf,
    const std::string& kernel,
    int sm,
    const fs::path& out_ptx,
    std::string* log,
    vtperf::Recorder* recorder) {
  vtperf::ScopedEvent translate_scope(*recorder, "vt", "generate_ptx_via_sbt");
  return run_child_process(...);
}
```

Implementation requirements:

- In `ptx_device/ventus.cpp`, emit canonical `vt_*` events for `vt_buf_alloc`, `vt_buf_free`, `vt_copy_to_dev`, `vt_copy_from_dev`, `vt_upload_kernel_file`, `vt_start`, and `vt_ready_wait`.
- Add PTX-specific drill-down events for `generate_ptx_via_sbt`, `read_generated_ptx`, `cuModuleLoadDataEx`, `cuModuleGetFunction`, `cuLaunchKernel`, and `cuCtxSynchronize`.
- Do not add `cuda_trace.cpp` as a Phase 1 requirement just to duplicate these facts; canonical CUDA-facing events come from `ptx_device` in this plan.
- Measure the SBT translation cost only from the parent process: `generate_ptx_via_sbt` must span the real subprocess wall time plus immediate parent-side setup/teardown around it, without requiring `sbt_ptx` internal instrumentation.
- Do not modify `sbt_ptx.cpp` or require canonical `events.sbt_ptx.jsonl` in Phase 1.
- Preserve `GPU_SBT_PTX_PROFILE_LOG` only as legacy compatibility output; the reporter must ignore it.
- In `tools/ventus_perf/wrap.py`, finalize wrapper-managed passes after `subprocess` return, classifying `completed`, `failed`, or `signaled` from the real return code instead of depending on `pocl_ventus_uninit()`.
- In `tools/ventus_perf/wrap.py`, reject unsupported backends for Phase 1 before spawning the workload; this rejection must be explicit and must not alter the behavior of non-perf direct runs outside the wrapper.
- Implement wrapper-managed pass execution with `subprocess.Popen`, not a fire-and-forget helper:
  - create `stdout.log` / `stderr.log`
  - record `start_mono_ns = time.monotonic_ns()`
  - spawn child and capture `proc.pid`
  - write `pass.begin.json` immediately after successful spawn with `pid`, `parent_pid`, `argv`, `cwd`, `stdout_path`, `stderr_path`, and env summary
  - wait for child completion and read `proc.returncode`
  - compute `end_mono_ns` / `duration_ns`
  - map `returncode == 0 -> completed`, `returncode > 0 -> failed`, `returncode < 0 -> signaled` with `term_signal = -returncode`
  - enumerate canonical event files from the pass directory
  - write final `pass.json` atomically
  - if spawn fails before `proc.pid` exists, write a terminal `pass.json` with `state=failed` and no `pid`, and do not fabricate `pass.begin.json`

- [ ] **Step 4: Implement the wrapper env builder and re-run the Python wrapper tests**

Run:

```bash
timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_wrap.py'
```

Expected: wrapper tests pass, proving unsupported backends are rejected explicitly and pass lifecycle is finalized by the wrapper.

- [ ] **Step 5: Commit the PTX driver + wrapper pass instrumentation**

```bash
git add driver/driver/ptx_device/ventus.cpp tools/ventus_perf/wrap.py tools/ventus_perf/tests/test_wrap.py
git commit -m "feat: instrument ptx driver perf spans"
```

### Task 5: Implement the wrapper CLI and offline report artifacts

**Files:**
- Create: `tools/ventus-perf.py`
- Create: `tools/ventus_perf/cli.py`
- Modify: `tools/ventus_perf/report.py`
- Modify: `tools/ventus_perf/wrap.py`
- Modify: `tools/ventus_perf/tests/test_wrap.py`
- Modify: `tools/ventus_perf/tests/test_report.py`

- [ ] **Step 1: Write the failing CLI test for `run` and `report`**

```python
import pathlib
import shutil
import subprocess
import tempfile
import unittest


class CliTests(unittest.TestCase):
    def test_report_writes_summary_and_json_views(self) -> None:
        fixture = pathlib.Path(__file__).resolve().parent / "fixtures" / "minimal_experiment"
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = pathlib.Path(tmpdir) / "experiment"
            shutil.copytree(fixture, work_dir)
            proc = subprocess.run(
                ["python3", "tools/ventus-perf.py", "report", str(work_dir)],
                cwd=pathlib.Path(__file__).resolve().parents[2],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        self.assertIn("Baseline Attribution", proc.stdout)
```

- [ ] **Step 2: Run the CLI tests and verify they fail before `tools/ventus-perf.py` exists**

Run:

```bash
timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_*.py'
```

Expected: missing file or missing CLI entrypoint.

- [ ] **Step 3: Implement the minimal wrapper/report CLI**

```python
def main(argv: list[str]) -> int:
    # Background: baseline perf attribution for the current ptx/sbtsim path.
    # Flow: parse args -> dispatch into ventus_perf.cli -> run wrapper/report logic.
    # Usage: python3 tools/ventus-perf.py run -- <cmd>
    #        python3 tools/ventus-perf.py report <experiment-dir|pass-dir>
    from ventus_perf.cli import main as cli_main
    return cli_main(argv)
```

Implementation requirements:

- `tools/ventus-perf.py` must remain the only user-facing script at `tools/` level for this tool.
- The multi-file implementation must live under `tools/ventus_perf/`; do not spread related modules directly in `tools/`.
- The direct-execution path `python3 tools/ventus-perf.py ...` must work without `PYTHONPATH`; structure imports accordingly and do not rely on `from tools...` package imports inside the entry script.
- `run` must create `build/ventus-perf/<experiment-id>/`, write `experiment.begin.json`, execute warmup/measured passes, then atomically write `experiment.json`.
- `run` must support only the Phase 1 `ptx` / `sbtsim` backend path and reject unsupported backends explicitly before spawning a pass.
- `run` must write `pass.begin.json` immediately after successfully spawning each pass subprocess and finalize `pass.json` only after collecting the real subprocess return code and artifact paths.
- `run` must capture each pass `stdout.log` and `stderr.log`.
- `run` should use a helper shaped like `run_pass(...) -> dict` that returns the final pass manifest assembled from:
  - wrapper-known facts: `pass_id`, `pass_type`, `argv`, `cwd`, env summary, `stdout_path`, `stderr_path`, `pid`
  - subprocess facts: `returncode`, `term_signal`, `start_mono_ns`, `end_mono_ns`, `duration_ns`
  - filesystem facts: `event_files`, `event_counts`, `artifact_paths`, `recorder_errors`
- `run_pass(...)` must also handle spawn-before-begin failure by returning a terminal `failed` manifest without a fabricated child `pid`.
- `report` must always write:
  - `reports/summary.txt`
  - `reports/summary.json`
  - `reports/timeline.json`
  - `reports/kernels.json`
- `report` must include `summary.wall_time_stats.mean_ns|min_ns|max_ns|stddev_ns` in machine-readable output.
- `report` must accept either an `experiment-dir` or a single wrapper-generated `pass-dir`; the latter writes outputs into `<pass-dir>/reports/` without inventing a direct-run lifecycle.
- `report` must print the same concise summary text to stdout.
- `report` must recompute `concurrency_detected` from canonical events and expose `summary.sub_buckets` for PTX-path drill-downs.
- Phase 1 baseline-only reports do not need to create `reports/profiler.json`; that artifact is reserved for later phases when profiler passes exist.
- `report` must not require `events.sbt_ptx.jsonl` for Phase 1 completeness; parent-side `generate_ptx_via_sbt` timing in canonical `vt` events is sufficient.
- If a pass is incomplete or failed, include it in the report explicitly instead of dropping it.

- [ ] **Step 4: Re-run the Python CLI/report tests until they pass**

Run:

```bash
timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_*.py'
```

Expected: all wrapper/report tests pass.

- [ ] **Step 5: Commit the wrapper CLI**

```bash
git add tools/ventus-perf.py tools/ventus_perf/cli.py tools/ventus_perf/report.py tools/ventus_perf/wrap.py tools/ventus_perf/tests/test_wrap.py tools/ventus_perf/tests/test_report.py
git commit -m "feat: add ventus perf wrapper and report cli"
```

### Task 6: Run end-to-end verification on one real measured pass

**Files:**
- Modify: none unless the smoke uncovers defects

- [ ] **Step 1: Verify the synthetic tests are still green**

Run:

```bash
cmake --build build/driver-perf --target test_perf_recorder -j4
timeout 60s ctest --test-dir build/driver-perf -R perf_recorder --output-on-failure
timeout 60s python3 -m unittest discover -s tools/ventus_perf/tests -t tools -p 'test_*.py'
```

Expected: all targeted tests pass before the runtime smoke.

- [ ] **Step 2: Execute a single-pass PTX smoke run against freshly installed artifacts in an isolated prefix**

Run:

```bash
test -d install
test -x pocl/build/examples/matadd/matadd
SMOKE_ROOT=$PWD/build/ventus-perf-smoke
SMOKE_PREFIX=$SMOKE_ROOT/install

rm -rf "$SMOKE_ROOT"
mkdir -p "$SMOKE_ROOT"
cp -a --reflink=auto ./install "$SMOKE_PREFIX"

cmake -S sbtsim -B build/sbtsim-perf \
  -DCMAKE_INSTALL_PREFIX=$SMOKE_PREFIX \
  -DSBT_SPIKE_ENCODING_H=$PWD/spike/riscv/encoding.h
cmake --build build/sbtsim-perf --target sbt_ptx -j4
cmake --install build/sbtsim-perf

cmake -S driver -B build/driver-perf \
  -DCMAKE_INSTALL_PREFIX=$SMOKE_PREFIX \
  -DVENTUS_INSTALL_PREFIX=$SMOKE_PREFIX \
  -DSPIKE_SRC_DIR=$PWD/spike \
  -DDRIVER_ENABLE_AUTOSELECT=ON \
  -DDRIVER_ENABLE_PTX=ON
cmake --build build/driver-perf --target auto_select_driver ptx_driver -j4
cmake --install build/driver-perf

cmake -S pocl -B build/pocl-perf \
  -DENABLE_HOST_CPU_DEVICES=OFF \
  -DENABLE_VENTUS=ON \
  -DENABLE_ICD=ON \
  -DDEFAULT_ENABLE_ICD=ON \
  -DENABLE_TESTS=OFF \
  -DSTATIC_LLVM=OFF \
  -DVENTUS_INSTALL_PREFIX=$SMOKE_PREFIX \
  -DCMAKE_INSTALL_PREFIX=$SMOKE_PREFIX \
  -DINSTALL_OPENCL_HEADERS=ON \
  -DPOCL_INSTALL_OPENCL_HEADER_DIR=$SMOKE_PREFIX/include/CL
cmake --build build/pocl-perf -j4
cmake --install build/pocl-perf

env \
  VENTUS_INSTALL_PREFIX=$SMOKE_PREFIX \
  PATH=$SMOKE_PREFIX/bin:$PATH \
  LD_LIBRARY_PATH=$SMOKE_PREFIX/lib:${LD_LIBRARY_PATH:-} \
  OCL_ICD_VENDORS=$SMOKE_PREFIX/lib/libpocl.so \
  POCL_DEVICES=ventus \
  POCL_ENABLE_UNINIT=1 \
  VENTUS_BACKEND=ptx \
  GPU_SBT_PTX=$SMOKE_PREFIX/bin/sbt_ptx \
  VENTUS_SBT_PTX=$SMOKE_PREFIX/bin/sbt_ptx \
  timeout 60s python3 tools/ventus-perf.py run --warmup 0 --repeat 1 -- ./pocl/build/examples/matadd/matadd
```

Expected:

- command exits `0`
- a new `build/ventus-perf/<experiment-id>/experiment.json` exists
- `reports/summary.txt`, `reports/summary.json`, `reports/timeline.json`, and `reports/kernels.json` are written
- the run actually executes with `VENTUS_BACKEND=ptx`
- the temporary prefix starts from a clone-on-write copy of the existing `./install` tree, so non-modified dependent subprojects remain available
- `sbt_ptx` resolves from `$SMOKE_PREFIX/bin/sbt_ptx`, not an old `install/bin/sbt_ptx`
- stdout contains a short `Baseline Attribution` section

- [ ] **Step 3: If the smoke prerequisites or targeted rebuild/install prerequisites are missing, report that explicitly instead of faking success**

Record this exact note in the implementation log:

```text
Runtime smoke skipped: `./install`, `pocl/build/examples/matadd/matadd`, or the targeted `sbtsim`/`driver`/`pocl` rebuild prerequisites are unavailable in this workspace. Synthetic tests passed; real PTX-path verification still pending on a rebuildable environment.
```

- [ ] **Step 4: Commit only if all required targeted tests passed and the smoke was either green or explicitly skipped for missing prerequisites**

```bash
git add -A
git commit -m "feat: complete phase1 ventus perf baseline attribution"
```

## Final Verification Checklist

- [ ] `driver/codetests/test_perf_recorder.cpp` passes under `ctest` within 60 seconds.
- [ ] `tools/ventus_perf/tests/test_report.py` passes under `unittest` within 60 seconds.
- [ ] `tools/ventus_perf/tests/test_wrap.py` passes under `unittest` within 60 seconds.
- [ ] One real measured PTX pass is either verified against freshly installed smoke artifacts or explicitly skipped due to missing rebuild prerequisites.
- [ ] No code path silently disables recording after `VENTUS_PERF=1`.
- [ ] Reporter consumes only canonical `events.*.jsonl`, `pass.begin.json`, and `pass.json`.
- [ ] Unsupported backends are rejected explicitly by the wrapper, while legacy non-perf runs remain unaffected.

## Deferred Work

- Phase 2: `cyclesim` / `rtlsim` / `spike` producer parity and backend-specific reporting.
- Phase 3: `nsys` / `ncu` pass orchestration, profiler artifacts, and supplement views.
- Phase 4: protobuf export/import, CI integration, and compare views.
