# Ventus Spike RTcore P1 实现与复现说明

## 文档范围

本文记录 `ventus-env` 当前 Spike RTcore 功能模型的实现落点、仓库内依赖、构建
入口和验证方法，供代码审阅与复现使用。P1 的目标是接入 RTcore 模型，同时保持
现有 `raytracingshadows` 出图功能不变。

本文是 Ventus 实现说明，不替代 RTcore 仓库中的公共语义架构文档，也不把 Spike
功能模型描述成 RTL 时序模型。

## 本阶段改动

### Spike RTcore 模型

- `sim_t` 持有当前逻辑 SM 的 `RtCoreModel`，所有 warp 通过同一实例进入 RTcore；
  reset 会同时清空 RTcore 私有状态。当前 Spike 没有显式多 SM 放置模型，因此只
  实例化逻辑 SM0；以后增加 SM identity 时仍遵守“一 SM 一 RTcore”。
- `vt.rt.traverse` 和 `vt.rt.release` 以 warp 指令发射，携带 SIMT active mask 和
  每个 active lane 的 handoff slot；RTcore 在模型内部逐 lane 执行并逐 lane 返回
  traversal status。
- 暂停的 candidate traversal 使用 RTcore 私有 context 保存，key 同时包含执行
  context identity 与 handoff slot，`release`、kernel reset 会回收对应状态。
- Spike 模型不设置硬件 backpressure、resident warp/lane 上限或访存带宽限制；
  这些属于 RTL 性能模型，不属于无周期的功能模型。
- 语义测试覆盖 triangle、TLAS/BLAS、procedural AABB、closest candidate、
  accept/ignore、terminate、release、active mask 和私有 context 生命周期。

### Mesa 与 driver bridge

- per-lane RT ABI 更新为 v2：shader-visible PDS 只保留 ray/control/candidate/
  committed-hit/attribute/payload 等交换状态；暂停遍历的 cursor/frontier/stack
  由 Spike `RtCoreModel` 的 C++ 私有 context 承载，不写入 PDS 或 driver buffer。
- dispatch args 和 per-lane PDS 预留了 compiler/runtime CPS arena 与 16-byte header
  carrier。当前 compiler 没有产生非零 continuation frame size，因此已验证路径中
  arena 地址和 header 都为零；Spike RTCore 不读取这套 CPS 骨架。
- LLVM 工具、driver、Spike 和 workload 路径由仓库根目录解析，不再带有其他
  Ventus checkout 的绝对路径。
- Vulkan buffer device address 以实际绑定的 backing storage 为准，避免相邻
  wrapper object 形成重叠伪地址区间并把 BLAS 输入解析到错误 buffer。

### 自包含 workload

workload 使用公开上游与仓库内 overlay，不复制或读取外部冻结工作树：

| 输入 | 固定版本 |
| --- | --- |
| `SaschaWillems/Vulkan` | `3b843fbf667a89a1cfcc64405e9fc6f9018e03b4` |
| GLM | `1ad55c5016339b83b7eec98c31007e0aee57d2bf` |
| `SaschaWillems/Vulkan-Assets` | `a27c0e584434d59b7c7a714e9180eefca6f0ec4b` |
| 完整场景 SHA-256 | `be6633150d09b951db637fda2254317a94c701c948a57c7819d09403113fde28` |

`workloads/rt/prepare.sh` 校验以上版本，稀疏获取唯一需要的完整场景，在
`build/rt-workload/source/` 中复制上游源码并应用 Ventus overlay，不修改官方
子模块。

## 从干净 checkout 构建

Mesa 要求 Meson 1.4 或更新版本。Ubuntu 24.04 的系统版本不足时，先创建仓库内
环境：

```bash
python3 -m venv build/meson-venv
build/meson-venv/bin/pip install 'meson>=1.4,<2'
```

然后执行：

```bash
cd /path/to/ventus-env
git submodule update --init

bash build-ventus.sh \
  --build "rt-toolchain;spike;driver-spike;mesa;rt-workload"
```

各目标职责如下：

| 目标 | 产物与边界 |
| --- | --- |
| `rt-toolchain` | 构建 Ventus LLVM `llc`、`ld.lld`、`llvm-nm`，并从独立的 `llvm/build-rt-libclc/` 安装 RT 链接所需的 `crt0.o` |
| `spike` | 构建包含 RTcore 模型的 Spike，并安装 `libspike_main.so` |
| `driver-spike` | 只构建 Spike backend，不要求 RTL、cycle simulator、GVM、PTX 或 NVIDIA driver |
| `mesa` | 构建 Ventus Vulkan ICD、RT compiler 和 driver bridge |
| `rt-workload` | 固定并准备上游输入，以 headless 模式构建 `raytracingshadows` |

`rt-toolchain` 不把 libclc 作为 LLVM 子项目启用。完整 libclc 会默认生成 AMDGPU
等无关目标，并曾在 `__clc_nextafter` 上失败；RT 路径只需要独立 riscv32 构建
安装的 `crt0.o`。旧的 `llvm/build-libclc/` 可以保留，但 RT 构建不会再使用它。

## 运行完整门禁

构建完成后执行：

```bash
python3 tools/rtcore.py regression --hardware-runner spike --width 4 --height 4
```

门禁会完成以下检查：

- 重新构建选中的应用，并记录实际源码与运行库信息；
- renderer 正常结束、没有超时，且生成所需图片；
- 同次运行的软件参考与硬件图片满足 manifest 的像素误差阈值；
- Verilator 的 PMU schema 与所需计数器完整。

此入口使用已准备的 `testcases/RTcoreSpikeCase/SaschaWillems_Vulkan`，
不是旧 shadows-only overlay 的冻结哈希门禁；不能用旧图像哈希代替当前比较。

结果位于每次运行独立的目录：

```text
artifacts/rtcore-regression/runs/<run-id>/raytracingshadows/4x4/
```

该入口同时检查 renderer 正常结束、图片存在和软件/硬件像素比较；不再保留绕过
图片门禁的 RT full-app shell 脚本。

## 聚焦测试

Spike 静态与 RTcore 语义测试：

```bash
cd spike
python3 tests/ventus_extra_stage2_static.py
g++ -std=c++17 -Iriscv tests/ventus_rt_semantics.cc \
  -o build/ventus_rt_semantics_test
./build/ventus_rt_semantics_test
cd ..
```

Mesa Ventus compiler 测试：

```bash
build/meson-venv/bin/meson test \
  -C mesa/build-ventus --suite ventus --print-errorlogs
```

若 ccache 目录不可写，可在构建或测试命令前添加 `CCACHE_DISABLE=1`。CMake policy
deprecation warning 和 Spike 的 circular shared-library dependency warning 当前是
非致命提示；判断失败应查看第一条 `FAILED:`，不能只看最后一行
`ninja: build stopped`。

## 当前能力边界

2026-09-11 稳定检查：ABI v12；shadows、sbtdata、intersection、reflections、
textures 五例 4×4 软件／Spike／Verilator 比较通过（10/10 硬件结果）。RTL 与
Spike 全部逐像素一致；textures 对软件最多相差 1，其余四例完全一致。
RTL LocalState/SRAM 单测 7/7，Mesa Ventus 单测 7/7。此处不是大尺寸性能验收，
basic 尚无软件截图钩子，不列入通过集合。`--include-candidates` 只允许候选用例，
不会自动选中它们；完整五例命令见根 README。

- 该结果证明当前 Spike 功能模型保持完整阴影场景和衍生 shadow ray 的出图能力。
- 它不证明 RTL 调度、周期、backpressure、resident 容量或访存带宽。
- 它不是 Vulkan RT conformance 结论，也不表示 callable、indirect、AS
  serialization 等未实现能力已经可用。
- 首次初始化需要联网拉取本文固定的公开仓库；完成初始化和构建后，运行门禁不
  依赖另一个 Ventus checkout。

## 主要审阅入口

| 路径 | 审阅内容 |
| --- | --- |
| `build-ventus.sh` | RT 构建目标、独立 libclc 目录和最小 Spike driver 配置 |
| `spike/riscv/ventus_rtcore_model.h` | warp 发射边界、lane 执行和 RTcore 私有状态 |
| `spike/riscv/ventus_custom.cc` | `traverse/release` 与 SIMT active mask 接入 |
| `mesa/src/ventus/compiler/vt_rt_abi.h` | 当前 ABI v12 的实现侧 bit layout |
| `mesa/src/ventus/vulkan/vtvk_driver_bridge.cpp` | CPS carrier 骨架、PDS 初始化和资源上传 |
| `mesa/src/ventus/vulkan/vtvk_memory.c` | Vulkan buffer device address 映射 |
| `workloads/rt/` | 固定上游、overlay、来源和准备脚本 |
| `tools/rtcore.py` | 全应用图片回归与 ABI 入口；实现在 `tools/rtcore/` |
