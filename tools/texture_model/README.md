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
| Image | 2D / 2D array、单 sample；完整或部分传统 mip 链，层数不随 mip 改变 |
| 格式 | RGBA8/BGRA8 UNORM 或 sRGB；RGBA 浮点结果，sRGB RGB 在过滤前解码，alpha 保持 UNORM |
| 存储 | 各 mip 独立 offset、width、height、row/layer pitch；允许行/层填充和非零偏移 |
| 寻址 | U/V 独立的 repeat、mirrored repeat、clamp-to-edge、clamp-to-border |
| Texel 过滤 | nearest 或 bilinear；独立 minFilter/magFilter，按 sampler clamp 后的 LOD 选择 |
| Border color | 核心 float transparent black/opaque black/opaque white，逐 tap 替换后过滤；opaque black 采样仅覆盖 identity view |
| LOD | 显式有限值；非零 sampler bias 先做 binary32 加法，再限制到 sampler/view 范围；bias 范围为 [-16,16] |
| 显式梯度 | `sample_grad` / `textureGrad`：有限 binary32 二维 dx/dy，以 view 基础级尺寸计算各向同性 LOD；再使用已有 bias/clamp/min-mag/mip/filter |
| Mip 过滤 | nearest（精确半整数选择较低一级）或 linear（相邻两级混合）；与级内过滤独立 |
| View swizzle | IDENTITY、R/G/B/A、ZERO/ONE；解码后通道置换/重复或浮点常量；适用于 sample/fetch |
| Offset | sample/fetch 的常量 signed 二维 texel offset，分量 [-8,7]；每个访问 mip 独立应用，不改变 LOD 或过滤权重 |
| 整数读取/查询 | 合法整数坐标 `texel_fetch`；相对 View 的 `texture_size`、`texture_array_size`、`texture_query_levels`；不使用 sampler |
| Layer/View | 独立 base layer/count；采样 layer 相对 view，以 ties-to-even 取整并 clamp，层间不插值；fetch 使用合法整数 layer |
| Trace | layer 与 view 范围、下/上 mip、跨级权重、输入 UV/LOD、受限后的相对 LOD；每个 tap 的绝对 mip、坐标、偏移、合并权重和颜色 |
| 多资源 | 不依赖全局采样状态，可交替采样不同 Image 和 Sampling 对象 |

`sample_level` 在指定的 image 绝对 mip 内用 `mag_filter` 采样，它是直接指定级别与级内过滤的 helper，不进行 LOD/min/mag 选择。`sample_lod` 的 LOD 则相对 View；例如 `{base_mip=1, level_count=2}` 的 LOD 0 访问 image mip 1。每个 mip 的尺寸按 `max(1, previous/2)` 递减；可不提供后续级别，但不能在 1×1 之后继续追加重复 mip。

当前尚无隐式导数、压缩、比较过滤、cube/3D 或模型的 descriptor binding 解析。多 Image 的纯函数测试不等于 Vulkan descriptor array 或 non-uniform indexing 已实现。

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

测试包含十六组，普通和 ASan/UBSan 模式目前均通过 3,248,351 项检查；包含独立 bias 与显式梯度 oracle：

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
12. 三种 core float border color、角点/边缘手算、七种混合 border 寻址组合、独立延伸图像 oracle、huge finite UV、合法 swizzle 和非法状态。

13. 非零 bias 的独立 LOD oracle，正负/端点/非法值及 clamp/filter 顺序。
14. 显式二维梯度的独立 `hypot/log2` oracle，view 基础尺寸、dx/dy 分别主导、旋转/负梯度、零/极小/巨大有限梯度、精确半级和相邻浮点值。

15. 常量二维 sample/fetch offset 的独立延伸 texel oracle、正负范围端点、1×1/非二次幂/mip/view、min-mag/bias/sRGB、过滤权重不变与合法最终 fetch 坐标；非法 offset、坐标上溢及极小负 UV 边界。

16. Array layer 的独立颜色/地址 oracle：1/2/4 层、全部格式、行/层填充、mip/layer view 子范围、半整数及相邻浮点值、极值 clamp、host 舍入模式独立性、sample/grad/fetch/query、无跨层过滤与非法 backing。

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
python3 tools/texture_model.py gpu-border
python3 tools/texture_model.py gpu-fetch-border
python3 tools/texture_model.py gpu-multi-border
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

## F06：Clamp-to-border 与核心浮点 border color

2026-10-07 增加 U/V 独立 `ClampToBorder` 和 `Sampling.border_color`：透明黑 `[0,0,0,0]`、不透明黑 `[0,0,0,1]`、不透明白 `[1,1,1,1]`。每个 tap 按其原整数坐标判断对应 border 轴是否越界，越界用 border color 替代，再参与 bilinear/mip-linear；不是仅凭中心UV决定整次采样是否返回 border。1×1 红色纹理在(0,0) bilinear时，透明黑得到 `[0.25,0,0,0.25]`，不透明黑得到 `[0.25,0,0,1]`，不透明白得到 `[1,0.75,0.75,1]`。

Border UV 先限制到 `[-1,2]`，该范围保留所有边缘混合，远处的所有 tap 都是同一 border color，避免 huge finite UV 乘尺寸后转换整数溢出。模型 border tap 标记 `border=true`，它的 x/y/byte_offset 占位值不代表 image 地址。GPU 先 clamp 待读取的 tap 坐标到有效范围，可能仍执行有效texel的无条件load，但在过滤前选择 border replacement；不生成越界地址。这是功能实现，未做TMU/cache访问数或性能承诺。

Sampler保存 borderColor，私有 descriptor offset76 的低8bits保存 mipmapMode、bits8..15保存 Vulkan borderColor；lowering分别提取，80字节descriptor、24字节mip entry、view mapping和解码表尾不变。这不是冻结的硬件接口，也未改变纹理存储。`vkCreateSampler`允许当前三种float border，主动使用border时拒绝integer/custom/非法color；不使用border时该字段仍可无效于采样计算。

Core规范把 opaque-black sampler 与非identity view 的部分组合定义为未定义；当前模型对该采样组合诊断拒绝，GPU不承诺该组合结果，验收只使用identity/explicit-identity view。透明黑/不透明白验证非identity mappings。未启用 `VK_EXT_border_color_swizzle` 或自定义border扩展。Fetch/query不读取samplerword；对创建成功的sampler，border寻址与颜色均不影响合法整数读取/查询，fetch可以使用其自身合法非identity view。

`gpu-border` 与 `gpu-fetch-border` 各 **6,720配置**，十组image/view × 四种格式 × 七种带border轴的寻址组合 × 三种color × 八种min/mag/mip状态。前者按color限定合法mapping并轮转九种corpus，后者不受opaque-black采样限制。包含边缘、负零、巨大finite binary32 UV、coherent上传和末级局部更新。`gpu-multi-border` 的432配置在两binding间交换border颜色、模式和clamp，覆盖混合sRGB/UNORM、双sRGB及共享image；有opaque-black时使用identity views确保sampler交换后仍合法。范围检查把旧border拒绝项换成integer-border拒绝，并新增非法bordercolor，总计39项。

规范依据继续固定 Vulkan-Docs v1.4.328 的 Border Replacement、Wrapping Operation、Component Swizzle 和 Instruction/Sampler/Image View Validation。Host/GPU误差标准保持 `2e-7`/`1e-6`。

## 代码组织与规范来源

- `model.hpp` / `model.cpp`：功能模型，只使用 C++ 标准库。
- `test.cpp`：独立 oracle、手算例子和回归。
- `demo.cpp`：采样过程示例。
- `gpu.cpp` / `gpu.py` / `sample.rgen` / `sample_mip.rgen` / `sample_multi.rgen` / `sample.rmiss`：真实 Vulkan→Spike 数值比较与运行证据。
- 一级入口 `../texture_model.py`：host 构建与运行。

模型语义依据 [Vulkan Sampling](https://docs.vulkan.org/spec/latest/chapters/textures.html)，特别是 Image Level(s) Selection、Texel Filtering 和 Texel Addressing。2026-10-05 核查了 [官方规范源文件](https://github.com/KhronosGroup/Vulkan-Docs/blob/main/chapters/textures.adoc) 的 preferred/alternative mip nearest 规则。没有将该文档的全部功能纳入本模型。

F01 依据固定版本 [Vulkan-Docs v1.4.328](https://github.com/KhronosGroup/Vulkan-Docs/blob/v1.4.328/chapters/textures.adoc)，使用 Image Level(s) Selection 和 Texel Filtering 两节；本地源文件 SHA256 为 `0ad90c16ec0d2d56dc606dcae62591b9426524c462beab20ab828cfdaa338a93`。

持续学习与跨层实现计划见外层仓库的 [纹理指南](../../../docs/architecture/VentusGPU_TEXTURE_IMPLEMENTATION_GUIDE.md)。


F06在TASK-1321修复LLVM leaf函数入口VGPR栈分配后完成验收。原来通用stackSize为0时跳过prologue，epilogue却仍释放VGPR frame；新增MIR回归独立验证有frame/无frame的入口与返回平衡。LLVM提交 `9d3d82603b4b`，未修改Spike、ISA编码/语义、默认优化或容差；历史调查及精确测试边界见根仓库 `docs/architecture/VentusGPU_TEXTURE_BORDER_CODEGEN_BLOCKER.md`。

修复后重新执行全部18个正向GPU命令、三种负向控制、host普通/sanitizer各12组1,375,170检查及Mesa unit，均符合预期。新sample/fetch各6,720配置、3,440,640颜色分量，fetch另有2,580,480精确查询分量；multi432配置、663,552分量，零差异，最大误差1.192092896e-7。主目录补验sample/multi/39项范围检查/unit/原any-hit通过，原图hash完全一致。LLVM扩展89项检查仍有18项与修复前相同的历史失败，新增栈回归由失败变为通过，没有新增失败；不声明整套LLVM全绿。证据在 `artifacts/texture_model/border-implementation/completed-verification.json`；下一切片F07独立image/sampler。

## F07 独立 sampled image/sampler

2026-10-07，TASK-1322 为现有软件路径加入 `VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE` 和 `VK_DESCRIPTOR_TYPE_SAMPLER`。Shader 的 texture deref 与 sampler deref 分别解析自己的 set/binding：image 地址、格式、mip/view 和 swizzle 从 image 记录读取，过滤、寻址、LOD clamp 与 border color 从 sampler 记录读取。Combined descriptor 仍让两种 deref 指向同一个记录。NIR texture instruction 进入现有 LLVM lowering 后生成软件计算及普通访存，没有新增 Vulkan 扩展、ISA 指令或 TMU 接口。

描述符更新分别保存 image/view 和 sampler；dispatch capture 与上传分别物化对应状态。私有记录仍是80字节，mip记录仍是24字节，槽号仍为 `set * 32 + binding`。本步支持固定、标量、完整绑定的资源，未实现 immutable sampler、descriptor array 或动态索引。参考模型已经把 image/view 和 Sampling 分开，因此无需改变模型数值规则。

新增三个可复现实验：

- `gpu-separate`：image 在 set0/binding2，sampler 在 set1/binding6，验证跨 set 的独立解析，并覆盖 F01–F06 已支持的过滤、格式、view 和边界状态。
- `gpu-fetch-separate`：只有 sampled image，没有创建 sampler，没有 sampler binding；SPIR-V 中没有 `OpTypeSampler`/`OpSampledImage`，整数 fetch、尺寸和级数查询正常执行。
- `gpu-multi-separate`：image 在 set0/binding2、13，sampler 在 set1/binding6、7。八阶段覆盖基线、交换 image、交换 image 与 sampler、仅交换 sampler、共享 sampler、更新 image、保持 descriptor 再 dispatch、恢复 image；三组资源包含混合格式、不同 view 和共享 image。

隔离源码验收：sample/fetch 各6,720配置、3,440,640颜色分量；fetch另有2,580,480精确查询分量。multi有432配置、663,552分量。均零差异，最大颜色误差 `1.192092896e-7`。21个正向命令和6个预期失败的比较器负向控制符合预期；39项范围检查继续拒绝 descriptor array，原先“独立 sampler 不支持”的检查改为独立 image array 检查。Host模型12组1,375,170检查和 Mesa translation unit test 通过。原160×96 any-hit图像保持 SHA256 `88b724540dc17b23161c638ebcbb71e9acaf73ff098b43764f9edf17cd5bebb0`。

复现入口为 `python3 tools/texture_model.py gpu-separate`、`gpu-fetch-separate`、`gpu-multi-separate`，支持 `--mesa-build` 和 `--out-dir`。命令、CSV、实际源码/工具/库哈希、LLVM/ELF及资源数量证明保存在 `artifacts/texture_model/separate-sampler-implementation/`；汇总见 `verification.json`。本步不扩大纹理数值 profile，不声明完整 Vulkan descriptor indexing 或硬件一致性。下一切片是 F08：固定长度 descriptor array 的常量索引。

## F08 固定 descriptor array 与常量索引

TASK-1324 已完成并通过主目录验收，基于已接受的 [ADR-0026](../../../docs/rt-implementation-architecture/decisions/ADR-0026-software-descriptor-array-directory.md)。本步的数组是descriptor引用数组，图像仍是2D、单layer/sample；只接受一维固定长度、完整绑定、合法编译期常量索引。

CPU layout/set保存每binding的type/count/stages及元素；`vkUpdateDescriptorSets`处理array element、count和兼容连续binding，copy按源/目标element复制。Set free、pool reset/destroy和失败回滚回收元素内存；分配失败清空所有输出handle。新增pool已满反例在补齐清空分支前失败、修复后须通过。Dispatch capture增加element维度；每binding上传80字节资源记录的元素表，并通过16字节目录项保存device address、count/type。目录定位仍为set*32+binding，mip entry、swizzle和sRGB decode规则不变。Dispatch carrier字段布局保留，table address/count/stride在这条私有软件路径解释为目录；不承诺旧shader二进制混用。

Compiler内部以低7位保存binding slot、高位保存element的32bit token；resource reindex只修改element，不把偏移解释为另一个binding。所有record字段读取先解析目录再访问element*80；image/sampler各自解析。这个token不是ISA/TMU接口。RV32在dispatch前检查目录/元素表device地址范围，RV64 lowering采用64bit地址读取；当前数值验收运行于RV32 Spike，不能将代码路径存在称为RV64运行已验证。

实验增加 `gpu-multi-array`、`gpu-multi-array-separate`、`gpu-fetch-array`，参数 `--array-length 1|2|4`、`--array-index`、`--array-binding 2|12`。Shader输入在artifact目录内生成并保留哈希，数组下标是编译期字面量；独立sampler使用反向下标，证明image/sampler索引分别解析。Binding2紧邻SSBO3，binding12/13测试两个相邻数组。局部元素更新、单元素copy、跨binding的双元素write/copy和重复dispatch均与既有数值oracle比较。双元素copy让被采样元素对应第二个destination，不能靠仅复制element0通过。

规范依据固定 [Vulkan-Docs v1.4.328 descriptorsets.adoc](https://github.com/KhronosGroup/Vulkan-Docs/blob/v1.4.328/chapters/descriptorsets.adoc)，SHA256 `8ff85412f05323d9d21e98ee1480fe8e38a721006d84c683931ce203ba3b2e52`。范围门禁以动态array和显式nonuniform array替代旧constant-array拒绝项。动态索引feature未提前开放，F09/F10、runtime array、immutable sampler、partially bound、variable count、update-after-bind和robustness扩展均未作为本步承诺。

最终验收：长度1/2/4的所有合法常量下标分别验证combined与separate，三种长度的fetch/query也完整通过；另有相邻数组及跨binding双元素copy验证。每个完整multi profile为432配置、663,552颜色分量，每个fetch profile为6,720配置、3,440,640颜色分量和2,580,480精确查询分量，均零差异，最大颜色误差1.192092896e-7。共65个有效命令（含主目录补验）符合预期，其中53正向、12比较器负向；范围检查40项通过，host12组1,375,170检查和隔离/主目录unit通过。Pool已满反例在修复前失败、修复后输出NULL与free/reset/reallocation检查通过。原any-hit图像在隔离/主目录保持原SHA256。LLVM未修改，18项既有失败未处理。

原array-separate-4-1执行期间host测试源码发生更新，导致manifest源码快照与已编译程序不一致；该次不计入有效验收，保留诊断证据，并以array-separate-4-1-replay重新执行通过。最终状态、命令、manifest/CSV/LLVM/ELF及失败反例保存在 `artifacts/texture_model/constant-array-implementation/verification.json`。Mesa本地实现提交为2948af7。下一切片是F09动态一致索引；本步不提前声明它已支持。

F09 adds `gpu-multi-uniform`, `gpu-multi-uniform-separate`, and
`gpu-fetch-uniform`, using the same array arguments. One pipeline/retained ELF
loads dispatch-wide push-constant indices; image and sampler indices change
across dispatches (samplers use the reverse index). Multi phase6 retains phase5
selectors for the unchanged-state redispatch. The manifests retain per-index
counts, queried/enabled core `shaderSampledImageArrayDynamicIndexing`, SPIR-V
loads/access chains and LLVM dynamic descriptor addressing. The nonuniform
feature, runtime arrays, partially bound and variable count remain disabled.

`--inject-index-mismatch` pins the pushed GPU index to zero while retaining the
intended CPU selection and must fail for the multi/fetch length4 profiles.
`gpu-checks` retains40 checks; the three array rejection cases now explicitly
mark combined, separate image and sampler indices NonUniform. Unmarked actual
nonuniform indexing is invalid usage rather than a required compiler diagnosis.
Evidence is retained under `artifacts/texture_model/uniform-index-implementation/`.
This remains a bounded RV32 Spike software profile, not full Vulkan conformance
or a texture hardware ISA/ABI.

## F10 非一致数组索引

新增 `gpu-multi-nonuniform`、`gpu-multi-nonuniform-separate` 和
`gpu-fetch-nonuniform`，沿用array参数。开启采样类NonUniform feature，
保留显式SPIR-V标记和逐lane descriptor寻址；native与分歧implicit LOD
property仍为false。Multi同一warp访问不同资源，A屏蔽四分之一lane、B
屏蔽一半lane，随后形成混合输出。Fetch不同lane选择不同mip view与
swizzle，生成各自合法的坐标/LOD，精确核对尺寸及级数。

`--inject-lane-index-mismatch` 提供逐lane索引错误反例。Fetch反例使用
`gpu-fetch-nonuniform --case-limit 1 --array-index 0 --inject-lane-index-mismatch`，
保证错误选择的view仍覆盖输入，排除越界语义。Multi使用16配置。
CSV记录实际lane资源、sampler、view、元素下标及活跃状态；SUMMARY samples
计数是读回的逻辑输出数量，真实活跃采样数量见LANES。

最终46个有效命令符合预期（41正向、5负向）；新长度2/4完整矩阵、
F01–F09累计回归、40项范围检查、unit/host和原图通过。规范、源码、
命令及数值证据见 `artifacts/texture_model/nonuniform-index-implementation/verification.json`。
F01–F10在既定RV32 Spike软件profile中累计完成；这不冻结TMU接口或声明
完整Vulkan/RV64/RTL一致性。LLVM backend/Spike/ISA未改，18项既有LLVM失败未处理。

## E01：sampler LOD bias（2026-10-07）

沿用现有 GPU profiles，并使用 `--lod-bias` 指定 sampler 状态。例如：

```sh
python3 tools/texture_model.py gpu-min-mag --lod-bias 0.5
python3 tools/texture_model.py gpu-multi-nonuniform-separate --lod-bias -1 --array-length 4
python3 tools/texture_model.py gpu-fetch-border --lod-bias 16
python3 tools/texture_model.py gpu-min-mag --lod-bias 1 --case-limit 8 --inject-bias-mismatch
```

Multi profiles 的两个 sampler 使用相反 bias，交换、共享及逐 lane 索引后仍按实际 sampler 计算 oracle；CSV 保存每个采样所用的 bias。`--inject-bias-mismatch` 让 GPU sampler 使用零 bias 而 oracle 保留指定 bias，必须返回 1。Fetch profiles 验证采样状态不影响整数读取和查询；separate fetch 仍不绑定 sampler。

依据 Vulkan-Docs v1.4.328 的 LOD Operation，sampler bias 参与显式 Lod 采样，先于 sampler clamp、min/mag 判定和 view/mip 选择。GPU 输入 bias 为有限 binary32，`maxSamplerLodBias` 查询值为 16。资源元素在偏移 80 追加 bias，大小从 80 扩为 88 字节；binding 目录、24 字节 mip 表和 texel 存储不变。旧阶段的 80 字节描述保留为历史记录。

E01 同时暴露了 LLVM 的 uniform FP scalar pattern 优先级问题：混合 vector fallback 抢先选择，随后产生非法 VGPR→GPRF32 COPY。LLVM `a4e6dfe7f818` 提升严格 uniform 的 add/sub/mul/div pattern 优先级；新增 scalar/zero-add 回归通过，既有 mixed/vector 检查没有新增失败，Spike 与自定义编码未改。完整证据保存在 `artifacts/texture_model/bias-implementation/`；下一项是显式二维梯度 E02。

## E02 显式二维梯度

```sh
python3 tools/texture_model.py gpu-min-mag --gradients
python3 tools/texture_model.py gpu-min-mag --gradients --lod-bias 0.5
python3 tools/texture_model.py gpu-min-mag --gradients --constant-gradient 0.125 0.125 0 0
python3 tools/texture_model.py gpu-multi-nonuniform-separate --gradients --array-length 4
python3 tools/texture_model.py gpu-min-mag --gradients --case-limit 8 --inject-gradient-mismatch
```

最后一个命令将 GPU 的梯度改成零，模型保留真实梯度，必须返回 1 并报告颜色差异。`--gradients` 只适用于 mip-capable sample profile，不适用于整数 fetch/query、LOD0-only profile 或 API 范围检查。`--constant-gradient` 接收 dx.xy、dy.xy 四个分量；默认 corpus 则逐 invocation 提供四分量，覆盖任一方向主导、旋转、负值、零和有限 binary32 极值。请求 buffer 每次采样为两个 vec4，第一项为 UV/dx，第二项为 dy；这只是实验输入，不是纹理存储或硬件接口。

参考模型先按 binary32 计算 `q=max(dot(dx*viewBaseSize),dot(dy*viewBaseSize))`，再用独立标准库 `0.5*log2(q)` 得到 binary32 LOD 输入。零/上溢 footprint 保留诊断值，并进入既有 sampler clamp。GPU 解析标准 NIR `txd/ddx/ddy`，在 Mesa 的 NIR→LLVM 转换里展开：对 q 做当前 2D profile 等价的范围限制，以指数/尾数分解和奇次多项式计算 log2，再进入既有 bias/filter 路径。无新 NIR intrinsic、LLVM texture intrinsic 或纹理指令。

GPU 的 q 限制为 `[2^-32,2^60]`，即基础 LOD `[-16,30]`；等价性依赖当前 bias±16、非负 minLod、最大 16384×16384/15 mip 的 Vulkan 范围，不能当作通用 log2 helper。精确 q=2 的幂保持整数/半整数 LOD。尾数近似截断误差小于 `1.6e-8`；梯度 profile 的颜色绝对误差上限预先规定为 `4e-6`，既有 explicit Lod profile 仍为 `1e-6`。参考模型内部没有复用 GPU 近似算法。

当前不提供 implicit derivatives、quad/helper/divergence、各向异性、projector、shadow 或其他维度的梯度采样。

E02 最终隔离/主目录验收共 43 个 GPU 命令（38 正向、5 预期失败反例），正向零差异，最大误差 `1.192092896e-7`；忽略梯度反例分别检出 2,240/2,088 差异。Mesa unit、46 项范围检查与原 any-hit 均通过。证据：`artifacts/texture_model/gradient-implementation/verification.json`。LLVM 配套提交 `88d9b6874ef0` 修正严格 divergent FP 规则被 fallback 抢先匹配；目录 89 项中 71 PASS/18 既有失败名称，未引入新失败。

## E03 常量二维 offset

```sh
python3 tools/texture_model.py gpu-min-mag --offset 1 -1
python3 tools/texture_model.py gpu-border --offset -8 7
python3 tools/texture_model.py gpu-multi-nonuniform-separate --gradients --offset 7 -8 --array-length 4
python3 tools/texture_model.py gpu-fetch-nonuniform --offset -8 7 --array-length 4
python3 tools/texture_model.py gpu-min-mag --offset 1 -1 --case-limit 8 --inject-offset-mismatch
```

最后一项把 shader offset 改成零，参考模型保持真实 offset，必须返回 1 并检测颜色差异。sample/fetch 使用标准 `textureLodOffset`、`textureGradOffset` 和 `texelFetchOffset`，对应 SPIR-V `ConstOffset` / 既有 NIR `nir_tex_src_offset`。Mesa 只接受常量二维 `[-8,7]`，设备同时报告 `minTexelOffset=-8` / `maxTexelOffset=7`；没有开启 runtime Offset、gather、projector 或新 intrinsic/ISA。纹理和描述符布局不变。

Offset 按每个访问 mip 的 texel 单位生效，随后进行 wrap/clamp/border；mip-linear 的两级分别使用各自尺寸，不把 offset 当成固定的 normalized UV 偏移。数学上整数平移与 floor 可交换：实现先算原始整数 tap 和权重，再平移整数 tap，避免极小负坐标或边界邻值被浮点加法吞掉。参考模型 `sample_level/sample_lod/sample_grad/texel_fetch` 增加可选 `TexelOffset`，Trace 保存实际 offset。它不是 sampler 状态。

Clamp 模式的提前 UV 截断拓宽到安全的 `[-9,10]`（仅非零 offset 分量），保证 `[-8,7]` 范围内可移回图像的坐标不被提前截断，巨大坐标仍安全地进入边缘/border。周期性寻址保留小的有符号 UV，修正参考模型提前加周期使极小负值舍入到边界的问题；GPU 先前已有同样保护。

Fetch 对最终整数坐标做合法性验证，不使用 sampler；实验输入构造为 `target-offset`，使每个 sampled view 的最终 fetch 坐标合法。查询尺寸/级数不使用 offset。不支持/不合法状态继续拒绝；范围检查同时验证设备两项 offset limits。

E03 最终共 42 个 GPU 命令（37 正向、5 预期失败反例），正向零差异，最大颜色误差 `1.192092896e-7`；忽略 offset 反例检出 1,768/6,088 差异。48 项范围/limits 检查、隔离/主目录 Mesa unit 和原 any-hit 通过。证据 `artifacts/texture_model/offset-implementation/verification.json`。


## E04 二维数组纹理

```sh
python3 tools/texture_model.py gpu-min-mag --image-layers 4
python3 tools/texture_model.py gpu-multi-nonuniform-separate --image-layers 4 --array-length 4 --gradients --offset -8 7
python3 tools/texture_model.py gpu-fetch-nonuniform --image-layers 4 --array-length 4
python3 tools/texture_model.py gpu-min-mag --image-layers 4 --single-layer-view
python3 tools/texture_model.py gpu-fetch-nonuniform --image-layers 4 --linear-image --array-length 4
python3 tools/texture_model.py gpu-min-mag --image-layers 4 --case-limit 8 --inject-layer-mismatch
```

`--image-layers` 指一张图像的层数；`--array-length` 是 descriptor 数组的资源引用数，两个索引彼此独立。层数实验可选 1/2/4，创建 profile 支持 1..2048 层；仍为单 sample、四种 RGBA8/BGRA8 UNORM/sRGB。`--single-layer-view` 使用普通 2D shader，读取多层 image 的一个非零层。最后一项把 shader layer 固定为零，模型保留真实请求，必须检出颜色差异并返回 1。

Sample 的 layer 坐标不归一化：相对 view 先 roundTiesToEven，再 clamp 到 `[0,view.layer_count-1]`，最后映射到 image 的绝对层。`0.5→0`，`1.5→2`，而 view 的 base layer 不参与奇偶判定。先 clamp 再对非负值做显式分数/奇偶判断与此规则等价，避免巨大有限 float 转整数越界，也不依赖 host 舍入模式。过滤和梯度 footprint 只用 U/V，层间没有插值。依据是 [Vulkan-Docs v1.4.328 的 Array Layer Selection](https://github.com/KhronosGroup/Vulkan-Docs/blob/v1.4.328/chapters/textures.adoc#textures-unnormalized-to-integer)。

模型函数的最后一个可选参数为 layer：`sample_lod/sample_grad` 接收 view-relative 浮点值；`texel_fetch` 接收合法 view-relative int32；`sample_level` helper 使用 image 的绝对整数层。旧调用默认层零。`texture_array_size` 返回宽、高、view 层数；层数与查询 mip 无关。Trace 保存选中的绝对 layer 和每个 tap 的地址。

上传 buffer 使用独立的 padded row/layer pitch，Vulkan backing 的每个 mip 存连续 layer planes；实验同时检查全层上传、最后一层的非零 baseArrayLayer 局部更新，以及多资源重上传。Image view 的 base/count mip 与 base/count layer 独立校验。`--linear-image` 还验证 `vkGetImageSubresourceLayout` 的 offset/rowPitch/arrayPitch：通过查询的布局直接定位映射内存，逐行比较已上传的独立 fixture 字节，再验证实际 shader 采样。此前该查询入口缺失，本切片补齐。Storage-image array、array attachment、cube、3D、层间过滤未开放。

软件 descriptor 按 [ADR-0028](../../../docs/rt-implementation-architecture/decisions/ADR-0028-software-texture-array-layer-metadata.md) 从 88B 扩为 96B，追加偏移 88 的 view layer count；binding directory 仍为 16B，mip record 仍为 24B。记录表按 `relativeLayer*viewMipCount+relativeMip` 索引，之后才是 sRGB decode table；producer、普通 buffer 元素寻址和 texture consumer 一起迁移。它不规定未来 TMU ABI，也未增加 NIR intrinsic、LLVM texture intrinsic 或 ISA。


E04 最终隔离/主目录共 44 个 GPU 命令（40 正向、4 预期失败反例）；正向零差异，最大颜色误差 `1.192092896e-7`。选层反例分别检出 3,072/4,008 差异；61 项支持范围/limits 检查、Mesa unit 与原 any-hit 图像均通过。源码和构建产物前后哈希一致；加入 LINEAR 查询、收紧数组 usage 前的中途验收和 smoke 不计入最终结论。证据 `artifacts/texture_model/image-array-implementation/verification.json`；下一项为 Cube 的方向/face/接缝规则。
