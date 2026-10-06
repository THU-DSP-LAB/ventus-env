# 纹理功能模型

这是一个独立的 C++17 数值模型，用于先验证纹理采样规则，未来可作为 Spike/TMU 执行模型的基础。当前重点是功能正确；C++ 数据结构和函数是本地实现细节，不是冻结的指令、描述符 ABI 或硬件接口。

## 运行

在 `ventus-env` 目录下执行：

```sh
python3 tools/texture_model.py test
python3 tools/texture_model.py test --sanitize
python3 tools/texture_model.py demo
```

只需要 Python 3 和支持 C++17 的 host 编译器。入口在系统临时目录编译并执行，不安装任何库，不要求 Mesa、Spike、RTL 或 Ventus 交叉编译器已构建。`--cxx clang++` 可选择编译器。Sanitizer 模式使用 host AddressSanitizer/UndefinedBehaviorSanitizer，在当前 Linux 环境还禁用 PIE，以避免地址布局干扰。

## 已实现范围

| 功能 | 行为 |
| --- | --- |
| Image | 2D、单 layer、单 sample；完整或部分传统 mip 链 |
| 格式 | RGBA8_UNORM、BGRA8_UNORM；RGBA 浮点结果 |
| 存储 | 各 mip 独立 offset、width、height、row pitch；允许行填充和非零偏移 |
| 寻址 | U/V 独立的 repeat、mirrored repeat、clamp-to-edge |
| Texel 过滤 | nearest 或 bilinear；使用一个 filter，对应 min/mag 相同的 profile |
| LOD | 显式有限值；sampler 范围限制后映射至 view 的可访问 mip |
| Mip 过滤 | mip nearest，精确半整数选择较低一级 |
| Trace | mip、输入 UV/LOD、受限后的相对 LOD、邻点坐标、偏移、权重和颜色 |
| 多资源 | 不依赖全局采样状态，可交替采样不同 Image 和 Sampling 对象 |

`sample_level` 在指定的 image 绝对 mip 内采样。`sample_lod` 的 LOD 则相对 View；例如 `{base_mip=1, level_count=2}` 的 LOD 0 访问 image mip 1。每个 mip 的尺寸按 `max(1, previous/2)` 递减；可不提供后续级别，但不能在 1×1 之后继续追加重复 mip。

当前没有 LOD bias、mip linear、导数、sRGB、压缩、比较过滤、layer array 或 descriptor binding 解析。多 Image 的纯函数测试不等于 Vulkan descriptor array 或 non-uniform indexing 已实现。

## 计算规则与数值边界

- Nearest 使用 `floor(u*W)`、`floor(v*H)`，再处理整数 texel 寻址。精确等距边界采用该确定性选择。
- Bilinear 使用 `u*W-0.5`、`v*H-0.5` 的四个邻点，每个邻点独立寻址，再按权重混合。
- Repeat 的整数周期为 W；mirrored repeat 为 2W，后半周期折返；clamp 将邻点限制在 `[0,W-1]`。
- 对周期性 UV 先做有限范围归约，clamp 坐标先截到 `[0,1]`，避免将巨大有限值直接转换成整数。Trace 的 raw 坐标是这次归约后、整数寻址前的坐标，原始 UV 单独保留。
- Mip nearest 采用 Vulkan 文本中的 preferred 选择：精确半整数向较低级；alternative 的半整数向较高级在规范中也有允许，但不是本模型的选择。使用分数比较实现，避免 `LOD+0.5` 的浮点舍入吞掉半整数附近的差别。
- 不模拟实际 TMU 的固定点权重或硬件时序。UNORM 解码和输出为 float；坐标与权重计算、插值累加为 double。这是明确的数学参考实现，不承诺与未来 TMU bit-identical。

NaN/Infinity UV、LOD、LOD 范围，负的最小 LOD、反向范围、未知枚举、无效 mip/view 和不足的 backing bytes，均抛出 `std::invalid_argument`。这是本地模型的输入诊断，不规定 Vulkan API 的返回码。巨大有限 UV 按已有 double 值处理；它们在输入类型中已经丢失的小数信息无法恢复。

所有 mip 的存储边界在读取前检查。大小检查使用剩余容量与除法，避免 `rowPitch*(height-1)` 的无符号溢出。最后一行的尾部 padding 不要求实际存在，因为采样不读取该区域。offset、row pitch 和通道都按字节解释，不依赖 host 端序或地址对齐。

## 验证

测试包含六组：

1. 手算四点平均、非对称插值、repeat 接缝和 mirror/clamp 边缘。
2. 负坐标、texel 边界两侧、精确边界、负零和巨大有限坐标。
3. 两种格式中全部 256 个 UNORM 值及 alpha。
4. 3,600 个独立对照样例：五种尺寸、两种格式、U/V 九种寻址组合、两种过滤、各 20 个坐标；包含 1×1、1×N、N×1、非二次幂和 padding。同时验证周期、镜像对称与 trace 权重。
5. 每级不同颜色、view base mip、LOD clamp、半整数及其相邻浮点值、非零 mip 上的 bilinear，以及多个 Image 交替访问。
6. 非法输入、截断数据、存储运算溢出风险及不合法 mip 链。

独立对照以 long double 在虚拟延伸图像上枚举 nearest 区间或 tent kernel，逐个真实 texel 累积贡献；不调用模型的坐标归约、四点选择或寻址函数。精确样例和 UNORM 解码做相应精确比较；过滤测试的每分量绝对误差上限为 `2e-7`，适用于该模型的 float 输出，不能直接当成未来 TMU 精度要求。

`demo` 使用一张红/蓝双色图，在 `u=0` 处 bilinear 采样：repeat 返回紫色，mirrored repeat 和 clamp-to-edge 返回红色，并打印 tap 的坐标、偏移和权重。

这些 host 测试证明独立模型的已覆盖规则。下面的 GPU 对照补充了现有软件采样在 Spike 上的执行证据；缓存、完整同步、RTL 或性能不在这两类测试的证明范围内。

## Vulkan→Spike 数值对照

```sh
python3 tools/texture_model.py gpu
python3 tools/texture_model.py gpu --case-limit 1
python3 tools/texture_model.py gpu-mips
python3 tools/texture_model.py gpu-multi
python3 tools/texture_model.py gpu-checks
python3 tools/texture_model.py gpu-mip-linear-check
python3 tools/texture_model.py gpu --case-limit 1 --inject-mismatch
```

`gpu --case-limit 1 --inject-mismatch` 是比较器负向校验：故意修改读回的一个颜色分量，**必须返回 1**，不应作为普通成功测试执行。`--case-limit` 可限定前 1..180 种 LOD0 配置、1..360 种 mip 配置或 1..432 种多 binding 配置；`--timeout` 限制运行时间；`--out-dir` 指向空目录，以避免旧结果混入证据。默认输出位于 `artifacts/texture_model/gpu-<UTC>-<id>/`。

此动作需要已构建的本地 Mesa `mesa/build-ventus`、driver `driver/build/driver/spike_device`、Spike `spike/build`、LLVM `llvm/build`、`llvm/build-rt-libclc/lib/crt0.o`，以及 PATH 上的 `glslangValidator`、host C++ 编译器、Vulkan headers/loader 和 `pkg-config vulkan`。`--mesa-build` 可选择另一份已有 Mesa build。它不构建或安装上述组件。

运行器清除继承的 `VENTUS_VK_*` 实验选项，指定本地 ICD、driver 和工具，编译 shader 并通过默认 V5 ELF completion frontier 检查，再实际提交到 Spike。shader 在空 TLAS 上执行一次 miss，payload=73 用作完成标记；随后执行 `textureLod(..., 0)`。CPU 模型只生成期望值，实际值来自 shader。运行后核对进程实际加载的三份库路径，并记录 SHA256。

| 对照维度 | 已覆盖范围 |
| --- | --- |
| 尺寸 | 1×1、1×5、5×1、3×5、4×4 |
| 格式 | RGBA8_UNORM、BGRA8_UNORM；四通道均比较 |
| 寻址与过滤 | U/V 各三种寻址，共九种组合；nearest、bilinear |
| 坐标 | 每配置 64 组 float32 dyadic UV，含负数、0/1、周期边界、负零、约 ±65536 的坐标 |
| 上传 | 非零 bufferOffset=16、源行 padding；目标 image 由驱动保存 |
| 采样状态 | 单 sampled binding、LOD0、单 mip、min/mag 相同、零 bias |

dyadic UV 能在本组尺寸下精确表示相关坐标计算，减少 nearest 边界附近由参考 double 与 shader float 舍入差异引入的歧义。这不是任意 UV 精度或巨大 float32 坐标范围的穷尽验证。每分量绝对误差上限预先设为 `1e-6`，没有根据失败结果放宽。

2026-10-05 的初始 SSBO 和 buffer-reference 写回均失败，定位为整数地址形式的 `store_deref` 被忽略。2026-10-06 已修复该分支，保留写入掩码、volatile 和 32/64 位宽；其他未支持位宽明确拒绝。原始失败证据仍保留在 `artifacts/texture_model/first-spike-probe/` 和 `bda-spike-probe/`。

正式比较现在直接从 host-visible coherent buffers 读回 SSBO 与 buffer-reference 两份结果，各自与模型比较，并检查两份结果位模式一致。每次 dispatch 先初始化为 NaN，检查每请求的完成标记，已移除旧 RGB image 字节传输及对内部 image backing 的读回依赖。

2026-10-06 写回修复的运行证据：

- `artifacts/texture_model/buffer-fixed-full/manifest.json`：180 配置、两条路径共 23,040 samples、92,160 分量，零 mismatch，零 marker/buffer failure，最大绝对误差 `5.960464478e-8`。
- `artifacts/texture_model/buffer-fixed-any-hit/textured-any-hit-validation.txt`：原 160×96 场景保持 SHA256 `88b724540dc17b23161c638ebcbb71e9acaf73ff098b43764f9edf17cd5bebb0`。
- Mesa `vt_nir_to_llvm_phi_test` 验证外部 masked vec4、volatile、64-bit store 和未支持位宽拒绝。
- 2026-10-05 的 `lod0-spike-verified/`、`lod0-spike-negative/` 是旧图像传输方案的历史证据，不能当作当前 buffer 版本的运行记录。

每次数值运行包含 `run.log`、逐路径逐分量 `numeric.csv`、保留的 SPIR-V/LLVM/ELF 和构建日志；manifest 记录实际加载库、源码与二进制哈希、运行命令、profile 和结果。拒绝检查不产生数值 CSV。

### 软件 mip/view/显式 LOD（2026-10-06）

`gpu-mips` 使用独立 shader `sample_mip.rgen`，从 SSBO 读取每个请求的 float32 UV 和 LOD，实际执行动态 `textureLod`。与模型 `sample_lod` 比较两条 buffer 写回路径，容差仍为 `1e-6`。

- 360 配置覆盖两种格式、九种寻址组合和两种过滤；尺寸含 5×3、8×4、1×8、1×1、3×5 和 16384×1。
- 完整/不完整 mip 链；最高 15 级；非零 view base、有限 count 和 `VK_REMAINING_MIP_LEVELS`。
- 32 种 LOD 含负值、整数、精确半整数及相邻 binary32 值、很大有限值；sampler 范围含 0..1000、0.75..1.75、2..2、0..0.5。每级内容不同。
- 非零上传偏移、行 padding、逐 mip region；记录 copy 后更新 coherent staging；同一命令缓冲内局部更新 mip 后采样。
- 单级 view 使用 mipmap-linear 验证它不会改变结果；多级动态 LOD 使用 mip-nearest。

当前 Mesa 的 image 私有存储为每级紧密行、顺序拼接，全部 mip 计入内存需求；view 保存相对可见范围。每次 dispatch 上传一个 view-relative mip 表，元素记录设备地址、宽高与 row pitch；它由已有 dispatch transient allocation 生命周期管理。软件 descriptor 现为 80 字节，mip 表元素为 24 字节，producer/consumer 有 layout static assertions。这是 Mesa 软件实现编码，**不是 TMU/Spike 纹理指令 ABI**；没有新增 ISA。

`gpu-checks` 覆盖 13 项 sampler、8 项 image、10 项 view 和 7 类 shader 拒绝检查。Sampler API 不支持的状态返回 `VK_ERROR_FEATURE_NOT_PRESENT`；当前不支持的 shader 编译经 pipeline 返回初始化失败；多级 mipmap-linear 与动态 LOD 的不支持组合在提交时拒绝，当前实验驱动返回 `VK_ERROR_DEVICE_LOST`，由 `gpu-mip-linear-check` 单独验证。合法而无效的 compareOp/maxAnisotropy 默认字段不会被一概拒绝。

兼容例外：单级 view 的 mipmap-linear 没有跨级效果；另外，已证明所有采样均为 LOD0 的普通 RT pipeline、sampler minLod=0 时，多级 mipmap-linear 仍只读取 view 的第 0 级。原 any-hit workload 属于后者。此例外保守地不开放给独立 global callback/resume ELF profile。

最终运行证据：

| 门禁 | 结果 | 本地证据 |
| --- | --- | --- |
| LOD0 | 180 配置、92,160 分量，零差异 | `artifacts/texture_model/lod0-final-v2/manifest.json` |
| Mip/view/LOD | 360 配置、184,320 分量，零差异 | `artifacts/texture_model/mip-final-v2/manifest.json` |
| 范围检查 | 13 sampler + 8 image + 10 view + 7 shader | `artifacts/texture_model/profile-final-v2/manifest.json` |
| 多级 mip-linear 拒绝 | 预期提交失败，检查器通过 | `artifacts/texture_model/mip-linear-rejected/manifest.json` |
| 比较器负向校验 | 故意改一个分量，mismatch=1、返回 1 | `artifacts/texture_model/mip-checker-negative/manifest.json` |
| 原 any-hit 场景 | 160×96，原 SHA256 不变 | `artifacts/texture_model/any-hit-final-v2/textured-any-hit-validation.txt` |

两组数值门禁的最大绝对误差均为 `5.960464478e-8`。Mesa `vt_nir_to_llvm_phi_test` 与 host 模型 47,885 项检查（普通/ASan/UBSan）通过。软件执行证据对应 Mesa 本地提交 `7f5b850`。

仍要求 min/mag 相同、零 bias、有限 UV/LOD、二维单 layer/sample、RGBA8/BGRA8 UNORM 和 identity swizzle。不支持跨 mip 线性混合、导数选级、独立 sampler、数组/非一致 descriptor 索引、comparison、anisotropy、offset、projector、gather、texelFetch、sRGB 或压缩。host 模型的 NaN/Inf 异常诊断不是 GPU 运行时异常能力。不同 Image 在同一 binding 顺序替换已验证，多 binding 同时采样尚未作为本次门禁覆盖。

上传和执行顺序的证据限定在当前 coherent backing、串行提交的 Spike 实验路径；不证明一般 Vulkan barrier/layout、跨队列或缓存一致性。模型尚未成为 Spike 的纹理指令处理程序，也未接入 RTL。

### 多固定 binding 回归与当前阻塞（2026-10-06）

`gpu-multi` 使用 `sample_multi.rgen`，在同一个 shader 中无条件采样 set 0 的 combined bindings **2 和 13**。每个请求分别提供两组显式 LOD，保存 A、B、`0.25*A + 0.75*B` 和完成标记；SSBO/BDA 两条路径各自对照模型。CSV 保留请求编号、资源/view、sampler 状态、选中 mip 和逐分量误差。

432 配置由三组资源 × 九种 U/V 组合 × 两种过滤 × 八个执行阶段组成。资源包含不同格式/尺寸/mip 链，以及同一 image 的两个不同 view；两个 sampler 使用不同寻址、过滤和 LOD clamp。八阶段为基线、互换 image/view、同时互换 image/view 和 sampler、只互换 sampler、共享 sampler、更新 A、保持 descriptor 不变重新 dispatch、恢复 A。同一 image 的两个 view 会共同观察该 image 的更新。

**当前默认编译配置未通过这项回归，不能标记 T0 已验收。**

| 运行 | 结果 | 证据 |
| --- | --- | --- |
| 默认 LLVM 配置 | 432 配置，663,552 分量；257,864 mismatch，marker/buffer failure 均为 0 | `artifacts/texture_model/multi-default-final/manifest.json` |
| 诊断：仅加 `llc -optimize-regalloc=false` | 同一 432 配置零差异，最大误差 `1.192092896e-7` | `artifacts/texture_model/multi-fast-ra-full/manifest.json` |
| 诊断负向校验 | 只破坏 binding 13 的一个 BDA 分量；mismatch=1，返回 1 | `artifacts/texture_model/multi-fast-ra-negative/manifest.json` |
| 原单 binding 回归 | LOD0 180 配置、mip 360 配置、38 项拒绝检查全部通过 | `multi-harness-lod0-regression/`、`multi-harness-mip-regression/`、`multi-harness-profile-regression/` 下的 manifest |

诊断 manifest 标记为 `DIAGNOSTIC_PASS` / `DIAGNOSTIC_EXPECTED_FAILURE`，记录独立 llc wrapper、命令、环境与哈希；它们不代替默认路径验收。生产 Mesa、LLVM、driver、Spike 源码和默认优化设置均未修改。单采样对照正确，改变双采样顺序会改变错误范围；`-O0` 也可消除首个用例的错误，`-O1` 和 legacy SGPR keepalive 仍失败。证据将问题缩小到优化寄存器分配相关的代码生成路径，具体错误指令、活跃区间或 pass 尚未确定。

下一代码任务应修复默认编译路径并让 `gpu-multi` 原命令通过，同时保持现有回归。范围不扩展到 descriptor array、独立 image/sampler、动态索引、其他 descriptor set 或 TMU/RTL。

## 代码组织与规范来源

- `model.hpp` / `model.cpp`：功能模型，只使用 C++ 标准库。
- `test.cpp`：独立 oracle、手算例子和回归。
- `demo.cpp`：采样过程示例。
- `gpu.cpp` / `gpu.py` / `sample.rgen` / `sample_mip.rgen` / `sample_multi.rgen` / `sample.rmiss`：真实 Vulkan→Spike 数值比较与运行证据。
- 一级入口 `../texture_model.py`：host 构建与运行。

模型语义依据 [Vulkan Sampling](https://docs.vulkan.org/spec/latest/chapters/textures.html)，特别是 Image Level(s) Selection、Texel Filtering 和 Texel Addressing。2026-10-05 核查了 [官方规范源文件](https://github.com/KhronosGroup/Vulkan-Docs/blob/main/chapters/textures.adoc) 的 preferred/alternative mip nearest 规则。没有将该文档的全部功能纳入本模型。

持续学习与跨层实现计划见外层仓库的 [纹理指南](../../../docs/architecture/VentusGPU_TEXTURE_IMPLEMENTATION_GUIDE.md)。
