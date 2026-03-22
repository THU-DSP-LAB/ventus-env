# Ventus Perf Phase 1 Execution Notes / Deviations

## 2026-03-20: recorder out-dir emptiness rule conflicts with wrapper-owned pass manifests

### Background

The spec and implementation plan require both of these behaviors:

1. `tools/ventus_perf/wrap.py` must write `pass.begin.json` immediately after a child process is successfully spawned so that `pid` is factual.
2. The runtime recorder should reject `VENTUS_PERF_OUT_DIR` when the target directory already exists and is non-empty.

For wrapper-managed runs, these requirements create a real race on the same pass directory. If the parent writes `pass.begin.json`, `stdout.log`, or `stderr.log` before the child-side recorder initializes, the recorder would fail even though the directory state was produced by the wrapper itself.

### Chosen adjustment

The recorder implementation keeps the "do not overwrite unexpected content" rule, but treats these wrapper-owned files as allowed pre-existing contents inside the pass directory:

- `pass.begin.json`
- `pass.json`
- `stdout.log`
- `stderr.log`

Any other pre-existing entry still causes explicit recorder initialization failure.

### Why this change is necessary

Without this adjustment, normal wrapper-managed Phase 1 runs can fail nondeterministically depending on scheduling between parent manifest creation and child recorder initialization. That directly harms the user-facing profiling workflow and makes the wrapper feel unreliable.

### Code scope

Current implementation scope:

- `driver/common/ventus_perf_recorder.cpp`

Expected follow-on scope as wrapper support lands:

- `tools/ventus_perf/wrap.py`
- report/tests that rely on wrapper-managed pass directories

### Rollback / alternative

If a stricter interpretation is preferred later, an alternative would be redesigning the wrapper/runtime directory contract so the recorder writes into a dedicated child-owned subdirectory rather than the pass root. That would be a broader layout change and would also require corresponding reporter updates.

## 2026-03-20: smoke-prefix installation had to use minimal artifact overlay instead of full `cmake --install`

### Background

The Phase 1 smoke verification was intended to run against an isolated prefix under `build/ventus-perf-smoke/install`.

Two repository-local installation issues blocked a straightforward `cmake --install` flow:

1. `build/driver-perf` install scripts still expect `spike_driver` artifacts, but the local spike driver build was not available in this environment (`spike_main.h` missing during build), even though the new perf work only needed `ptx_driver` and `auto_select_driver`.
2. `cmake --install pocl/build --prefix <smoke-prefix>` wrote into the workspace `install/` tree instead of the requested isolated prefix in this local setup.

### Chosen adjustment

For smoke verification only, the isolated prefix was prepared by:

- copying the existing `install/` tree into `build/ventus-perf-smoke/install`
- overlaying freshly built `libptx_driver.so`
- overlaying freshly built `libauto_select_driver.so`
- re-pointing `libventus_driver.so` to `libauto_select_driver.so`
- overlaying freshly built `libpocl.so*`
- overlaying freshly built `libpocl-devices-ventus.so`

This kept the smoke environment isolated enough for validation without forcing unrelated spike/install refactors into the perf change.

### Why this change is necessary

The alternative was either:

- broadening this perf task into unrelated driver-install and PoCL-install infrastructure repair, or
- skipping real runtime verification entirely.

Given the user requirement to prioritize a working Phase 1 path and the fact that the install issues were not caused by the perf design itself, the minimal overlay approach was the lowest-risk way to keep verification meaningful.

### Code / artifact scope

No source-level behavior change was introduced for the product itself. The deviation only affected verification-time artifact staging:

- `build/driver-perf/driver/ptx_device/libptx_driver.so`
- `build/driver-perf/driver/auto_select/libauto_select_driver.so`
- `pocl/build/lib/CL/libpocl.so.2.11.0`
- `pocl/build/lib/CL/devices/ventus/libpocl-devices-ventus.so`
- `build/ventus-perf-smoke/install/`

### Rollback / alternative

If the repository’s install flow is repaired later, the smoke procedure can return to a normal `cmake --install ... --prefix <isolated-prefix>` path with no special overlay step.
