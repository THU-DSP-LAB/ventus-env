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

这些测试证明独立模型的已覆盖规则；尚未证明 Vulkan→Spike 采样、指令执行、缓存、同步、RTL 或性能正确。后续可将同一输入交给 GPU 软件路径、Spike 模型和 RTL，分别记录对照结果。

## 代码组织与规范来源

- `model.hpp` / `model.cpp`：功能模型，只使用 C++ 标准库。
- `test.cpp`：独立 oracle、手算例子和回归。
- `demo.cpp`：采样过程示例。
- 一级入口 `../texture_model.py`：host 构建与运行。

模型语义依据 [Vulkan Sampling](https://docs.vulkan.org/spec/latest/chapters/textures.html)，特别是 Image Level(s) Selection、Texel Filtering 和 Texel Addressing。2026-10-05 核查了 [官方规范源文件](https://github.com/KhronosGroup/Vulkan-Docs/blob/main/chapters/textures.adoc) 的 preferred/alternative mip nearest 规则。没有将该文档的全部功能纳入本模型。

持续学习与跨层实现计划见外层仓库的 [纹理指南](../../../docs/architecture/VentusGPU_TEXTURE_IMPLEMENTATION_GUIDE.md)。
