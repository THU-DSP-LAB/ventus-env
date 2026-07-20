# raytracingshadows 来源记录

## 上游基线

- 源码仓库：`https://github.com/SaschaWillems/Vulkan.git`
- 固定提交：`3b843fbf667a89a1cfcc64405e9fc6f9018e03b4`
- 上游许可证：MIT，见源码子模块中的 `LICENSE.md`
- 资产仓库：`https://github.com/SaschaWillems/Vulkan-Assets.git`
- 上游固定的资产提交：`a27c0e584434d59b7c7a714e9180eefca6f0ec4b`
- GLM 提交：`1ad55c5016339b83b7eec98c31007e0aee57d2bf`

该源码版本与冻结兼容基线中的 CMake、Vulkan headers、RT 基类、shader 和原始
`raytracingshadows.cpp` 一致。后继提交 `5667e237` 已把 Vulkan headers 从
1.4.351 更新到 1.4.355，因此不是该 workload 的精确基线。

## Ventus 差异

`raytracingshadows.patch` 来自旧 testcases 工作树的提交差异：

- 基线提交：`9e6a1fa`；
- Ventus 修改提交：`f56c213`。

补丁增加以下能力：

- 通过 `RT_SHADOW_ASSET` 选择场景；
- 关闭 UI overlay，避免污染验证图像；
- 通过 `RT_SHADOW_SCREENSHOT` 将 storage image 写为 P6 PPM；
- 每次进程只保存一次验证图像。

`vulkanscene_shadow_minimal.gltf` 是 Ventus 自建的快速测试场景。默认完整场景
`vulkanscene_shadow.gltf` 仍来自官方资产子模块。该上游模型带有独立的使用与
分发说明，因此本仓库只记录固定提交，不复制模型文件。

旧目录中的 `raytracingsbtdata` 修改属于另一项实验，不是当前
`raytracingshadows` P1 兼容门禁的一部分，本次迁移不吸收。
