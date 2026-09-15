# Register-aligned + detailed PMU baseline — 2026-09-15

这是恢复 resident AS table 优化之前的源码与性能 checkpoint。
寄存器对齐及既有正确性修复保留，新增 detailed PMU 已实现并验证；
不再使用本文早期版本中“detailed 只有配置名称”的描述。

## Source checkpoint

| 仓库 | 完整 revision | 内容 |
| --- | --- | --- |
| gpgpu | `12593cbda94453d3271bff666cb4cd69c364bc5d` | 正确性修复、寄存器分配断言、可选主机累计 detailed PMU |
| driver | `e41e0356ebdce6b653dcb8c59d43025c2455e223` | 可选 extended GPU/RT JSON 导出，兼容旧运行库 |
| Mesa | `307af3f9be40e6e254a3668850f9dd12afb9b047` | launch SGPR/VGPR 按 4 对齐、resident PDS 池修复 |
| Spike | `823614c3a1f19c35df8c6f9782d21b91203941a8` | watertight 求交及有界诊断 |
| LLVM | `fb2d0bff35e9355c46e0ea7cd4e7a7d42d5a66c5` | 本轮使用的编译器源码 checkpoint |

本次文档更新所在的顶层提交固定上述 submodule revisions，并包含回归工具
与本文。顶层 `/docs/` 已加入忽略规则，本文是此前已跟踪文件，继续更新；
下文链接的其他新增指标报告仅保存在本地，不随 checkout 自动恢复。
旧顶层 `5bc6ab11234ceec8ab8ee35071ec4642f71ddba4`、
旧 gpgpu `e5fe7e4` 不包含本轮 detailed PMU。

测试在提交前运行，artifact provenance 保留当时 HEAD + dirty diff/未跟踪
源码指纹；不是提交后重跑。上述新提交收录了已验证的 PMU 实现。

未新建分支或 worktree，未 push。无关文档、IDE 配置、构建/安装目录、
driver 的无关 `.gitignore` 修改及 Mesa 未提交的 direct-callback-group
调试开关均不属于 checkpoint；测试显式禁用该 Mesa 开关。

## 当前配置与统计范围

- 2 SM，32 lane/warp，1 warp/workgroup，1 Verilator 模型线程。
- 每个 RTCore 一个 resident session；8 Box4、2 Triangle、1 resolve。
- AS table 仍为 32 条目、每条目最多 2 waiter，只合并 unsent 请求；
  最后一个 waiter 消费后释放条目，不保留 resident line。
- Scheduler 仍是单路 AS readyLine，RT LSU 仍为单在途。
- 外部读延迟配置为 100 cycles，不是测得的完整 LSU 延迟。
- canonical Mesa 构建为 `mesa/build-ventus`。

| PMU 模式 | 当前实现 |
| --- | --- |
| off | 沿用关闭策略，不含新增 detailed/L2 观察端口 |
| basic | 原有 RT 计数；JSON 补充已有 pipeline、指令分类和 L1 计数 |
| detailed | 同 basic，通过组合观察端口、C++ 主机累计 IU/AS/状态/延迟/复用直方图及 L2 事件 |

Detailed 不驱动 ready/valid 或调度决策。外层 JSON 为 schema v2，
`extended` 内部为 schema v1。当前没有 resident eviction，替换后再访问
指标为 null，不是测得零。完整口径见 [detailed PMU](../detailed-pmu.md)。

## 后续优化使用的 16×9 基线

固定 `VENTUS_VERILATOR_SEED=544222971`，1 benchmark frame，无 waveform/
printf 诊断开关，使用新生成 ELF 和 lavapipe 参考图。

| 例子 | basic cycles | detailed cycles | 图像差异像素 |
| --- | ---: | ---: | ---: |
| raytracingshadows | 151,384 | 151,384 | 0 |
| raytracingreflections | 299,257 | 299,257 | 0 |

两例均正常退出。basic/detailed 的 ELF SHA256、逐 SM 原有 RT 计数及全部
pipeline/inst_class/dcache 计数相同，主机握手统计与 RTL PMU 交叉校验通过。
`clock_cycles` 为累计仿真时间除以 10，包含启动/复位等阶段，不是 RT-only
周期；IU 利用率使用对应窗口的 sampled_cycles。

Artifact 根目录为 `artifacts/rtcore-regression/runs/`：

| 测试 | Run |
| --- | --- |
| basic 16×9 两例 | `20260915T093547.573841Z-1821973` |
| detailed 16×9 两例 | `20260915T092724.754551Z-1629017` |
| basic 1×1 两例（原运行库） | `20260915T091925.585268Z-1541539` |
| detailed 1×1 两例 | `20260915T092408.282414Z-1590545` |
| 新 basic 导出验证，1×1 shadows | `20260915T093039.240049Z-1669857` |

每例保留 `verilator/perf.json`、`perf-summary.json`、render 日志、ELF、
`comparison.verilator.json` 和 run provenance。具体指标见
[首轮 PMU 验证报告](2026-09-15-detailed-pmu-validation.md)。

1×1 同 seed 对照通过：shadows 18,912 cycles，reflections 27,300 cycles。
主机观察器定向测试及回归工具 18 项单测通过；off/basic/detailed 均可生成
RTL，basic/detailed 运行库及驱动构建成功。off 未另跑完整渲染。
此前寄存器 bank 对齐测试、Mesa Ventus 7/7 和 1×1 Spike 验证作为前置
记录保留，不宣称本次重新运行了全部前置测试。

## 复测命令

按上述 revisions 构建对应 isolated basic/detailed 运行库及驱动后执行。
每条命令保持一行；结果进入新的唯一 run 目录，不覆盖旧 artifact。

```sh
env -u VENTUS_VK_RT_DEBUG_DIRECT_CALLBACK_GROUP_2 VENTUS_SPIKE_LOG=0 VENTUS_VERILATOR_SEED=544222971 python3 tools/rtcore.py regression --hardware-runner verilator --pmu-mode basic --case raytracingshadows --case raytracingreflections --include-candidates --width 16 --height 9 --benchmark-frames 1 --timeout-seconds 1200
```

```sh
env -u VENTUS_VK_RT_DEBUG_DIRECT_CALLBACK_GROUP_2 VENTUS_SPIKE_LOG=0 VENTUS_VERILATOR_SEED=544222971 python3 tools/rtcore.py regression --hardware-runner verilator --pmu-mode detailed --case raytracingshadows --case raytracingreflections --include-candidates --width 16 --height 9 --benchmark-frames 1 --timeout-seconds 1200
```

## 历史记录与已知限制

- 原 register-aligned 16×9 run `20260915T052151.690336Z-3092336` 两例图像
  通过，但使用随机 seed `380312329`。旧周期 154,559/295,547 不与本轮
  固定 seed 数据混用；[旧报告](2026-09-15-16x9-pmu-report.md) 仅作历史记录。
- 先前 32×18 basic batch：
  `artifacts/rtcore-perf/baseline-20260915-basic/runs/20260915T061801.733045Z-3746417`。
  reflections 通过，997,668 cycles；shadows 出现 UNDEFINED INSTRUCTION，
  未获得有效图像/PMU。不能宣称 32×18 两例均通过，失败样本不用于性能比较。
- 9 月 7 日 shadows 的 144,879 cycles 缺少完整旧 run 配置，不能仅凭周期差
  归因于某项修复。后续 AS 优化先与本文同 seed 基线对比。
- 面积由独立工作处理，本 checkpoint 没有提供综合面积验证。
