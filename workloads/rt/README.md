# Ventus RT workload

本目录保存 Ventus Vulkan RT 全应用验证所需的、可复现的 workload 输入。

`SaschaWillems_Vulkan/` 直接引用官方
[`SaschaWillems/Vulkan`](https://github.com/SaschaWillems/Vulkan) 仓库，固定在
`3b843fbf667a89a1cfcc64405e9fc6f9018e03b4`。不要把它替换成浮动的
`master`，也不要在子模块内直接保存 Ventus 修改。

`ventus-overlay/` 只保存 Ventus 自己维护的差异：

- `raytracingshadows.patch`：增加场景选择和 storage image PPM 导出；
- `vulkanscene_shadow_minimal.gltf`：用于快速 ABI、SBT、payload 与 hit/miss
  冒烟验证的两三角形场景；
- `provenance.md`：记录上游版本、资产来源和迁移边界。

首次使用时只初始化 workload 源码子模块：

```sh
git submodule update --init workloads/rt/SaschaWillems_Vulkan
workloads/rt/prepare.sh
```

`prepare.sh` 会自行初始化固定版本的 GLM，并从公开的
`SaschaWillems/Vulkan-Assets` 仓库稀疏获取
`models/vulkanscene_shadow.gltf`、`models/reflection_scene.gltf` 和
`textures/gratefloor_rgba.ktx`，不会递归下载完整资产仓库。它会校验源码提交、
GLM 提交、资产提交及资产 SHA-256，然后在 `build/rt-workload/source/` 中复制上游
源码并应用 overlay；官方子模块始终保持干净。

从仓库根目录构建 workload：

```sh
bash build-ventus.sh --build rt-workload
```

完整 Spike RT 构建和逐字节出图验证命令见仓库根目录的
[`README_zh_cn.md`](../../README_zh_cn.md)。

纹理与 any-hit 路径可单独验证：

```sh
tools/rtcore/verify_textured_any_hit_image.sh
```
