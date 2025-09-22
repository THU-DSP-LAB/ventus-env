# Ventus Development Environment

## 软硬件条件

* 推荐硬件内存32GiB以上
* 推荐系统环境：Ubuntu 24.04
* 推荐编译安装Verilator 5.034加入PATH
* 下载解压安装CIRCT [firtool 1.62.0](https://github.com/llvm/circt/releases/download/firtool-1.62.0/firrtl-bin-linux-x64.tar.gz)加入PATH
* 其它系统依赖：
```bash
apt-get install \
    mold ccache ninja-build cmake clang clangd clang-format gdb \
    help2man perl perl-doc flex bison libfl2 libfl-dev zlib1g zlib1g-dev libgoogle-perftools-dev numactl \
    libfmt-dev libspdlog-dev libelf-dev libyaml-cpp-dev device-tree-compiler bsdmainutils ruby default-jdk
```

## 使用方法简介

### 部署

clone本仓库到本地路径（假如为`ventus-env/`）后：
```bash
cd ventus-env/
make init # 从github拉取所有需要的仓库，注意llvm仓库较大，保持网络环境畅通
```

gpu-rodinia所使用的数据集需要额外[下载](https://cloud.tsinghua.edu.cn/d/ad60a4502fbb43daa45e/)rodinia_data.tar.xz并解压
```bash
tar -xf rodinia_data.tar.xz
```

使用编译脚本编译全部项目并安装到`ventus-env/install/`   
```bash
bash build-ventus.sh
```
若您更新或修改了部分子仓库，也推荐使用此脚本来编译，使用`--build XXX`参数可单独编译指定子仓库，详见`--help`参数打印的帮助文档

**使用ventus前必须设置环境变量：**
```bash
source env.sh
```

可以以不同仿真器作为实际执行后端运行openCL程序：
```bash
cd rodinia/opencl/gaussian
make
./run # 默认使用spike
VENTUS_BACKEND=spike    ./run # 和上一条等效
VENTUS_BACKEND=rtlsim   ./run # 使用verilator仿真Chisel RTL
VENTUS_BACKEND=cyclesim ./run # 使用周期仿真器
```

提供一些环境变量用来调整仿真行为，罗列如下：
* `VENTUS_BACKEND=XXX`选取使用哪种底层设备，可选值`spike`|`isa`, `rtl`|`rtlsim`|`gpgpu`, `cyclesim`|`systemc`|`simulator`
* `VENTUS_WAVEFORM=1`时可以使rtlsim后端导出fst波形文件，让cyclesim后端导出vcd波形文件
* `VENTUS_WAVEFORM_BEGIN`和`VENTUS_WAVEFORM_END`设定为一对数字可以使rtlsim后端只导出这一段仿真时间内的波形，以加速仿真。cyclesim后端不支持此功能
* `VENTUS_DUMP_RESULT=filename.json`可以将所有OpenCL程序从device端拷贝回host端的数据及其在设备端的地址保存到指定json文件中，辅助调试
* `NUM_THREAD=32`告知POCL设备端单个线程束（warp）支持多少线程。对于rtlsim和cyclesim应当与硬件规格对齐，对于spike可以任意调整数值
* `NUM_WARP=8`告知POCL设备端单个线程块最多有多少线程束（warp）。对于rtlsim和cyclesim应当与硬件规格对齐，对于spike可以任意调整数值

### 测试

本仓库的`regression-test.py`包含了一些rodinia测试集的回归测试
```bash
python3 ./regression-test.py  # 默认使用spike
VENTUS_BACKEND=spike    python3 ./regression-test.py # 和上一条等效
VENTUS_BACKEND=rtlsim   python3 ./regression-test.py # 使用verilator仿真Chisel RTL
VENTUS_BACKEND=cyclesim python3 ./regression-test.py # 使用周期仿真器
```

推荐您先调整`regression-test.py`中的部分参数：
* `TIMEOUT_SCALE`：依据您的计算机运行速度调整测例的运行时间限制，运行速度慢需要调大此参数
* `MULTIPROCESS_NUM`：并行运行的测试进程数量，使用RTL仿真时每个测试进程又是多线程的（默认8线程），依据您的计算机具体情况调整

#### OpenCL-CTS 测试

编译（确保已 `source env.sh`，并已初始化 `OpenCL-CTS` 子模块）：
```bash
cd OpenCL-CTS && git checkout dev-ventus
mkdir -p build
cmake -S . -B ./build \
    -DCL_INCLUDE_DIR=${VENTUS_INSTALL_PREFIX}/include \
    -DCL_LIB_DIR=${VENTUS_INSTALL_PREFIX}/lib \
    -DOPENCL_LIBRARIES=OpenCL
cmake --build ./build --config Release -j $(nproc)
```

调试：将 `--config` 改为 `Debug`，可使用 VS Code + gdb 进行断点调试。


运行某测例（以 compiler 为例）：
```bash
cd OpenCL-CTS/build/test_conformance/compiler
./test_compiler    # 运行 compiler 下的全部测试
```

只运行某个 kernel（以 atomics/atomic_add 为例）：
```bash
cd OpenCL-CTS/build/test_conformance/atomics
./test_atomics atomic_add
```

提示：`atomic_add` 等 kernel 名称可在 `OpenCL-CTS/test_conformance/atomics` 目录下搜索，通常由 CMake 中的 `ADD_TEST` 添加。

推荐将终端输出同时写入日志，便于后续检索：
```bash
./test_compiler |& tee output.log
```

批量运行脚本（并行执行全部/大批量测试）：
```bash
cd OpenCL-CTS
python3 run_test_parallel.py --json test_list_new.json --max-workers 20
```
- `--json`: 测试列表（示例为 `test_list_new.json`）。
- `--max-workers`: 并发工作进程数，按机器核心数与负载酌情设置。
- 运行时会显示“准备执行 N 个测试任务，最大并发数 = K”，随后打印每个测试的结果行（如 `[  OK  ] basic_intmath_long2`）。
- 建议在高并发下同时将标准输出落盘：`python3 run_test_parallel.py ... |& tee cts_parallel.log`。

### 开发环境

Chisel RTL使用scala开发环境，可以使用vscode scala metals插件导入mill配置（`build.sc`），或者使用Makefile提供的`make idea`后使用IntelliJ IDEA打开，详见README

其它项目均为C++开发环境，使用`cmake`或`bear`导出compile_commands.json文件后即可导入并使vscode插件或其它IDE正常工作

## 构建Docker

gpu-rodinia所使用的数据集需要额外[下载](https://cloud.tsinghua.edu.cn/d/ad60a4502fbb43daa45e/)rodinia_data.tar.xz并解压
```bash
cd ventus-env/
tar -xf rodinia_data.tar.xz
```

> 注意，若您先`make init`再解压rodinia_data.tar.xz，没有问题   
> 若您先解压rodinia_data.tar.xz导致`ventus-env/rodinia/`非空，之后再`make init`会导致rodinia子仓库clone异常   
> 需要将`ventus-env/rodinia/`路径清空再`make init`

构建docker image
```bash
docker build --target ventus-dev -t ventus-dev:latest .
docker build --target ventus -t ventus:latest .
```

## Trouble Shoot

运行RTL verilator仿真时出现类似如下报错：
```txt
%Error: /opt/verilator/5.034/share/verilator/include/verilated.cpp:2729: VerilatedContext has 8 threads but model 'Vdut' (instantiated as 'TOP') was Verilated with --threads 11.
```
这是因为构建Verilated模型时选择的并行度过大，可能大于本机CPU逻辑线程数量   
可以将`ventus-env/gpgpu/sim-verilator/verilate.mk`与`ventus-env/gpgpu/sim-verilator-nocache/verilate.mk`中`VLIB_NPROC_DUT`的数值改小，重新编译`bash build-ventus.sh --build gpgpu`
