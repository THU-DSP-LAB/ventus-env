# Ventus RT 回归输入

光追回归只使用一套维护输入：

- `workloads/rt/SaschaWillems_Vulkan`：固定官方源码提交 `3b843fbf667a89a1cfcc64405e9fc6f9018e03b4`，不直接修改。
- `testcases/RTcoreSpikeCase/SaschaWillems_Vulkan`：五例应用、GLSL、场景、截图钩子和 manifest。
- `build/rt-workload/source`：准备脚本组合出的完整源码与生成的 SPIR-V。
- `build/rt-workload/build`：应用构建目录，不纳入 Git。

从仓库根目录执行：

```sh
git submodule update --init testcases workloads/rt/SaschaWillems_Vulkan
bash build-ventus.sh --build rt-workload
python3 tools/rtcore.py regression
```

准备脚本初始化固定 GLM 提交 `1ad55c5016339b83b7eec98c31007e0aee57d2bf`，
从 Git 对象导出上游，仅叠加 testcases 中已跟踪的维护输入，再重新编译五例的
GLSL。本地额外测例、诊断 shader、已有构建产物不会混入生成树。
上游首次初始化可能需要联网；场景资产直接来自 testcases，无需拉取整个资产库。

重复准备只刷新脚本拥有的生成树。已有旧构建没有 ownership 标记时不会覆盖；
可用 `RT_WORKLOAD_BUILD_ROOT` 选择新目录，并在回归中显式传入对应的
`--workload-dir` 和 `--build-dir`。已有个人 workload 也可继续通过这两个选项使用。

每次改动应用或 GLSL 后重新执行 `--build rt-workload`。五例完整运行命令和
验证边界见 [RT 使用说明](../../docs/rtcore/validation.md)。
`tests/resource_layout` 是独立的 descriptor/多 geometry 定向测试，应保留；
它不代替五例图像回归。
