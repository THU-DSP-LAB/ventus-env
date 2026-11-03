# 从远端分支复现纹理软件路径

本文对应 `raytracing_develop` 中的纹理模型及 Vulkan→Mesa→Ventus LLVM→Spike 软件采样。以本仓库分支为版本入口，依照 gitlink 初始化依赖即可，不需要外部 Mesa 工作树或旧实验日志。它不包含正式 fragment 光栅化、RTL 验收或性能结论。

## 固定依赖

| 子模块 | 纹理功能基线提交 |
| --- | --- |
| Mesa | `8d2ba4ac6d92963494d225e8e66f9a6e70346194` |
| Ventus LLVM | `88d9b6874ef051f9d626045e389aa59632997333` |
| Spike | `4b34446ada05775dfd0da98cd6e978ec37ae8497` |
| Driver | `0881c396a81fd081d454b8991d113f3fd3ed0bc5` |
| Driver membox | `fdccb9a64675465e682136979d9dc394afc89b88` |

父仓库固定提交，不要求在子模块中切换同名分支。不要使用 `git submodule update --remote`，否则会绕过该版本组合。GPGPU、POCL、SystemC 和其他模拟器不参与本软件纹理验收，不必初始化。

## Linux 主机依赖

以 Ubuntu 24.04、Python 3.12、GCC 13 为复现环境。本机 LLVM 20.1.2 来自 Ubuntu noble-updates/noble-security 的 universe；其他发行版可参考 [LLVM 官方软件源](https://apt.llvm.org/)，安装明确版本。Mesa 链接的是主机 LLVM 20，Ventus ELF 则由本仓库编译的 LLVM 16 定制后端生成，二者不能互换。

```bash
sudo apt-get install build-essential cmake ninja-build git python3 python3-venv \
    pkg-config flex bison device-tree-compiler libboost-regex-dev \
    libboost-system-dev libboost-thread-dev libfmt-dev libspdlog-dev \
    libelf-dev zlib1g-dev libzstd-dev libexpat1-dev libdrm-dev libunwind-dev \
    libvulkan-dev glslang-tools libx11-dev libx11-xcb-dev libxcb1-dev \
    libxcb-dri3-dev libxcb-present-dev libxcb-randr0-dev libxcb-shm0-dev \
    libxcb-sync-dev libxcb-xfixes0-dev libxshmfence-dev llvm-20-dev \
    libyaml-cpp-dev nlohmann-json3-dev ripgrep
```

构建使用 `build/texture-tools` 内的 Meson，避免系统旧 Meson。工具入口的子进程使用 `/usr/bin:/bin`，不会继承另一个 checkout 的 LLVM、ICD、`LD_LIBRARY_PATH` 或 Ventus 实验开关。所有构建产物都在本 checkout，Spike 安装到 `install-texture`。

## 获取版本与初始化

Git 对象操作串行执行，限制打包线程和内存。以下函数在 Linux 上使用 `prlimit`，也可替换为本机 systemd cgroup 限制；限制触发时先检查日志和锁状态，不要盲目重试或关闭限制。

```bash
git_safe() {
    timeout 1200s nice -n 10 ionice -c 3 \
      prlimit --as=6442450944 -- \
      git -c pack.threads=1 -c fetch.parallel=1 -c submodule.fetchJobs=1 "$@"
}
git_safe clone --depth 1 --branch raytracing_develop \
    https://github.com/THU-DSP-LAB/ventus-env.git ventus-texture
cd ventus-texture
git_safe submodule update --init --depth 1 --jobs 1 llvm spike driver mesa
git_safe -C driver submodule update --init --depth 1 --jobs 1 dependencies/membox

python3 -m venv build/texture-tools
build/texture-tools/bin/pip install 'meson==1.11.1' 'Mako==1.3.10' \
    'MarkupSafe==3.0.4' 'PyYAML==6.0.3' 'packaging==25.0'
python3 tools/texture_repro.py check
```

`check` 会拒绝未初始化或与父仓库 gitlink 不一致的依赖，不自动 checkout、安装或下载。需要完全锁定以后某次实验时，还应记录父仓库提交号；分支名本身会随开发前进。

## 构建

```bash
python3 tools/texture_repro.py build --jobs 8
```

依次构建定制 LLVM、RT 启动对象、Spike、仅 Spike 后端的 Driver，以及 Mesa。LLVM 使用 Release、AMDGPU/X86/RISCV targets、共享库、assertions OFF；只构建本路径所需工具，不生成完整 OpenCL libclc。`crt0.o` 从固定的 `libclc/riscv32/lib/crt0.S` 使用新构建的 Ventus clang 编译。

构建失败时查看 `artifacts/texture-build/run-*/commands.json` 与编号日志。每次构建记录使用独立目录，修复依赖后可只重跑一个组件，例如：

```bash
python3 tools/texture_repro.py build --component mesa --jobs 8
```

Mesa 的主机 LLVM 配置程序默认 `/usr/bin/llvm-config-20`，可通过 `--host-llvm-config` 指定其他 LLVM 20 安装。该参数不改变生成 Ventus ELF 的定制编译器。

## 数值验收

```bash
python3 tools/texture_repro.py verify --suite quick
python3 tools/texture_repro.py verify --suite full
```

`quick` 执行普通模型、一个 GPU fine 正例、一个故意错邻居反例和支持范围检查。`full` 增加 ASan/UBSan 模型、22 个功能组合的各 64-case 代表性回归，以及原完整 quad 验收的全部 28 条命令；历史两组分别使用主工作树/隔离工作树，本入口统一使用版本相同的本 checkout Mesa。

| 对照 | 预期 |
| --- | --- |
| 模型普通与 sanitizer | 各 21 suites、42,658,996 checks |
| 原完整 quad 矩阵 | 20 条正向、8 条预期负向；每条计数与 `tools/texture_repro/matrix.json` 一致 |
| 正向采样 | manifest 为 PASS，颜色误差不超过该 profile 的 tolerance，相关写回/完成/梯度检查通过 |
| 错邻居或错符号反例 | 退出码 1，且实际检出梯度错误；仅程序报错或超时不算成功 |
| 禁用私有 quad 开关反例 | 退出码 1，日志明确包含私有 quad 的编译拒绝原因 |
| 范围检查 | 由 `gpu-checks` 的 manifest 及退出状态确认拒绝边界 |

入口会使用新目录，保存源码版本、命令、返回码、逐项 manifest 和总报告；失败立即停止。默认删除已校验运行的逐分量 `numeric.csv*`，保留 manifest 中的结果与哈希；用 `--keep-numeric` 可保留原始 CSV。没有独立保存的历史 CSV 不影响重新生成输入和执行实验。

22 个代表性 profile 并非所有旧功能的穷举矩阵；完整 quad 的固定计数与普通模型作为共同回归基线。模型使用固定种子和独立 oracle，GPU 输入由 `tools/texture_model/gpu.cpp` 及 shader 模板产生，quad 输入由 `quad_fixture.hpp` 产生。全部细节见 [纹理功能模型说明](../tools/texture_model/README.md)。

## 原 any-hit 图像回归

这是独立的完整应用检查，除数值入口外还需要固定的公开 workload、GLM 和场景资源。初始化和准备过程包含 Git 网络操作，应在上述资源限制下串行执行：

```bash
git_safe submodule update --init --depth 1 --jobs 1 workloads/rt/SaschaWillems_Vulkan
git_safe -C workloads/rt/SaschaWillems_Vulkan submodule update \
    --init --depth 1 --jobs 1 external/glm

# prepare.sh 自身获取固定的公开 Vulkan-Assets；对子 Git 继承串行打包配置。
GIT_CONFIG_COUNT=3 \
GIT_CONFIG_KEY_0=pack.threads GIT_CONFIG_VALUE_0=1 \
GIT_CONFIG_KEY_1=fetch.parallel GIT_CONFIG_VALUE_1=1 \
GIT_CONFIG_KEY_2=submodule.fetchJobs GIT_CONFIG_VALUE_2=1 \
  timeout 1200s nice -n 10 ionice -c 3 prlimit --as=6442450944 -- \
  bash workloads/rt/prepare.sh

cmake -G Ninja -S build/rt-workload/source -B build/rt-workload/build -DUSE_HEADLESS=ON
ninja -C build/rt-workload/build -j 8 raytracingtextures
python3 tools/texture_repro.py verify --suite full --with-image
```

预期 160×96 PPM SHA-256 为：

```text
88b724540dc17b23161c638ebcbb71e9acaf73ff098b43764f9edf17cd5bebb0
```

图像门禁还检查真实 Spike 执行、独立 any-hit 入口、纹理 descriptor 与 push constants，不以“图片看起来正常”为通过条件。

## 结果解释

Quad 是完整、统一执行的逻辑四槽位实验，当前由 RT raygen 驱动；不代表正式 fragment/helper/分歧控制流语义已经支持。指令与编译器的历史修复已进入固定子模块提交。新机器重新构建后的二进制哈希可与旧机器不同，应保存新 manifest 并对照相同配置下的数值和图像门禁；不能把“构建成功”当作 GPU 功能验收。

纹理软件运行不要求外部 RTcore 文档仓库，光栅化及其 ABI 设计也不是运行上述实验的先决条件。新的图形实现应建立新结果基线，并保留这些正反例作为回归。

## 2026-11-03 独立检出验收

已从远端通过 HTTPS 在新目录检出，按固定 gitlink 获取依赖，重新构建上述全部组件及 headless 应用，没有复用旧 build/install、ccache 或 Git alternates。环境仍为同一台 Ubuntu 24.04 主机，不是重新安装的操作系统。测试使用父仓库提交 `5773217ae52b88f9ab1f613758758457743223ed`；本节与结果文件是其后的文档补充，不改变测试代码。

| 实测项目 | 结果 |
| --- | --- |
| 普通模型、ASan/UBSan 模型 | 各 21 suites、42,658,996 checks，全部通过 |
| 22 个代表性纹理 profile | 各 64 cases，全部通过 |
| 完整 quad/API 矩阵 | 20 条正向通过，8 条负向检出预期错误 |
| 验收入口自身测试 | 5 项通过，包括拒绝将超时或普通崩溃误判为负例成功 |
| 原 any-hit 图像 | 160×96，SHA-256 与上文基线一致 |

数值入口共完成 52 条检查命令，另执行图像门禁。完整精简对照见 [机器可读验收结果](texture-reproduction-results.json)：包含源码版本、主机工具和 Python 包版本、逐条参数、返回码、数值汇总及图像哈希。之后复现时，对照这些计数与误差条件，无需保存或下载原始大日志。

全新构建期间修复了两处复现入口问题：LLVM Ninja 应构建 `lld` 目标，由该目标生成 `ld.lld`；Mesa 的代码生成 Python 必须来自包含固定 Mako 依赖的工具 venv。两处修复均已进入上述测试提交，没有修改 ISA 或纹理算法。

## 补充：原有光追功能矩阵

在上述工具链、Mesa 和公开 workload 准备完成后，可补跑维护中的 15 项 MegaKernel 光追回归。先构建所需 headless 应用：

```bash
ninja -C build/rt-workload/build -j 4 \
  raytracingbasic raytracingcallable raytracingreflections raytracingshadows \
  raytracingintersection raytracingsbtdata raytracingtextures
```

再用与纹理验收相同的隔离环境运行已有入口。入口会清理指定的输出目录，下面预先检查目录不存在，避免覆盖历史结果：

```python
from pathlib import Path
import subprocess
from tools.texture_repro.workflow import environment

output = Path('artifacts/rtcore-spike/replay-full-matrix').resolve()
if output.exists():
    raise SystemExit('请换一个尚不存在的输出目录')
env = environment()
env.update(OUT_DIR=str(output), MESA_SHADER_CACHE_DISABLE='true', CCACHE_DISABLE='1')
with Path('rt-matrix-replay.log').open('x') as log:
    subprocess.run(['bash', 'tools/rtcore/verify_megakernel_feature_matrix.sh'],
                   env=env, stdout=log, stderr=subprocess.STDOUT,
                   timeout=3300, check=True)
```

从仓库根目录用 `python3` 执行这段代码。预期入口退出 0，日志包含 `PASS megakernel-feature-matrix gates=15`，`matrix.tsv` 有 15 条 PASS 记录。矩阵包括 Callable、嵌套 Callable、SBT 数据、实例索引与变换、迭代反射、Miss 内二次射线、多 descriptor set、程序化 AABB、剔除标志、透明纹理 any-hit 和三角形顶点位置获取。

其中 `callable_shader_restrictions` 是源码保护检查，确认两层有界 Callable 的实现与拒绝诊断存在；它不动态触发超深嵌套或 Callable 内 traceRay 的拒绝。其余 14 项包含真实 Spike 渲染并按各自脚本检查图像哈希、像素语义、运行日志或反汇编。不能将该矩阵等同于全部 Vulkan RT 扩展、其他执行 profile 或 RTL 验收。

2026-11-03 已在与纹理验收相同的独立构建环境中完成上述矩阵，15/15 项通过，耗时约 9 分 17 秒。其中 14 项运行检查共产生 33 张图像，逐一核对真实 Spike 执行与 completion frontier 通过；另一项为上述源码保护检查。没有修改测试预期、纹理实现或光追实现。逐项结果、脚本哈希、图像哈希和运行配置见 [光追回归结果](rt-regression-results.json)。
