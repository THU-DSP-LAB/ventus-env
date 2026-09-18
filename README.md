# Ventus Development Environment

[中文版](README_zh_cn.md)

## System Requirements

* **Memory:** 32 GiB RAM or higher recommended
* **OS:** Ubuntu 24.04 recommended
* **Verilator:** 5.034 (build from source and add to `PATH`)
* **CIRCT firtool:** Install [firtool 1.62.0](https://github.com/llvm/circt/releases/download/firtool-1.62.0/firrtl-bin-linux-x64.tar.gz) and add to `PATH`
* **CUDA (optional, for `VENTUS_BACKEND=ptx`):** NVIDIA driver + CUDA toolkit (for headers/tools). The PTX backend uses CUDA Driver API (`libcuda.so`) at runtime and `find_package(CUDAToolkit)` at build time.
* **Other system dependencies:**

```bash
apt-get install \
    mold ccache ninja-build cmake clang clangd clang-format gdb \
    help2man perl perl-doc flex bison libfl2 libfl-dev zlib1g zlib1g-dev libgoogle-perftools-dev numactl \
    libfmt-dev libspdlog-dev libelf-dev libyaml-cpp-dev nlohmann-json3-dev \
    device-tree-compiler bsdmainutils ruby default-jdk python3-tqdm \
    libvulkan-dev glslang-tools python3-venv python3-pip
```

The Mesa submodule requires Meson 1.4 or newer. Ubuntu 24.04 ships an older
version; a repository-local environment can be prepared with:

```bash
python3 -m venv build/meson-venv
build/meson-venv/bin/pip install 'meson>=1.4,<2'
```

`build-ventus.sh` detects this environment automatically.

A Dockerfile that bundles all dependencies is provided below.

## Quick Start

### Setup

After cloning this repository to a local path (e.g., `ventus-env/`):

```bash
cd ventus-env/
make init # Fetches all required repositories and data from GitHub; this can take a while—ensure a stable connection.
```

The dataset used by gpu-rodinia will be downloaded and extracted automatically:
[http://dspdev.ime.tsinghua.edu.cn/images/ventus\_dataset/ventus\_rodinia\_data.tar.xz](http://dspdev.ime.tsinghua.edu.cn/images/ventus_dataset/ventus_rodinia_data.tar.xz)

You may also download `rodinia_data.tar.xz` manually from the mirror:
[https://cloud.tsinghua.edu.cn/d/ad60a4502fbb43daa45e/](https://cloud.tsinghua.edu.cn/d/ad60a4502fbb43daa45e/)
and extract it with:

```bash
tar -xf rodinia_data.tar.xz
```

Build all projects and install them under `ventus-env/install/`:

```bash
bash build-ventus.sh
```

If you have updated or modified certain sub-repositories, we recommend using this script as well. Use `--build XXX` to build a specific sub-repo. See `--help` for details.

**Before using Ventus, set environment variables:**

```bash
source env.sh
```

Run OpenCL programs with different simulators as the execution backend:

```bash
cd rodinia/opencl/gaussian
make
./run                         # Uses spike by default
VENTUS_BACKEND=spike    ./run # Same as above
VENTUS_BACKEND=rtlsim   ./run # Verilator-based Chisel RTL simulation
VENTUS_BACKEND=cyclesim ./run # Cycle-accurate simulator
VENTUS_BACKEND=ptx      ./run # PTX backend (SBT ELF->PTX + CUDA Driver API)
```

The following environment variables adjust simulation behavior:

* `VENTUS_BACKEND=XXX` — Select the device/backend: `spike`|`isa`, `rtl`|`rtlsim`|`gpgpu`, `cyclesim`|`systemc`|`simulator`.
  * PTX backend: `ptx` (requires building `build-ventus.sh --build "ptx"` and CUDA availability).
* `VENTUS_WAVEFORM=1` — Enable waveform dump: `rtlsim` → FST, `cyclesim` → VCD.
* `VENTUS_WAVEFORM_BEGIN` / `VENTUS_WAVEFORM_END` — Dump only a selected simulation interval for `rtlsim` (speeds up simulation). Not supported by `cyclesim`.
* `VENTUS_DUMP_RESULT=filename.json` — Save all device→host copies from OpenCL programs and their device addresses to a JSON file (useful for debugging).
* `VENTUS_TIMING_DDR=0` — Disable DDR timing in `cyclesim` (enabled by default). Current RTL simulation does not support DDR timing.
* `VENTUS_RTL_EXTMEM_LATENCY=100` — Fixed non-LDS memory response latency used when generating the cache-enabled Verilator RTL; default is 100 cycles.
* `VENTUS_RTLSIM_PERF_JSON=path` — Write the final cache-enabled Verilator PMU snapshot, including RTcore boundary counters, as JSON when the device closes.
* `VENTUS_SPIKE_LOG=1` - Enable `spike` simulator to output instruction-granularity log into file. (disabled by default)
* `NUM_THREAD=32` — Number of threads per warp reported by the POCL device. For `rtlsim`/`cyclesim`, this should match hardware specs; for `spike`, any value is acceptable.
* `NUM_WARP=8` — Max warps per thread block reported by the POCL device. For `rtlsim`/`cyclesim`, match hardware specs; for `spike`, any value is acceptable.

### Vulkan Ray Tracing Baseline

2026-09-18 开发检查点（不是替换下述历史性能基线）：gpgpu `a17af22`、
Mesa `549471cacd0`。包含 lane 独立栈、固定分组多 IU 派发，以及启动寄存器
分配修复：按 `max(shader需求, crt0需求)` 再对齐，SGPR 下限32，VGPR
覆盖v32，避免相邻 warp 覆盖启动临时寄存器。当前源码默认 **1 SM、
8 LSU MSHR、外存延迟100 cycles**；1-cycle 仅是独立构建时的实验覆盖值。

当前检查点的 shadows 单SM测试覆盖1×1、16×9、32×1/2/4/8/9/16/32，
以及固定2:1的4×2至32×16曲线。固定比例曲线加1×1的九点中，100-cycle
basic/detailed 周期及共有RT计数一致，两种延迟下18次detailed图像回归均与
lavapipe零差异。测试在提交前运行，原始provenance与二进制哈希不回写。
构建、安装及测试产物不纳入Git；本地报告见
`artifacts/rtcore-regression/reports/2026-09-18-sm1-固定比例曲线/报告.md` 和
`artifacts/rtcore-regression/reports/2026-09-18-sm1-外存延迟对照/报告.md`。
旧的scheduler阻塞分类及按单路扣减的未选中计数尚未完全适配多发，不能用
其中的零值断言无阻塞。尚未实施无candidate的traverse边界warp切换。

当前 RTCore 性能基线标签：`baseline/rtcore-embree4-2026-09-15`。
该检查点固定顶层及子模块源码，采用 Embree 3.13.4 建树，保持 VTAS ABI
和现有 RTL 不变；尚未加入 resident AS cache 或更宽的 Scheduler 发射。

16×9、seed `544222971`、单帧、2 SM、32 lane、单 Verilator 线程、外部读延迟
100 cycles：shadows 为 **94,299 cycles**，reflections 为 **160,499 cycles**。
两例 basic/detailed 周期、共有 PMU 计数和 shader ELF 完全一致，图像与
lavapipe 零差异；另外验证了五个例子的 1×1 图像。off 模式及更大分辨率
不在本次基线验收范围内，不代表 Vulkan 全面兼容性验证。

复现时应显式选择 `VENTUS_VK_BVH_BUILDER=embree4`，并在 canonical
`mesa/build-ventus` 启用 `-Dventus_embree=enabled`。可用
`-Dventus_embree_prefix=/absolute/embree-sdk` 指定 Embree 3 二进制 SDK；
SDK 不包含在 Git 中。建树策略、栈预算和构建说明见
[BVH.md](mesa/src/ventus/vulkan/BVH.md)。

验收记录均在 `artifacts/rtcore-regression/runs/`：detailed 为
`20260915T133151.467205Z-831583`，basic 为
`20260915T135354.348742Z-1026213`，同二进制 Morton 对照为
`20260915T133518.072949Z-867553`。测试在提交前执行，provenance 记录了
当时的未提交源码指纹；未将已有、且测试中禁用的 callback 调试改动纳入提交。
详细[中文报告](docs/rtcore/baselines/2026-09-15-embree4.md)和原始 artifacts
按现有忽略策略仅保存在本地，不随 checkout 恢复；本节保留验收摘要。

The pinned ray-tracing submodules can execute a real Vulkan RT vertical slice
on the Ventus Spike backend or the cache-enabled Verilator RTL backend. The
verified path runs the SaschaWillems
`raytracingshadows` application through the Mesa Ventus ICD, compiles its
SPIR-V shaders through NIR/LLVM into a Ventus RISC-V ELF, builds and uploads
triangle BLAS plus instance TLAS data, executes the generated raygen kernel on
Spike, and reads the storage image back as PPM.

The manifest-driven regression is the sole full-application correctness gate.
It preserves every invocation under
`artifacts/rtcore-regression/runs/<run-id>/<case>/<geometry>/software|spike|verilator`.
The command prints the run directory before rendering. Older results in the
previous `<case>/<geometry>` layout remain untouched:

```bash
# Functional reference (default) and RTL/PMU execution use the same application.
python3 tools/rtcore.py regression --hardware-runner spike --width 4 --height 4
bash build-ventus.sh --build rtcore-rtl
python3 tools/rtcore.py regression --hardware-runner verilator --width 1 --height 1
# Capture both backends without overwriting either result.
python3 tools/rtcore.py regression --hardware-runner all --width 1 --height 1
# Include the callback, texture, recursion and SBT candidate cases explicitly.
python3 tools/rtcore.py regression --include-candidates --case raytracingshadows \
  --case raytracingsbtdata --case raytracingintersection --case raytracingreflections \
  --case raytracingtextures --hardware-runner all --width 4 --height 4
python3 tools/rtcore.py abi --check
```

The Verilator run emits `verilator/perf.json` and its validated
`verilator/perf-summary.json`; the ordinary `env.sh` default remains Spike.
Each renderer writes `begin.json` before launch and `render.json` on exit or
timeout, including the exit status and a bounded error excerpt. An image
captured before a process crash is retained for comparison but cannot pass
the regression. Comparisons include differing-pixel counts by row and column,
their bounding rectangle, and up to 16 RGB samples. With `--hardware-runner
all`, `comparison.spike-verilator.json` also directly compares the two hardware
backends. `provenance.json` records source state and runtime-library hashes.

For RTL image failures, first map the differing pixels to the scene and inspect
the corresponding source semantics. Recompute existing ray/AABB inputs and
turn the smallest failing case into a unit test before rebuilding the full
model. A new trace should distinguish a concrete pair of hypotheses and have
a bounded output size. Run normal regressions without detailed traces, and
advance from 1x1 and 4x4 to multi-warp images only after the smaller gates pass.

The implementation changes, build-target responsibilities, known build
pitfalls, and review entry points are collected in the Chinese
[`docs/rtcore-spike-p1.md`](docs/rtcore-spike-p1.md).

The all-example runner uses the prepared workload under
`testcases/RTcoreSpikeCase/SaschaWillems_Vulkan`; its manifest and maintained
application/shader overrides are versioned in `testcases`. The surrounding
SaschaWillems source, dependencies and configured build must already exist;
this is not supplied by the regression runner. `--workload-dir` and
`--build-dir` select another deliberately prepared tree. The older
`workloads/rt/prepare.sh` prepares a shadows-only upstream overlay, not this
all-example suite. Generated ELFs, logs, and images stay in `artifacts/`.

Implementation details and environment variables are documented in
[`mesa/src/ventus/README.md`](mesa/src/ventus/README.md), and workload
provenance is documented in
[`workloads/rt/README.md`](workloads/rt/README.md). This is a bounded functional
path, not Vulkan RT conformance. The current validated image baseline is
documented in [`docs/rtcore-spike-p1.md`](docs/rtcore-spike-p1.md).

### Performance Attribution for PTX Backend

Use `tools/ventus-perf.py` to run a wrapper-managed PTX performance pass and generate offline reports:

```bash
source env.sh
export VENTUS_BACKEND=ptx

python3 tools/ventus-perf.py run --repeat 1 -- ./run
```

The `run` subcommand currently supports:

```bash
python3 tools/ventus-perf.py run \
  --warmup 1 \
  --repeat 3 \
  -- ./run
```

Profiler passes can also be scheduled explicitly on supported phase1 backends:

```bash
python3 tools/ventus-perf.py run \
  --repeat 1 \
  --profile nsys \
  -- ./run

python3 tools/ventus-perf.py run \
  --repeat 1 \
  --profile ncu \
  --ncu-kernel matadd \
  -- ./run
```

Rules:

* `--ncu-kernel` requires `--profile ncu`
* profiler passes are accepted only for the wrapper-supported phase1 backends: `ptx`, `sbt`, `ptxsim`, `sbtsim`
* missing profiler tools are recorded as failed profiler passes; they are not silently skipped

The wrapper creates an experiment directory under `build/ventus-perf/<experiment-id>/` and writes:

* per-pass manifests such as `pass.begin.json` and `pass.json`
* canonical event logs such as `events.pocl.jsonl` and `events.vt.jsonl`
* offline reports under `reports/`, including `summary.txt`, `summary.json`, raw `timeline.json`, `kernels.json`, and Perfetto-compatible `perfetto.json`
* when an experiment contains profiler passes, `summary.txt` / `summary.json` also include a brief reference-only profiler summary; baseline attribution buckets still come only from measured passes
* `reports/profiler.json` only when the experiment contains profiler passes

To re-render reports from an existing experiment or a single wrapper-managed pass:

```bash
python3 tools/ventus-perf.py report build/ventus-perf/<experiment-id>/
python3 tools/ventus-perf.py report build/ventus-perf/<experiment-id>/passes/measure-0001/
```

Current support boundary:

* baseline wrapper-managed attribution currently supports only the wrapper-supported phase1 backends: `ptx`, `sbt`, `ptxsim`, `sbtsim`
* profiler pass orchestration is also limited to those same wrapper-supported phase1 backends
* unsupported backends are rejected explicitly by the wrapper

## Testing

### Test Suites & Regression

This repository includes the GPU-Rodinia test suite (`rodinia/opencl`) and Ventus’s own OpenCL tests (`testcases/`).

The `regression-test.py` script runs a subset of the above as regression tests:

```bash
python3 ./regression-test.py                 # Uses spike by default
VENTUS_BACKEND=spike    python3 ./regression-test.py
VENTUS_BACKEND=rtlsim   python3 ./regression-test.py # Verilator-based Chisel RTL
VENTUS_BACKEND=cyclesim python3 ./regression-test.py # Cycle-accurate simulator
VENTUS_BACKEND=ptx      python3 ./regression-test.py # PTX backend (requires CUDA)
```

Before running, we recommend tuning these options:

* `-t TIMEOUT_SCALE` — Scale timeouts based on your machine’s speed (increase if your system is slower).
* `-j JOBS` — Number of parallel test processes. With RTL simulation, each test process is multi-threaded (8 threads by default). Adjust to your machine.

To run a single test case manually, change to its directory and:

* Run `make` to build the test.
* Use the `./run` script to execute. Many tests require command-line arguments; the `run` script includes examples. All tests supported by `regression-test.py` provide a `run` script (tests with no arguments may omit it).

```bash
source env.sh
cd rodinia/opencl/backprop  # for example
make
VENTUS_BACKEND=rtl ./run
```

### OpenCL-CTS Tests

Build the OpenCL CTS test suite:

```bash
bash build-ventus.sh --build cts
```

Run all tests under a topic (e.g., `compiler`):

```bash
cd OpenCL-CTS/build/test_conformance/compiler
# Run all 'compiler' tests and save output to a log file
./test_compiler |& tee output.log
```

Run a specific test kernel:

```bash
cd OpenCL-CTS/build/test_conformance/basic
./test_basic --help        # List available testcases
./test_basic intmath_int4  # Testcase name from the previous command
```

Batch-run helper (execute all or many tests in parallel):

```bash
cd OpenCL-CTS
python3 run_test_parallel.py --json test_list_new.json --max-workers 20
```

* `--json`: Path to the test list (example: `test_list_new.json`).
* `--max-workers`: Degree of parallelism; set based on core count and system load.
* The runner prints “Preparing to execute N test tasks, max concurrency = K”, followed by per-test result lines (e.g., `[  OK  ] basic_intmath_long2`).
* For high concurrency, also log stdout to disk:
  `python3 run_test_parallel.py ... |& tee cts_parallel.log`.

**Notes**

* OpenCL-CTS is large for simulators and runs take a long time. We run CTS on `spike` only to verify software-stack correctness.
* The `spike` simulator produces instruction-level logs by default. Running CTS can generate *huge* logs. Before large CTS runs, consider disabling logs in `build-ventus.sh` by removing `--enable-commitlog`:

```diff
--- a/build-ventus.sh
+++ b/build-ventus.sh
@@ -183,7 +183,7 @@ build_spike() {
   # rm -rf ${SPIKE_BUILD_DIR} || true
   mkdir -p ${SPIKE_BUILD_DIR}
   cd ${SPIKE_BUILD_DIR}
-  ../configure --prefix=${VENTUS_INSTALL_PREFIX} --enable-commitlog
+  ../configure --prefix=${VENTUS_INSTALL_PREFIX}
   make -j${BUILD_PARALLEL}
   make install
 }
```

## Developer Setup

For the Chisel RTL, use a Scala development environment. You can import the `mill` config (`build.sc`) into VS Code via the Scala Metals plugin, or run `make idea` and open the project in IntelliJ IDEA. See each sub-repo’s README for details.

All other projects use C++. Export `compile_commands.json` with `cmake` or `bear` to enable proper language tooling in VS Code or other IDEs.

## Building Docker Images

Build the images:

```bash
docker build --target ventus-dev -t ventus-dev:latest .
docker build --target ventus -t ventus:latest .
```

* `ventus-dev` contains all repositories, build artifacts, and test suites—best for development.
* `ventus` includes only final build artifacts and a subset of tests—best for a quick tryout.

## Troubleshooting

1. **Verilator RTL simulation error like:**

   ```txt
   %Error: /opt/verilator/5.034/share/verilator/include/verilated.cpp:2729: VerilatedContext has 8 threads but model 'Vdut' (instantiated as 'TOP') was Verilated with --threads 11.
   ```

   The Verilated model was built with too much parallelism (possibly exceeding your CPU’s logical thread count). Reduce `VLIB_NPROC_DUT` in:
   `ventus-env/gpgpu/sim-verilator/verilate.mk` and
   `ventus-env/gpgpu/sim-verilator-nocache/verilate.mk`,
   then rebuild with:

   ```bash
   bash build-ventus.sh --build gpgpu
   ```

2. **No characters echoing in the terminal after running spike or regression tests:**
   Try typing blindly and run `stty echo`.

3. **Verilator internal error like** `%Error: Internal Error: ../V3FuncOpt.cpp:162: Inconsitent terms`.
   This appears to be an occasional Verilator issue. In most cases, simply re-run the build or simulation.
