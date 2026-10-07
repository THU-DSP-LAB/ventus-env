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
| 格式 | RGBA8/BGRA8 UNORM 或 sRGB；RGBA 浮点结果，sRGB RGB 在过滤前解码，alpha 保持 UNORM |
| 存储 | 各 mip 独立 offset、width、height、row pitch；允许行填充和非零偏移 |
| 寻址 | U/V 独立的 repeat、mirrored repeat、clamp-to-edge |
| Texel 过滤 | nearest 或 bilinear；独立 minFilter/magFilter，按 sampler clamp 后的 LOD 选择 |
| LOD | 显式有限值；sampler 范围限制后映射至 view 的可访问 mip |
| Mip 过滤 | nearest（精确半整数选择较低一级）或 linear（相邻两级混合）；与级内过滤独立 |
| View swizzle | IDENTITY、R/G/B/A、ZERO/ONE；解码后通道置换/重复或浮点常量；适用于 sample/fetch |
| 整数读取/查询 | 合法整数坐标 `texel_fetch`；相对 View 的 `texture_size`、`texture_query_levels`；不使用 sampler |
| Trace | 下/上 mip、跨级权重、输入 UV/LOD、受限后的相对 LOD；每个 tap 的绝对 mip、坐标、偏移、合并权重和颜色 |
| 多资源 | 不依赖全局采样状态，可交替采样不同 Image 和 Sampling 对象 |

`sample_level` 在指定的 image 绝对 mip 内用 `mag_filter` 采样，它是直接指定级别与级内过滤的 helper，不进行 LOD/min/mag 选择。`sample_lod` 的 LOD 则相对 View；例如 `{base_mip=1, level_count=2}` 的 LOD 0 访问 image mip 1。每个 mip 的尺寸按 `max(1, previous/2)` 递减；可不提供后续级别，但不能在 1×1 之后继续追加重复 mip。

当前没有 LOD bias、导数、压缩、比较过滤、layer array 或 descriptor binding 解析。多 Image 的纯函数测试不等于 Vulkan descriptor array 或 non-uniform indexing 已实现。

## 计算规则与数值边界

- Nearest 使用 `floor(u*W)`、`floor(v*H)`，再处理整数 texel 寻址。精确等距边界采用该确定性选择。
- Bilinear 使用 `u*W-0.5`、`v*H-0.5` 的四个邻点，每个邻点独立寻址，再按权重混合。
- Repeat 的整数周期为 W；mirrored repeat 为 2W，后半周期折返；clamp 将邻点限制在 `[0,W-1]`。
- 对周期性 UV 先做有限范围归约，clamp 坐标先截到 `[0,1]`，避免将巨大有限值直接转换成整数。Trace 的 raw 坐标是这次归约后、整数寻址前的坐标，原始 UV 单独保留。
- Mip nearest 采用 Vulkan 文本中的 preferred 选择：精确半整数向较低级；alternative 的半整数向较高级在规范中也有允许，但不是本模型的选择。使用分数比较实现，避免 `LOD+0.5` 的浮点舍入吞掉半整数附近的差别。
- Mip linear 先将 LOD 限制到 sampler、view 范围，令 `l0=floor(LOD)`、`l1=min(l0+1,viewCount-1)`、`t=LOD-l0`。在两级分别按其尺寸进行 nearest/bilinear 采样，再返回 `(1-t)*C0+t*C1`；级内 bilinear 加跨级 linear 即 trilinear。整数 LOD、末级和单级 view 不引入其他级的颜色。`sample_level` 直接指定一个 mip，不使用 mip filter。
- sampler clamp 后的 LOD（lambda）≤ 0 使用 `mag_filter`，> 0 使用 `min_filter`；这一步先于 view 范围限制与 mip 取整。两级采样使用同一个选中的级内 filter。正 lambda 最终选中相对 mip 0 或单级 view 时仍使用 minFilter。Trace 将 `sampler_lod` 与 view 限制后的 `clamped_lod` 分开保存。
- sRGB 的 RGB 字节先归一化为 `e=byte/255`，当 `e≤0.04045` 时解码为 `e/12.92`，否则为 `((e+0.055)/1.055)^2.4`。每个 tap 先解码再参与级内和跨级过滤；alpha 直接按 UNORM 解码。Trace 的 tap 颜色也处于线性空间，存储字节不改写。
- 不模拟实际 TMU 的固定点权重或硬件时序。UNORM 解码和输出为 float；坐标与权重计算、插值累加为 double。这是明确的数学参考实现，不承诺与未来 TMU bit-identical。

NaN/Infinity UV、LOD、LOD 范围，负的最小 LOD、反向范围、未知枚举、无效 mip/view 和不足的 backing bytes，均抛出 `std::invalid_argument`。这是本地模型的输入诊断，不规定 Vulkan API 的返回码。巨大有限 UV 按已有 double 值处理；它们在输入类型中已经丢失的小数信息无法恢复。

所有 mip 的存储边界在读取前检查。大小检查使用剩余容量与除法，避免 `rowPitch*(height-1)` 的无符号溢出。最后一行的尾部 padding 不要求实际存在，因为采样不读取该区域。offset、row pitch 和通道都按字节解释，不依赖 host 端序或地址对齐。

## 验证

测试包含十一组，普通和 ASan/UBSan 模式目前均通过 778,593 项检查：

1. 手算四点平均、非对称插值、repeat 接缝和 mirror/clamp 边缘。
2. 负坐标、texel 边界两侧、精确边界、负零和巨大有限坐标。
3. 两种格式中全部 256 个 UNORM 值及 alpha。
4. 3,600 个独立对照样例：五种尺寸、两种格式、U/V 九种寻址组合、两种过滤、各 20 个坐标；包含 1×1、1×N、N×1、非二次幂和 padding。同时验证周期、镜像对称与 trace 权重。
5. 每级不同颜色、view base mip、LOD clamp、半整数及其相邻浮点值、非零 mip 上的 bilinear，以及多个 Image 交替访问。
6. 非法输入、截断数据、存储运算溢出风险及不合法 mip 链。
7. Mip linear 手算与独立跨级 oracle；两种格式、全部寻址/级内过滤组合、view/clamp、小数/整数相邻 LOD、非二次幂/padding，检查跨级 tap 权重和整数 LOD 的精确退化。
8. min/mag 手算及独立 LOD oracle；四种过滤组合、两种 mip filter、零点相邻值与负零、sampler clamp、view clamp 与 mip rounding 的区别。
9. 两种 sRGB 通道顺序、全部 256 个编码值、alpha 精确保持 UNORM、过滤前解码的手算样例，以及 sRGB/min-mag/mip/address/view 独立 oracle。
10. 整数 fetch 的独立颜色/字节地址 oracle、与 nearest 中心读取交叉核对，合法 view 子范围的尺寸/级数、非法整数坐标和 LOD 拒绝。
11. 四种格式的全部 2,401 种 view component mappings，独立 sample/fetch oracle、常量精确性、trace 与属性查询不变。

独立对照以 long double 在虚拟延伸图像上枚举 nearest 区间或 tent kernel，逐个真实 texel 累积贡献；不调用模型的坐标归约、四点选择或寻址函数。精确样例和 UNORM 解码做相应精确比较；过滤测试的每分量绝对误差上限为 `2e-7`，适用于该模型的 float 输出，不能直接当成未来 TMU 精度要求。

`demo` 使用一张红/蓝双色图，在 `u=0` 处 bilinear 采样：repeat 返回紫色，mirrored repeat 和 clamp-to-edge 返回红色，并打印 tap 的坐标、偏移和权重。

这些 host 测试证明独立模型的已覆盖规则。下面的 GPU 对照补充了现有软件采样在 Spike 上的执行证据；缓存、完整同步、RTL 或性能不在这两类测试的证明范围内。

## Vulkan→Spike 数值对照

```sh
python3 tools/texture_model.py gpu
python3 tools/texture_model.py gpu --case-limit 1
python3 tools/texture_model.py gpu-mips
python3 tools/texture_model.py gpu-multi
python3 tools/texture_model.py gpu-multi-linear
python3 tools/texture_model.py gpu-min-mag
python3 tools/texture_model.py gpu-multi-min-mag
python3 tools/texture_model.py gpu-fetch-query
python3 tools/texture_model.py gpu-swizzle
python3 tools/texture_model.py gpu-fetch-swizzle
python3 tools/texture_model.py gpu-multi-swizzle
python3 tools/texture_model.py gpu-srgb
python3 tools/texture_model.py gpu-srgb-values
python3 tools/texture_model.py gpu-multi-srgb
python3 tools/texture_model.py gpu-checks
python3 tools/texture_model.py gpu-mip-linear-check
python3 tools/texture_model.py gpu --case-limit 1 --inject-mismatch
```

`gpu --case-limit 1 --inject-mismatch` 是比较器负向校验：故意修改读回的一个颜色分量，**必须返回 1**，不应作为普通成功测试执行。`--case-limit` 可限定前 1..180 种 LOD0 配置、1..360 种 mip 配置、1..432 种多 binding 配置、1..1440 种独立 min/mag/sRGB 配置、1..144 种 sRGB 全编码值配置或 1..2880 种 fetch/query 配置；`--timeout` 限制运行时间；`--out-dir` 指向空目录，以避免旧结果混入证据。默认输出位于 `artifacts/texture_model/gpu-<UTC>-<id>/`。

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

`gpu-checks` 覆盖 13 项 sampler、8 项 image、10 项 view 和 7 类 shader 拒绝检查。Sampler API 不支持的状态返回 `VK_ERROR_FEATURE_NOT_PRESENT`；当前不支持的 shader 编译经 pipeline 返回初始化失败。合法而无效的 compareOp/maxAnisotropy 默认字段不会被一概拒绝。2026-10-07 F01 开放了当前 profile 的多级 mipmap-linear；`gpu-mip-linear-check` 已改为正向数值门禁，见后面的 F01 节。

历史兼容例外：F01 之前，单级 view 和已证明只用 LOD0/minLod=0 的普通 RT pipeline 可使用 mipmap-linear；其他多级组合会被拒绝。现在已实现通用的跨级混合，不再依靠这个 LOD0 例外。

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

当前仍要求零 bias、有限 UV/LOD、非负 sampler minLOD、二维单 layer/sample、RGBA8/BGRA8 UNORM 和 identity swizzle；F02 已解除 min/mag 相同的要求。不支持导数选级、独立 sampler、数组/非一致 descriptor 索引、comparison、anisotropy、offset、projector、gather、texelFetch、sRGB 或压缩。host 模型的 NaN/Inf 异常诊断不是 GPU 运行时异常能力。不同 Image 在同一 binding 顺序替换已验证；多 binding 的专项验证见下一节。

上传和执行顺序的证据限定在当前 coherent backing、串行提交的 Spike 实验路径；不证明一般 Vulkan barrier/layout、跨队列或缓存一致性。模型尚未成为 Spike 的纹理指令处理程序，也未接入 RTL。

### 多固定 binding 回归与执行路径修复（2026-10-06）

`gpu-multi` 使用 `sample_multi.rgen`，在同一个 shader 中无条件采样 set 0 的 combined bindings **2 和 13**。每个请求分别提供两组显式 LOD，保存 A、B、`0.25*A + 0.75*B` 和完成标记；SSBO/BDA 两条路径各自对照模型。CSV 保留请求编号、资源/view、sampler 状态、选中 mip 和逐分量误差。

432 配置由三组资源 × 九种 U/V 组合 × 两种过滤 × 八个执行阶段组成。资源包含不同格式/尺寸/mip 链，以及同一 image 的两个不同 view；两个 sampler 使用不同寻址、过滤和 LOD clamp。八阶段为基线、互换 image/view、同时互换 image/view 和 sampler、只互换 sampler、共享 sampler、更新 A、保持 descriptor 不变重新 dispatch、恢复 A。同一 image 的两个 view 会共同观察该 image 的更新。

首次运行的默认路径有数值错误。修复 Spike 的 VF 标量 `regext` 读取后，**默认 LLVM 配置通过本项 T0 回归**。

| 运行 | 结果 | 证据 |
| --- | --- | --- |
| 修复前的默认路径 | 432 配置，663,552 分量；257,864 mismatch，marker/buffer failure 均为 0 | `artifacts/texture_model/multi-default-final/manifest.json` |
| 诊断：仅加 `llc -optimize-regalloc=false` | 同一 432 配置零差异，最大误差 `1.192092896e-7` | `artifacts/texture_model/multi-fast-ra-full/manifest.json` |
| 诊断负向校验 | 只破坏 binding 13 的一个 BDA 分量；mismatch=1，返回 1 | `artifacts/texture_model/multi-fast-ra-negative/manifest.json` |
| 原单 binding 回归 | LOD0 180 配置、mip 360 配置、38 项拒绝检查全部通过 | `multi-harness-lod0-regression/`、`multi-harness-mip-regression/`、`multi-harness-profile-regression/` 下的 manifest |
| Spike 修复后的默认路径 | 432 配置，663,552 分量零差异；最大误差 `1.192092896e-7` | `artifacts/texture_model/multi-regext-default-final/manifest.json` |
| 默认路径负向校验 | 修改 binding 13 的一个 BDA 分量，恰好 1 mismatch、返回 1 | `artifacts/texture_model/multi-regext-negative/manifest.json` |

诊断 manifest 标记为 `DIAGNOSTIC_PASS` / `DIAGNOSTIC_EXPECTED_FAILURE`，记录独立 llc wrapper、命令、环境与哈希；它们保留为定位证据。单采样对照正确，改变双采样顺序会改变错误范围；`-O0` 和 basic 分配器可消除首个用例的错误。最终根因是 Spike `riscv/v_ext_macros.h` 的 `VFP_SCALAR` 未给标量 rs1 加上 `ext_rs1()`，把 x49 等高编号寄存器读成 x17。greedy 分配增加了使用高寄存器的机会，从而暴露执行器的解码缺陷。

Spike 本地修复提交 `4b34446a` 在选择 Zfinx/XPR 或标准 F/FPR 后端前应用扩展位。修复前后默认 ELF 的 SHA256 完全相同，Mesa、LLVM、driver 二进制也相同，只有 Spike 库发生变化；因此不需要关闭优化或修改纹理 lowering。

指令级测试直接调用生产指令处理函数，覆盖两种浮点寄存器域、全部八个 register bank、四种算术和四种比较、16/32 位输入的两类 widening、inactive lane 与无 prefix 的低寄存器读取。低寄存器、高寄存器和另一浮点域故意存放不同值；该测试在修复前复现 bank 1 的错误。

```sh
python3 spike/tests/ventus_zfinx_vf_static.py --spike-build spike/build --spike-dasm spike/build/spike-dasm
```

最终单 binding LOD0/mip、38 项范围检查和多级 mip-linear 拒绝检查均通过，分别见 `lod0-regext-final/`、`mip-regext-final/`、`profile-regext-final/`、`mip-linear-regext-final/` 的 manifest；原 any-hit 场景仍保持同一 SHA256，见 `any-hit-regext-final/textured-any-hit-validation.txt`。T0 证据限定为本测试的两个固定 combined bindings，范围不扩展到 descriptor array、独立 image/sampler、动态索引、其他 descriptor set 或 TMU/RTL。

### F01：跨 mip 线性混合（2026-10-07）

模型增加 `Sampling::mip_filter`，默认仍为 nearest。`sample_lod` 的结果保存下/上 mip 和跨级权重；tap 的权重包含级内与跨级两部分。独立 oracle 在所有 view 级别上枚举一维 tent 权重，再结合独立的级内过滤 oracle，避免照抄两级选择代码。

Mesa 本地提交 `cc0f2da` 将 sampler 的 `mipmapMode` 写入原 80 字节 descriptor 的第 76 字节处，替换原 reserved 字段；producer、consumer 和 static assertions 同步更新。`lowerTex` 在 NIR texture instruction 转 LLVM IR 时生成两级访存、级内过滤与跨级浮点混合。继续使用普通 LLVM load、算术和 select，不增加纹理指令，也不更改 image/mip 存储方式。

目前软件 lowering 会计算两次级内采样，再选择/混合结果；nearest 时两次使用同一 mip，末级访问也保持在 view 范围内。这是功能版本，尚未优化额外访存或衡量性能。数学模型不模拟规范所述的固定 mipmapPrecisionBits 量化，GPU 使用 binary32；本轮采用既定绝对误差标准，不据此宣称完整 Vulkan 或硬件精度一致性。

`gpu-mip-linear-check` **现在执行 360 配置的正向数值比较**，不再期待提交失败。它使用不同颜色的 mip、整数及其相邻 binary32 LOD、0.25/0.5/0.75 等权重、15 级链、view/clamp 和上传更新。`gpu-multi-linear` 使用原 432 配置及八阶段，并覆盖 linear/nearest 和 linear/linear 的 mip filter 组合；状态互换与共享 sampler 同样适用。

最终证据均在 `artifacts/texture_model/mip-linear-implementation/`；以下结果来自主目录 Mesa 构建和默认 LLVM 配置：

| 门禁 | 结果 | 子目录 |
| --- | --- | --- |
| mip-linear | 360 配置、184,320 分量，零差异 | `final-linear/` |
| 双 binding linear/mixed | 432 配置、663,552 分量，零差异 | `final-multi-linear/` |
| 原 LOD0 | 180 配置、92,160 分量，零差异 | `final-lod0/` |
| 原 mip-nearest | 360 配置、184,320 分量，零差异 | `final-mip-nearest/` |
| 原双 binding nearest | 432 配置、663,552 分量，零差异 | `final-multi-nearest/` |
| 原范围检查 | 13 sampler + 8 image + 10 view + 7 shader 全部通过 | `final-profile/` |
| 两项负向校验 | 各故意破坏一个分量，各报 1 mismatch、返回 1 | `final-linear-negative/`、`final-multi-linear-negative/` |
| 原 textured any-hit | 160×96，原 SHA256 保持不变 | `final-any-hit/` |

数值 manifest 保存实际加载库、工具与源码哈希、运行环境、ELF 和 CSV 哈希；新功能的最大绝对误差为 `1.192092896e-7`，容差 `1e-6`。主目录 `vt_nir_to_llvm_phi_test` 通过，构建和测试日志也保存在上述目录。负向校验 manifest 的 `FAIL` 表示按预期检测到了故意注入的错误，不能混算成正向通过。原拒绝门禁保留为历史证据。

F01 仍要求 equal min/mag、零 bias、有限 UV/LOD、2D 单 layer/sample、RGBA8/BGRA8 UNORM 和 identity swizzle。下一切片为独立 min/mag（F02），其放大/缩小判定需依据 sampler clamp 后的 LOD，而非选中的 mip 编号。

### F02：独立 min/mag 与负零选级修复（2026-10-07）

`Sampling` 现在保存 `mag_filter`、`min_filter`、`mip_filter`。三个参数的构造形式仍表示 equal min/mag；独立状态使用 `Sampling{uMode,vMode,magFilter,minFilter,mipFilter}`。这些是可调整的模型内部类型，不是设备 ABI。

Mesa 在 sampler 创建与绑定处分别检查 min/mag 都为 nearest 或 linear；不再要求相等。descriptor 原本已保存两种 filter，布局保持不变。`lowerTex` 在 sampler clamp 后判断 `lambda>0`，选择 min 或 mag，再做 view clamp 和 mip 选择。没有新增指令，也没有修改 Spike、LLVM 优化或容差。

新矩阵还发现并修复原 mip-nearest 的负零 LOD 缺陷。`-0.0` 在浮点比较中等于零，但位模式 `0x80000000` 的无符号整数比较会越过所有半级阈值，误选相对 mip 14。15 级链返回错误颜色，较短链可能读取 mip 表外并使 buffer/完成标记未写回。现在只对已经限制为非负的选级比较值清除符号位，因此 `-0.0` 正确选中相对 mip 0，并保留精确半整数的较低级规则。

`gpu-min-mag` 覆盖 1,440 配置：原十组 image/view/clamp × 两种格式 × 九种寻址 × 四种 min/mag × 两种 mip filter。32 种 LOD 含负零、零两侧的最小 binary32 值、正 LOD 仍取 mip 0、整数/大有限值；source upload 和 SSBO/BDA 读回沿用现有门禁。`gpu-multi-min-mag` 用原 432 配置和八阶段交换不同 min/mag、mip filter 及 sampler clamp。

规范和数值标准沿用 F01；额外依据 LOD Operation 与 Texel Filtering 对 lambda 的定义。修复前失败证据保留在 `artifacts/texture_model/min-mag-implementation/isolated-min-mag-smoke/` 与 `isolated-min-mag/`，修复后的最终验收见该目录的 `verification.json`。后续 F03 扩展的 sRGB 见下节。

主目录默认 LLVM 的 `final-min-mag/` 检查 737,280 分量、`final-multi-min-mag/` 检查 663,552 分量，均零差异，最大误差 `1.192092896e-7`。F01 两组、原 LOD0/mip-nearest/双 binding nearest 和 38 项范围检查全部通过；两个负向门禁各报一个注入错误。模型普通/sanitizer 各 170,285 检查、Mesa translation unit test 通过，`final-any-hit/` 保持原 160×96 图像 SHA256。

## F03：RGBA8/BGRA8 sRGB

2026-10-07 增加 `RGBA8_SRGB`、`BGRA8_SRGB`。RGB 每个 texel 先按 sRGB EOTF 解码至线性空间，再进行 nearest/bilinear 与跨 mip 过滤；alpha 始终是 UNORM。以黑、白两个 texel 的平均为例，正确线性结果是 0.5，而不是先平均编码值再解码所得的约 0.214。

模型用 double 分段公式和 `std::pow`，独立 oracle 用 long double；GPU 从实际 image 字节取出通道，再读取 Mesa 的 256 项 float 解码表。该表由驱动随 dispatch 上传在 view-relative mip 表之后：`decoder = mip_table_addr + view_level_count * 24`，共 1,024 字节。80 字节 descriptor 与 24 字节 mip entry 不变，image 原始字节、mip offset/row pitch 不变。UNORM view 也有有效表尾，以保证当前无条件生成的查表 load 地址有效；UNORM 结果仍使用原解码算术。CPU 不替 GPU 计算采样结果；没有新增 ISA、`pow` 运行库或安装组件。额外元数据与查表成本尚未做性能优化。

Vulkan sampled/transfer image 创建、同格式 identity view、mip 内存范围和格式能力查询已支持这两种 sRGB。只声明 sampled、linear-filter 与 transfer 能力；sRGB storage、color attachment、格式重解释等仍拒绝。也补上原 UNORM format query 遗漏的 sampled/linear-filter 标志。未据此声称完整 Vulkan 格式一致性。

三个入口覆盖不同目的：`gpu-srgb` 有 1,440 配置，复用 F02 的所有 min/mag/mip/address/view/clamp 和上传更新组合；`gpu-srgb-values` 有 144 配置，每次读取 256×1 纹理的全部编码值中心；`gpu-multi-srgb` 有 432 配置，覆盖 sRGB+UNORM、RGBA/BGRA sRGB、同一 sRGB image 的不同 view，以及八阶段交换/共享/更新。每组还执行 18 项格式属性、image-format 查询和 sRGB storage 拒绝检查，实际结果从 SSBO/BDA 分别读回。

规范使用 Vulkan-Docs v1.4.328 的 [Formats](https://github.com/KhronosGroup/Vulkan-Docs/blob/v1.4.328/chapters/formats.adoc) 和 [Texel Decode](https://github.com/KhronosGroup/Vulkan-Docs/blob/v1.4.328/chapters/images.adoc)，传递函数依据 Khronos DataFormat 的 `TRANSFER_SRGB_EOTF`。后者取源码快照 SHA256 `f0d612c91c45927980f94d76ad138235a6da85c098fbeeecd7a08624ad4009ce`，避免把 main 当作固定版本；三个快照、哈希及运行证据保存在 `artifacts/texture_model/srgb-implementation/`。Host/GPU 容差保持 `2e-7`/`1e-6`，不代表未来 TMU 精度。

最终主目录默认 LLVM 验收：host 普通/sanitizer 各 **9 组、228,157 检查**。`gpu-srgb` **1,440 配置、737,280 分量**，`gpu-srgb-values` **144 配置、294,912 分量**，`gpu-multi-srgb` **432 配置、663,552 分量**，全部零差异，最大绝对误差 `1.192092896e-7`。每组 18 项格式检查均通过；F01/F02 和原 LOD0/mip-nearest/双 binding nearest、38 项范围检查、Mesa translation unit test 均通过。两个比较器负向校验各检测一个注入错误，原 any-hit 160×96 图像仍为 SHA256 `88b724540dc17b23161c638ebcbb71e9acaf73ff098b43764f9edf17cd5bebb0`。汇总为 `artifacts/texture_model/srgb-implementation/verification.json`，下一切片是 F04 整数读取与查询。

## F04：整数 texelFetch 与尺寸/级数查询

2026-10-07 新增模型 `texel_fetch(image, view, x, y, lod)`、`texture_size(image, view, lod)`、`texture_query_levels(image, view)`，以及 GPU 的 NIR `txf`、`txs`、`query_levels` lowering。Fetch 使用合法 signed int32 texel 坐标和相对 view 的 integer LOD，直接读取一个 texel，按 UNORM/sRGB 解码；不归一化坐标，不使用 sampler 寻址、过滤或 LOD clamp。尺寸查询返回 view 对应 mip 的 width/height，级数查询返回 view.level_count。GPU 查询只读取描述符/mip 元数据，不访问 texel 数据。

本轮仍使用固定 combined binding，API 对象需要合法 sampler，但这些操作不读取其采样字段；独立 image/sampler 留在 F07。支持当前四种格式、2D 单 layer/sample、同格式 identity view，不包含 fetch offset、多采样、array、sparse 或新的 descriptor 索引。

越界边界单列：模型对非法 view、负/过大 LOD、负/越界 texel 坐标抛出 `std::invalid_argument`；Vulkan 未启用相关 robustness 时这些输入的结果可能未定义。当前 GPU 将 index/coord 先替换为安全值再做 load，并选择零输出，仅用于内部防越界，不声明 robustness feature 或把零规定为可依赖的应用语义。合法输入完全不进行 clamp/wrap。

`gpu-fetch-query` 的 2,880 配置覆盖十组 image/view/clamp × 四种格式 × 九种寻址 × 八种 min/mag/mip 状态。每次 64 请求覆盖每个 view mip 的四个角点并补充内部点；包括 1×1、NPOT、非零 base、单级 view 和 15 级链。GPU fetch 与模型比较，两份 SSBO/BDA 的尺寸/级数读回做精确比较；query CSV 独立记录在 `numeric.csv.queries.csv`。上传与末级局部更新沿用已有门禁。`--case-limit 1 --inject-mismatch` 和 `--case-limit 1 --inject-query-mismatch` 分别检验颜色和查询比较器，必须返回 1。

规范继续采用 Vulkan-Docs v1.4.328 的 Integer Texel Coordinate Operations 和 Image Query Instructions（`textures-integer-coordinate-operations`、`textures-queries`），颜色解码复用 F03 的 Texel Decode。没有新增 Vulkan API、ISA 或纹理字节布局。

最终验收：host 普通/sanitizer 各 **10 组、527,509 检查**；`gpu-fetch-query` **2,880 配置、1,474,560 颜色分量、1,105,920 查询分量**，零差异，尺寸/级数精确一致，颜色最大误差 `2.980232239e-8`。18 项格式检查、F01–F03 和原 LOD0/mip-nearest/双 binding nearest、38 项范围检查、Mesa translation unit test 全部通过。颜色/查询负向校验分别只检测一个注入错误，原 160×96 any-hit 图像 SHA256 不变。证据汇总为 `artifacts/texture_model/fetch-query-implementation/verification.json`，保留命令、CSV、库/源码/ELF 哈希。

## F05：View component swizzle

2026-10-07 增加 `View.components`，四个输出分量可各自选择 IDENTITY、R/G/B/A、ZERO/ONE。R/G/B/A 是解码后的逻辑通道；例如 sRGB view 把 A 映射到 R 时，R 得到原 alpha 的 UNORM 值，不能对 alpha 补做 sRGB 解码。ONE 是精确 float 1.0，不是编码字节 1。模型的 tap/结果都记录映射后的颜色；`sample_level` 没有 View 参数，仍是 image 绝对 mip helper。

软件 path 在 view 创建时检查并保存 mapping，使用每个 24 字节 mip entry 的原 reserved 字段（offset20）传递四个 Vulkan selector，每个占4 bits。80 字节 descriptor、24 字节 mip entry、F03 解码表尾和纹理字节不变。lowering 在解码/级内/跨级过滤后对结果重排或选择常量；对当前 nearest/normalized linear filter，这与规范逐 tap swizzle 的颜色结果等价。Fetch 解码后应用 mapping，size/levels 不受影响；无效整数输入仍先保护 load，最后选择零输出。Storage image 仍要求 identity view，未扩展 storage swizzle。

`gpu-swizzle` 与 `gpu-fetch-swizzle` 各有 2,880 配置，四种格式和已有十组 image/view、九种寻址、八种 min/mag/mip 状态中轮转九种 mapping。包含 identity/explicit identity、RGB 交换、AAAA/RRRR 重复、alpha/RGB 互换和常量；每种 mapping 完整运行320配置。这是 GPU 的代表性 corpus，不是全部 2,401 mapping 的笛卡尔积。Host 则穷举全部 mapping。`gpu-multi-swizzle` 的432配置在 sRGB/UNORM、两种 sRGB 通道顺序及共享 image 的不同 view 上交换带不同 mapping 的描述符并更新纹理。原 view-swizzle 拒绝门禁替换为非法 selector 拒绝门禁。

规范沿用 Vulkan-Docs v1.4.328 的 Sampling Operations、Component Swizzle 与 F03 Texel Decode。未来 F06 的 opaque-black border 加 nonidentity view 在未启用相应 border-color-swizzle feature 时存在未定义组合；本切片没有 border sampling，不把该限制隐藏在“全部 swizzle 已实现”的说法里。

最终主目录验收：host 普通/sanitizer 各 **11组、778,593检查**。新 sample/fetch 各 **2,880配置、1,474,560颜色分量**，fetch 另有 **1,105,920精确查询分量**；新双binding **432配置、663,552分量**，均零差异，最大误差 `1.192092896e-7`。每组18项格式检查，F01–F04及原门禁、38项范围检查、Mesa translation unit test 均通过。采样/双binding/查询负向校验各检测一个注入错误，原160×96 any-hit图像SHA256不变。证据汇总在 `artifacts/texture_model/view-swizzle-implementation/verification.json`，下一切片为F06。

## 代码组织与规范来源

- `model.hpp` / `model.cpp`：功能模型，只使用 C++ 标准库。
- `test.cpp`：独立 oracle、手算例子和回归。
- `demo.cpp`：采样过程示例。
- `gpu.cpp` / `gpu.py` / `sample.rgen` / `sample_mip.rgen` / `sample_multi.rgen` / `sample.rmiss`：真实 Vulkan→Spike 数值比较与运行证据。
- 一级入口 `../texture_model.py`：host 构建与运行。

模型语义依据 [Vulkan Sampling](https://docs.vulkan.org/spec/latest/chapters/textures.html)，特别是 Image Level(s) Selection、Texel Filtering 和 Texel Addressing。2026-10-05 核查了 [官方规范源文件](https://github.com/KhronosGroup/Vulkan-Docs/blob/main/chapters/textures.adoc) 的 preferred/alternative mip nearest 规则。没有将该文档的全部功能纳入本模型。

F01 依据固定版本 [Vulkan-Docs v1.4.328](https://github.com/KhronosGroup/Vulkan-Docs/blob/v1.4.328/chapters/textures.adoc)，使用 Image Level(s) Selection 和 Texel Filtering 两节；本地源文件 SHA256 为 `0ad90c16ec0d2d56dc606dcae62591b9426524c462beab20ab828cfdaa338a93`。

持续学习与跨层实现计划见外层仓库的 [纹理指南](../../../docs/architecture/VentusGPU_TEXTURE_IMPLEMENTATION_GUIDE.md)。
