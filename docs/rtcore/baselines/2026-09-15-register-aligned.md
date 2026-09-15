# Register-aligned RT baseline — 2026-09-15

## Source checkpoint

- gpgpu: `e5fe7e4` — traversal fixes, optional PMU, register-allocation assertions and directed tests.
- Mesa: `307af3f9be4` — padded launch SGPR/VGPR counts and resident PDS pool sizing.
- driver: `af26492` — launch-allocation contract, PMU and diagnostic output.
- Spike: `823614c3` — watertight intersection and bounded record diagnostics.

The parent commit containing this document pins the full submodule revisions.
No new branches or worktrees were created. Unrelated documents, IDE files,
build/install directories and the uncommitted Mesa direct-callback-group debug
bypass are excluded. The bypass is disabled in the performance command below.

## Correctness evidence

Run: `artifacts/rtcore-regression/runs/20260915T052151.690336Z-3092336`.
Both `raytracingreflections` and `raytracingshadows`, 16x9 RTL versus lavapipe:
zero differing pixels and zero maximum channel error; normal process exits.
The previous reflections result had 26 differing pixels.

Additional checks: register-bank stride-33 collision / stride-36 isolation test;
Mesa Ventus suite 7/7; both applications at 1x1 on Spike and RTL; regression
tool unit tests 16/16. Area is intentionally outside this test scope.

## First performance batch

Keep the existing 2-SM design, 32 lanes per warp, one warp per workgroup and
single-thread Verilator model. Use 32x18, one benchmark frame, fixed Verilator
seed 544222971, basic PMU, and no waveform/printf diagnostic elaboration.
Render each case against a fresh lavapipe reference; accept PMU results only
when the image and process checks pass. The 3600-second limit is per renderer.

```sh
env -u VENTUS_VK_RT_DEBUG_DIRECT_CALLBACK_GROUP_2 VENTUS_SPIKE_LOG=0 VENTUS_VERILATOR_SEED=544222971 python3 tools/rtcore.py regression --hardware-runner verilator --pmu-mode basic --case raytracingreflections --case raytracingshadows --include-candidates --width 32 --height 18 --benchmark-frames 1 --timeout-seconds 3600 --out-dir artifacts/rtcore-perf/baseline-20260915-basic
```

Keep raw `perf.json`, `perf-summary.json`, render logs, image comparisons,
fresh ELF and run provenance under the unique artifact run directory.
Compare simulated cycles, not host simulation wall time, across RTL versions.

`detailed` currently selects a configuration name but has no additional
`PMU_DETAILED`-gated instrumentation. This batch measures existing counters;
it does not claim lane reuse histograms or other future fine-grained metrics.
