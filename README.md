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
    libfmt-dev libspdlog-dev libelf-dev libyaml-cpp-dev nlohmann-json3-dev \
    device-tree-compiler bsdmainutils ruby default-jdk python3-tqdm
```

## 使用方法简介

### 部署

clone本仓库到本地路径（假如为`ventus-env/`）后：
```bash
cd ventus-env/
make init # 从github拉取所有需要的仓库与数据，耗时较长，请保持网络环境畅通
```

gpu-rodinia所使用的数据集会自动[下载](http://dspdev.ime.tsinghua.edu.cn/images/ventus_dataset/ventus_rodinia_data.tar.xz)并解压   
您也可以手动从[备用网址](https://cloud.tsinghua.edu.cn/d/ad60a4502fbb43daa45e/)下载rodinia_data.tar.xz并用如下命令解压
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

#### GPU-Rodinia测试集

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

如果希望运行rodinia中指定的单个测例，需进入到对应路径（`rodinia/opencl/*`）下手工处理  
* 运行`make`编译测例
* 使用`./run`脚本运行测例。很多测例需要命令行参数，`run`脚本中提供了示例，`regression-test.py`中支持的测例都提供了`run`脚本
```bash
source env.sh
cd rodinia/opencl/backprop  # for example
make
VENTUS_BACKEND=rtl ./run
```

#### OpenCL-CTS 测试
运行`bash build-ventus.sh --build cts`编译OpenCL CTS测试套件

运行某主题下的全部测例（以 compiler 为例）：
```bash
cd OpenCL-CTS/build/test_conformance/compiler
# 运行compiler主题下的全部测试并保存输出到日志文件
./test_compiler |& tee output.log
```

只运行某个指定测试kernel
```bash
cd OpenCL-CTS/build/test_conformance/basic
./test_basic --help  # check what testcase is support
./test_basic intmath_int4  # testcase name got from previous cmd
```

提供批量运行脚本，并行执行全部/大批量测试：
```bash
cd OpenCL-CTS
python3 run_test_parallel.py --json test_list_new.json --max-workers 20
```
- `--json`: 测试列表（示例为 `test_list_new.json`）。
- `--max-workers`: 并发工作进程数，按机器核心数与负载酌情设置。
- 运行时会显示“准备执行 N 个测试任务，最大并发数 = K”，随后打印每个测试的结果行（如 `[  OK  ] basic_intmath_long2`）。
- 建议在高并发下同时将标准输出落盘：`python3 run_test_parallel.py ... |& tee cts_parallel.log`。

Note:   
* OpenCL-CTS对于仿真来说规模偏大，运行时间较长，我们仅在spike仿真器上运行以检验软件栈的正确性
* spike仿真器默认会导出指令粒度的日志，运行CTS可能会产生极大量的日志文件。推荐大规模运行CTS测试前先修改build-ventus.sh中的spike编译选项，关闭日志输出：
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

### 开发环境

Chisel RTL使用scala开发环境，可以使用vscode scala metals插件导入mill配置（`build.sc`），或者使用Makefile提供的`make idea`后使用IntelliJ IDEA打开，详见对应仓库的README

其它项目均为C++开发环境，使用`cmake`或`bear`导出compile_commands.json文件后即可导入并使vscode插件或其它IDE正常工作

## 构建Docker

构建docker image：
```bash
docker build --target ventus-dev -t ventus-dev:latest .
docker build --target ventus -t ventus:latest .
```
其中`ventus-dev`包含所有代码仓库、编译产物与测试用例，适合开发人员使用   
`ventus`只包含最终编译产物与部分测试用例，适合尝鲜

## Trouble Shoot

1. 运行RTL verilator仿真时出现类似如下报错：
```txt
%Error: /opt/verilator/5.034/share/verilator/include/verilated.cpp:2729: VerilatedContext has 8 threads but model 'Vdut' (instantiated as 'TOP') was Verilated with --threads 11.
```
这是因为构建Verilated模型时选择的并行度过大，可能大于本机CPU逻辑线程数量   
可以将`ventus-env/gpgpu/sim-verilator/verilate.mk`与`ventus-env/gpgpu/sim-verilator-nocache/verilate.mk`中`VLIB_NPROC_DUT`的数值改小，重新编译`bash build-ventus.sh --build gpgpu`

2. 有时运行spike运行测例或回归测试后，命令行中输入字符无显示，可尝试盲输并运行`stty echo`

3. 有时Verilator报错类似`%Error: Internal Error: ../V3FuncOpt.cpp:162: Inconsitent terms`.   
这可能是未知的Verilator问题，但出现频率较低，一般来说重新运行即可
