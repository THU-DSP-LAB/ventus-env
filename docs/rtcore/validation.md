# Ventus RT 构建与图像回归

## 输入和入口

标准 Vulkan RT pipeline 使用 megakernel：raygen、closest-hit、miss 和
candidate callback 由 Mesa lowering 组合，LLVM 生成 Ventus ELF。
RTcore 执行固定遍历动作，shader 在 parent 的 RT Local/PDS 中读写状态。
candidate spill/resume 与硬件稀疏任务合批见
[设计说明](candidate-spill-and-task-batching.md)。

源码输入只有固定 SaschaWillems 上游和 testcases 中维护的五例覆盖文件；
组合后的源码、SPIR-V 和应用构建放在 build/rt-workload，日志、图像、
生成 ELF 和测量证据放在 artifacts。详见
[workload 准备说明](../../workloads/rt/README.md)。

## 构建

```sh
git submodule update --init
bash build-ventus.sh --build "rt-toolchain;spike;driver-spike;mesa;rt-workload"
python3 tools/rtcore.py abi --check
```

Mesa 全应用路径只使用 canonical `mesa/build-ventus`。
Mesa 需要 Meson 1.4 或更新版本，必要时先建立 build/meson-venv，
build-ventus.sh 会自动检测。驱动和工具链路径由仓库解析，不依赖另一份 checkout。

`rt-workload` 准备并编译五例应用，重新生成 GLSL SPIR-V。
`rt-toolchain` 构建 LLVM 工具与 RT 链接需要的 crt0；
`driver-spike` 只构建 Spike 驱动；`mesa` 构建 ICD 和编译器。
RTL 使用隔离运行库：

```sh
VENTUS_PMU_MODE=detailed bash build-ventus.sh --build rtcore-rtl
```

不要把尚未确认的工具链或 RTL 安装覆盖到稳定 install/。
个人准备好的 workload 可显式传入 --workload-dir 和 --build-dir，
默认值是 build/rt-workload/source 和 build/rt-workload/build。

## 图像门禁

先运行小图，再增加尺寸和 warp 数：

```sh
python3 tools/rtcore.py regression --hardware-runner spike --width 1 --height 1
python3 tools/rtcore.py regression --hardware-runner all --pmu-mode detailed \
  --include-candidates --case raytracingshadows --case raytracingsbtdata \
  --case raytracingintersection --case raytracingreflections \
  --case raytracingtextures --width 4 --height 4
```

--include-candidates 只允许选择候选例子，不自动选中它们。
基本例子没有 software 截图钩子，不纳入通过集合。
textures 对软件允许 RGB8 单通道误差 1；其他例子按 manifest 要求比较。
32×16 intersection 的历史测量存在已知软件/硬件蓝色 1 LSB 差异，
不能把 RTL/Spike 一致说成软件严格通过。

每次调用保存到 artifacts/rtcore-regression/runs/<run-id>：
renderer 正常退出、图片存在且满足像素阈值才通过，崩溃前截图不能通过。
provenance 保留源码状态与运行库哈希，--hardware-runner all 还直接比较
Spike/Verilator。basic/detailed 构建应有相同周期和公共计数，
不可把仪表行为差异当作硬件收益。

## 定向检查与诊断

- Scala 单测覆盖 stack、SRAM、candidate 生命周期、spill ACK 与 parent 代理映射。
- Mesa：meson test -C mesa/build-ventus --suite ventus --print-errorlogs。
- Spike：tests/ventus_rt_semantics.cc 与 tests/ventus_extra_stage2_static.py。
- Descriptor/多 geometry：python3 workloads/rt/tests/resource_layout/run.py。
- PMU 报告：tools/rtcore.py report；有明确假设后才启用 record trace，
  离线分析使用 tools/rtcore.py coherence。
- RT 工具单测：python3 -m unittest discover -s tools/rtcore/tests -t .。

常规运行关闭 Spike commit log 和 RTL detailed trace，避免巨大日志。
失败时先检查源码语义、已有图像差异和最小单测；新 trace 应区分具体假设，
输出必须有界。不要反复构建仅为了增加无目标日志。

这些是有界功能和性能验证，不是 Vulkan RT conformance，也不证明未覆盖的
callable、indirect、serialization 或任意尺寸/场景正确性。
