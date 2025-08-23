# Ventus Development Environment

## 软硬件条件

* 推荐硬件内存48GiB以上
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

clone本仓库到本地路径（假如为`ventus-env/`）后：
```bash
cd ventus-env/
make init # 从github拉取所有需要的仓库，注意llvm仓库较大
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

使用ventus前必须设置环境变量：
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

本仓库`regression-test.py`包含了一些rodinia测试集的回归测试
```bash
python3 ./regression-test.py  # 默认使用spike
VENTUS_BACKEND=spike    python3 ./regression-test.py # 和上一条等效
VENTUS_BACKEND=rtlsim   python3 ./regression-test.py # 使用verilator仿真Chisel RTL
VENTUS_BACKEND=cyclesim python3 ./regression-test.py # 使用周期仿真器
```

推荐您先调整`regression-test.py`中的部分参数：
* `TIMEOUT_SCALE`：依据您的计算机运行速度调整测例的运行时间限制，运行速度慢需要调大此参数
* `MULTIPROCESS_NUM`：并行运行的测试进程数量，使用RTL仿真时每个测试进程又是多线程的（默认11线程），依据您的计算机具体情况调整

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
可以将`ventus-env/gpgpu/sim-verilator/verilate.mk`与`ventus-env/gpgpu/sim-verilator-nocache/verilate.mk`中`VLIB_NPROC_DUT = 11`的数值改小，重新编译`bash build-ventus.sh --build gpgpu`
