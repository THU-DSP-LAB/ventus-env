# Ventus Development Environment

[中文版](README_zh_cn.md)

## System Requirements

* **Memory:** 32 GiB RAM or higher recommended
* **OS:** Ubuntu 24.04 recommended
* **Verilator:** 5.034 (build from source and add to `PATH`)
* **CIRCT firtool:** Install [firtool 1.62.0](https://github.com/llvm/circt/releases/download/firtool-1.62.0/firrtl-bin-linux-x64.tar.gz) and add to `PATH`
* **Other system dependencies:**

```bash
apt-get install \
    mold ccache ninja-build cmake clang clangd clang-format gdb \
    help2man perl perl-doc flex bison libfl2 libfl-dev zlib1g zlib1g-dev libgoogle-perftools-dev numactl \
    libfmt-dev libspdlog-dev libelf-dev libyaml-cpp-dev nlohmann-json3-dev \
    device-tree-compiler bsdmainutils ruby default-jdk python3-tqdm
```

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
```

The following environment variables adjust simulation behavior:

* `VENTUS_BACKEND=XXX` — Select the device/backend: `spike`|`isa`, `rtl`|`rtlsim`|`gpgpu`, `cyclesim`|`systemc`|`simulator`.
* `VENTUS_WAVEFORM=1` — Enable waveform dump: `rtlsim` → FST, `cyclesim` → VCD.
* `VENTUS_WAVEFORM_BEGIN` / `VENTUS_WAVEFORM_END` — Dump only a selected simulation interval for `rtlsim` (speeds up simulation). Not supported by `cyclesim`.
* `VENTUS_DUMP_RESULT=filename.json` — Save all device→host copies from OpenCL programs and their device addresses to a JSON file (useful for debugging).
* `VENTUS_TIMING_DDR=0` — Disable DDR timing in `cyclesim` (enabled by default). Current RTL simulation does not support DDR timing.
* `NUM_THREAD=32` — Number of threads per warp reported by the POCL device. For `rtlsim`/`cyclesim`, this should match hardware specs; for `spike`, any value is acceptable.
* `NUM_WARP=8` — Max warps per thread block reported by the POCL device. For `rtlsim`/`cyclesim`, match hardware specs; for `spike`, any value is acceptable.

## Testing

### Test Suites & Regression

This repository includes the GPU-Rodinia test suite (`rodinia/opencl`) and Ventus’s own OpenCL tests (`testcases/`).

The `regression-test.py` script runs a subset of the above as regression tests:

```bash
python3 ./regression-test.py                 # Uses spike by default
VENTUS_BACKEND=spike    python3 ./regression-test.py
VENTUS_BACKEND=rtlsim   python3 ./regression-test.py # Verilator-based Chisel RTL
VENTUS_BACKEND=cyclesim python3 ./regression-test.py # Cycle-accurate simulator
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

4. Occasionally (especially in Docker containers), running `VENTUS_BACKEND=rtl ./regression-test.py` may cause some test cases to fail. In `regression-test-logs/XXX.log` you may see:

    ```txt
    clang-16: error: unable to execute command: posix_spawn failed: Resource temporarily unavailable
    ```
    
    or
    
    ```txt
    terminate called after throwing an instance of 'std::system_error'
    what(): Resource temporarily unavailable
    ```
    
    This is typically due to excessive parallelism exceeding operating system or container resource limits. Regression tests using the RTL simulation backend are both multi-process and multi-threaded, and are therefore most prone to this issue. To mitigate, reduce the number of parallel processes, for example:
    
    ```bash
    VENTUS_BACKEND=rtl python3 regression-test.py -j 6
    ```
