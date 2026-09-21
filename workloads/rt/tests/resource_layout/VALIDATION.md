# Binding 与多 geometry 验证（2026-09-21）

本次收尾只涉及 Vulkan descriptor 寻址、资源传输以及已有多 geometry
实现的验证。没有改变应用的 BLAS 划分，没有新增 Ray Query 或硬件 spill。
工作区同时存在的 BVH 栈容量和 RTL/driver 修改不属于本次 binding 收尾。

## 实现

- 保存 descriptor layout 的类型、数量和稀疏 API binding 到内部连续 slot 的映射。
  pipeline/set 保存布局副本，避免依赖 layout handle 的后续生命周期。
- 按实际 set/binding 重映射 NIR resource index、image 和 sampler；AS 的
  占位变量不包含真实绑定信息，必须使用对应 resource-index 指令。
- 记录全部 descriptor sets 和动态 offset；数组索引、descriptor 更新/复制
  计入元素下标。image store/size 不再假设 binding 1 或 dispatch 尺寸。
- bridge 按底层内存分配上传、读回可写内存，保留同一 allocation 的别名关系；
  标准复制命令按提交顺序执行。补齐 LLVM 对全局 SSBO store_deref 的写入。
- 添加 descriptor layout support 查询，明确拒绝超容量、未支持类型及 flags。
  当前容量为 4 个 set、每 set 32 个 descriptor 元素，binding 编号可大于 32。
- 保留多 geometry BLAS 构建及参数深拷贝：GeometryIndex 对应 geometry 序号，
  PrimitiveID 在各 geometry 内独立编号，构建尺寸汇总各 geometry 的图元数。

## 可重复运行的测试

入口和覆盖范围见 [README.md](README.md)。
它使用标准 Vulkan API、同一组 SPIR-V 和解析预期值，无窗口依赖。
三角形及 AABB 各有单次 dispatch、同一命令缓冲区重新绑定两种变体。
测试输出同时检查像素和 SSBO，不能仅凭 renderer 正常退出判定通过。

| 验证 | 结果 | 本地产物 |
|---|---|---|
| lavapipe：三角形/AABB 四种变体 | 4/4 通过 | `artifacts/rtcore-binding/20260921T121752/` |
| Spike：相同四种变体 | 4/4 通过 | 同上 |
| RTL basic-sm1-curve：三角形两种变体 | 2/2 通过 | `artifacts/rtcore-binding/20260921T121407/` |
| RTL basic-sm1-curve：AABB 两种变体 | 0/2，custom index 错误 | `artifacts/rtcore-binding/20260921T121526/` |
| 单 geometry AABB 对照：lavapipe/Spike | 两者通过 | `artifacts/rtcore-binding/20260921T121922/` |
| 单 geometry AABB 对照：RTL | 同样 custom index 错误 | `artifacts/rtcore-binding/20260921T121822/` |
| 维护版 Sascha 五例 4×4，lavapipe 对比 Spike | 5/5 通过 | `artifacts/rtcore-regression/runs/20260921T041241.105465Z-1697240/` |
| Mesa Ventus 单元测试 | 8/8 通过 | `build/rt-binding-test/mesa-tests.log` |

集成测试记录 SPIR-V 与所选工具链/运行库的 SHA-256。测试代码后续移到
`workloads/rt/tests/resource_layout/`，不改变上述已执行测试的语义。
RTL 测试选用现有 `basic-sm1-curve` 隔离运行库，未覆盖其他 RTL 构建。

## 未解决的 RTL 问题

AABB 的 GeometryIndex、PrimitiveID、动态 UBO 值和输出位置符合预期，
但两个实例的 custom index（7、11）均变成 0。单 geometry 对照也复现，
所以不能归因于多 geometry 合并。

源码中 `RtCoreScheduler.scala` 的 AABB candidate 路径将 HitRecordMeta0
写成固定 status/kind，未像 triangle 路径一样加入 instanceCustomIndex；
这个遗漏与实际结果一致。本次未修改 RTL，也未降低测试预期或误标通过。

## 范围限制

这是针对性兼容性证据，不是 CTS 或 Vulkan 完整一致性认证。未覆盖的
descriptor 模式和图像格式见测试 README。所有受支持布局之间的兼容性/
disturbance 规则、独立 sampler/image、immutable sampler、runtime array、
update-after-bind 等仍需独立补齐，不能据本次通过结果宣称全部支持。
