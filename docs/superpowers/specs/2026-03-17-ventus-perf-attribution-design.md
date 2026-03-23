# Ventus Performance Attribution Design

**Date:** 2026-03-17

**Status:** Draft for review

## 1. 背景与目标

当前 `ventus-env` 软件栈已经具备多种运行后端：

- `spike`
- `rtlsim`
- `cyclesim`
- `ptx` / `sbtsim`

其中 `ptx` / `sbtsim` 路径额外包含：

- Ventus ELF -> PTX 静态二进制翻译
- NVIDIA CUDA Driver API JIT 加载
- NVIDIA GPU 上执行转译结果

本设计的目标是在不破坏现有运行方式的前提下，为整条软件栈与仿真链路增加一套可扩展、可复用、可离线分析的性能统计归因系统，满足以下要求：

1. 以“单次 OpenCL 程序运行”为基本归因单位。
2. 保留程序内部每一笔关键事件的时间序列，而不是只给粗粒度总时间。
3. 尽量做到归因分项之和约等于本次运行的 wall time。
4. 对 `ptx` / `sbtsim` 提供额外细分：
   - Ventus ELF -> PTX 翻译耗时
   - NVIDIA JIT 编译耗时
   - kernel 在 NVIDIA 上执行的等待耗时
   - CUDA-facing 边界的 launch / sync 等
   - 如果当前实现路径天然可见，再额外细分到 CUDA API 级别的 memcpy
5. 支持 warmup、多次 measured run、以及 `nsys` / `ncu` 这类会干扰时序的外部 profiler。
6. 明确区分“运行态记录”和“离线报告”，后者从前者读取并再加工。
7. 默认产出最简短、最有用的 summary，同时：
   - 写入文件
   - 打印到 stdout
8. 不做 TUI 或分屏显示；报告直接写入文件。

## 2. 非目标

本设计当前不追求：

- 首版引入 HTML/TUI 可视化界面
- 首版引入 protobuf 作为唯一落盘格式
- 依赖 `vt_dump_perf()` 作为主采集通道
- 在首版就把所有 simulator 内部微结构计数器统一抽象完整
- 在同一个 pass 中同时做 baseline attribution 与 `nsys/ncu` 深度 profile

## 3. 关键设计原则

### 3.1 记录与展示解耦

- 运行态 recorder 只记录事实，不负责复杂 summary。
- report 层负责聚合、归因、渲染和对比。

### 3.2 wrapper 是唯一用户入口，环境变量是内部控制面

- 用户入口固定为 wrapper 编排 experiment。
- `VENTUS_PERF*` 环境变量仍保留，但仅作为 wrapper 与 runtime recorder 之间的内部控制面，不作为面向用户的稳定直跑接口。
- recorder、producer、wrapper、reporter 共享同一套 schema 与目录约定。

### 3.3 schema 先行，格式后定

- 先定义稳定的逻辑数据模型。
- 首版落盘采用 append-friendly 的 JSONL / JSON。
- 后续可增加 protobuf exporter/importer，但不改变上层语义模型。

### 3.4 归因基于互斥桶，不直接求和嵌套事件

- 原始事件允许嵌套。
- wall attribution 使用互斥的顶层桶，避免重复计时。

### 3.5 禁止静默降级

- 用户显式启用 perf 时，如果 recorder 初始化失败、输出目录不可写、关键日志无法创建，必须明确报错。
- `nsys` / `ncu` pass 如果工具不存在或执行失败，也必须在 experiment 中明确记录为失败，不做 silent skip。

### 3.6 首版并发语义必须显式受限

- 首版“归因分项之和约等于 wall time”的保证，仅针对当前 Ventus 执行模型成立：
  - 单进程单机运行
  - PoCL Ventus 设备路径基本同步提交与同步等待
  - backend 内不存在多个稳定可见的并发 in-flight kernel
- recorder 仍需记录 `pid`、`tid`、可选 `queue_id` 与 scope 层级，避免未来扩展时丢失事实。
- reporter 必须在发现重叠 kernel wait / launch window 时进入保守模式：
  - 明确标记 `concurrency_detected=true`
  - 不再宣称整次 pass 的闭合归因严格成立
  - 输出 per-thread / per-scope 的保守统计，并扩大 `uncategorized`

### 3.7 跨模块上下文传播必须显式建模

- 首版不得假设“同一进程中的不同共享库/可执行模块”天然共享同一份 recorder 单例、`thread_local` scope 栈或 launch sequence 状态。
- `thread_local` + RAII scope guard 只能解决“单个已链接模块内部”的语义继承。
- 如果 PoCL、driver、shim 或其他 producer 被分别编译/链接，跨模块关联必须通过显式上下文快照传播：
  - `scope_id`
  - `parent_event_id`
  - `launch_seq`
  - `kernel_name`
  - `kernel_occurrence`
  - `kernel_signature_hash`
- Phase 1 不允许把“跨模块 scope 自动继承”作为正确性前提；必须要么显式传递上下文，要么退化为基于正式顶层字段的保守关联。

推荐方案：

- 在 `vt_*` 主 ABI 之外增加独立 sideband perf context API，而不是扩展所有 `vt_*` 函数签名。
- 建议新增：
  - `vt_perf_context_t`
  - `vt_set_perf_context(vt_device_h, const vt_perf_context_t*)`
  - `vt_clear_perf_context(vt_device_h)`
- `vt_perf_context_t` 至少承载：
  - `scope_id`
  - `parent_event_id`
  - `launch_seq`
  - `kernel_name`
  - `kernel_occurrence`
  - `kernel_signature_hash`
- PoCL 在进入关键语义区间前设置当前上下文，driver 在生成 `vt_*` 事件时读取并重发这些正式顶层字段。
- 如果默认用户入口经过 `libventus_driver.so` 的 auto-select loader，则该 loader 也必须显式导出并转发 sideband perf context API，不能假设 PoCL 总是直连某个具体 backend DSO。
- 不推荐复用现有 `taskID` / `kernelID` 做隐式编码，因为它们无法稳定覆盖 `vt_start()` 前后的 buffer / memcpy / upload 路径。
- Phase 1 只要求 `ptx` / `sbtsim` 路径真正消费这些 sideband perf context。对其他 backend，可允许 sideband API 显式 no-op，但不得影响原有非 perf 执行能力。

## 4. 总体架构

整套系统拆成四个组件：

1. `runtime recorder`
2. `event producers`
3. `experiment wrapper`
4. `offline reporter`

### 4.1 runtime recorder

职责：

- 在运行时将结构化事件写入当前 pass 目录
- 为 producer 提供统一写日志接口
- 管理当前进程的上下文信息

不负责：

- 复杂聚合
- 跨 pass 对比
- `nsys/ncu` 结果解释
- `experiment.begin.json` / `experiment.json` / `pass.begin.json` / `pass.json` 这类生命周期 manifest 的权威写入

### 4.2 event producers

职责：

- 在各个边界上产生事件

包括三层：

1. PoCL 语义层
2. driver `vt_*` ABI 层
3. backend 内部细分层

### 4.3 experiment wrapper

职责：

- 创建 experiment 目录
- 编排 warmup / measured / `nsys` / `ncu`
- 为每个 pass 分配唯一目录
- 写入 `experiment.begin.json` / `experiment.json` / `pass.begin.json` / `pass.json`
- 注入环境变量与 `LD_PRELOAD`
- 收集 stdout/stderr 与 profiler artifact
- 触发离线 report

### 4.4 offline reporter

职责：

- 读取多个 pass 的原始记录
- 生成 summary / timeline / kernel / profiler 视图
- 将 baseline measured pass 与 `nsys/ncu` 补充分析综合到同一个 report

## 5. 用户入口

### 5.1 wrapper 编排 experiment

建议实现为 `tools/ventus-perf.py`。

示例：

```bash
python3 tools/ventus-perf.py run -- ./run
```

带 repeat / profiler 的示例：

```bash
python3 tools/ventus-perf.py run \
  --warmup 2 \
  --repeat 5 \
  --profile nsys \
  --profile ncu \
  --ncu-kernel BFS_1 \
  -- ./run
```

语义：

- 用户只通过 wrapper 发起 profiling 请求。
- wrapper 为每个 pass 分配唯一目录，并向运行时注入 `VENTUS_PERF*` 环境变量。
- runtime recorder 不负责决定 experiment / pass 生命周期，只负责把 canonical event 写到 wrapper 指定的 pass 目录。

### 5.2 离线报告入口

示例：

```bash
python3 tools/ventus-perf.py report <experiment-dir>
python3 tools/ventus-perf.py report <pass-dir>
python3 tools/ventus-perf.py report <experiment-dir> --view timeline
python3 tools/ventus-perf.py report <experiment-dir> --view kernel
python3 tools/ventus-perf.py report <experiment-dir> --view profiler
```

语义：

- 当输入是 `experiment-dir` 时，reporter 读取 `experiment.json` 与 `actual_passes` 作为权威索引。
- 当输入是单个 `pass-dir` 时，该目录也必须来自 wrapper-managed experiment：
  - 至少包含 wrapper 写出的 `pass.begin.json` 或 `pass.json`
  - 不伪造完整 experiment 生命周期
  - 仍产出该 pass 的 `summary` / `timeline` / `kernel` 视图
  - 报告文件固定写入 `<pass-dir>/reports/`

## 6. 数据模型

推荐的逻辑数据模型：

- `experiment`
- `pass`
- `trace event`
- `artifact`
- `report`

### 6.1 experiment

表示一次 profiling 请求，例如：

- 2 次 warmup
- 5 次 measured
- 1 次 `nsys`
- 1 次 `ncu`

`experiment` 也必须有明确 schema，避免 wrapper、reporter、compare 视图各自发明字段。

建议文件：

- `experiment.begin.json`
  - wrapper 创建 experiment 时立即写出
- `experiment.json`
  - 所有 pass 编排结束后原子写出
  - 是 reporter 认定 experiment 完整性的唯一完成态文件

建议字段至少包含：

- `schema_version`
- `experiment_id`
- `state`
- `finalized`
- `created_at_wall`
- `host_info`
- `wrapper_version`
- `command`
- `cwd`
- `base_env_summary`
- `backend`
- `requested_plan`
  - `warmup_count`
  - `measure_count`
  - `profile_passes`
- `actual_passes`
  - pass 列表，按顺序记录 `pass_id` / `pass_type` / `state`
- `pass_dirs`
- `report_paths`
- `start_mono_ns`
- `end_mono_ns`
- `duration_ns`
- `failure_summary`
- `recorder_errors`

建议状态：

- `running`
- `completed`
- `failed`
- `incomplete`

reporter 规则：

- `experiment.begin.json` 存在但 `experiment.json` 不存在：
  - experiment 视为 `incomplete`
  - reporter 只允许做 best-effort 报告
- 只有 `experiment.json` 且 `finalized=true` 时，才视为完整 experiment
- `actual_passes` 是 wrapper-managed experiment 与 reporter 之间的唯一权威 pass 索引，不允许各组件自行扫描目录推断 pass 顺序

### 6.2 pass

表示一次真实进程执行。

支持的 pass 类型：

- `warmup`
- `measure`
- `nsys`
- `ncu`

每个 `pass` 必须有明确生命周期与完成态，避免 reporter 无法区分“正常无事件”和“异常中断导致日志截断”。

建议状态：

- `running`
- `completed`
- `failed`
- `signaled`
- `incomplete`

建议文件：

- `pass.begin.json`
  - 在子进程成功 spawn 并拿到 `pid` 后由 wrapper 立即写出
  - 记录 pass 的初始上下文
- `pass.json`
  - 结束时由 wrapper 原子写出
  - 是 reporter 认定 pass 完整性的唯一完成态文件

补充约束：

- 所有受支持的 pass 都属于 wrapper-managed experiment。
- runtime recorder 不写 `pass.begin.json` / `pass.json` 这两个权威 manifest。
- wrapper-managed pass 的权威完成态始终由 wrapper 写入，不依赖进程内 teardown。
- 如果 wrapper 在 spawn 前就失败，允许直接写终态 `pass.json`：
  - `state=failed`
  - `finalized=true`
  - `pid` 留空或省略
  - `pass.begin.json` 可以不存在
- 因此 `pid` 对“成功 spawn 过的 pass”是必填，对“spawn 前失败”的终态记录不是强制字段。
- wrapper 必须在 spawn 前就准备好：
  - `experiment_id`
  - `pass_id`
  - `pass_type`
  - `VENTUS_PERF_OUT_DIR`
- 如果 runtime 在 `VENTUS_PERF=1` 下缺少这些 wrapper 注入的关键字段，初始化必须显式失败，而不是偷偷退回“无 experiment / 无 manifest”的临时模式。

建议 `pass.begin.json` / `pass.json` 字段至少包含：

- `schema_version`
- `experiment_id`
- `pass_id`
- `pass_type`
- `profiler_mode`
- `profile_target`
- `state`
- `finalized`
- `pid`
- `parent_pid`
- `argv`
- `cwd`
- `env_summary`
- `clock_source`
- `start_mono_ns`
- `end_mono_ns`
- `duration_ns`
- `exit_code`
- `term_signal`
- `stdout_path`
- `stderr_path`
- `event_files`
- `event_counts`
- `artifact_paths`
- `recorder_errors`

其中：

- `concurrency_detected` 不属于 pass lifecycle 的权威事实字段。
- 如需缓存，可作为 reporter 生成的派生摘要附加到 `pass.json` 或单独 report 文件中，但权威判断必须来自离线 report 重新计算。

其中 `profile_target` 用于 profiler pass 的目标声明，建议包含：

- `target_mode`
  - `none`
  - `kernel_name`
  - `kernel_signature`
  - `reference_invocation`
- `kernel_name`
- `kernel_signature_hash`
- `reference_pass_id`
- `reference_launch_seq`
- `reference_kernel_occurrence`
- `target_resolved`

约束：

- `profile_target` 是“profiler 打算观察谁”的声明，不等于自动保证对齐成功
- 只有当 `target_resolved=true` 且 replay 校验通过时，report 才能展示精确的 invocation 级 supplement

reporter 规则：

- `pass.begin.json` 存在但 `pass.json` 不存在：
  - 该 pass 视为 `incomplete`
  - 相关 JSONL 视为可能截断
- 只有 `pass.json` 且 `finalized=true` 时，才视为完整完成态
- `state=signaled` 或 `state=failed` 的 pass 仍可保留部分事件，reporter 应展示但不得与正常 measured pass 混为一谈
- 单 pass 报告输入也必须来自 wrapper-managed pass 目录：
  - `pass.json` + `events.*.jsonl`
  - 或 `pass.begin.json` + `events.*.jsonl`
- 若缺少权威 manifest，只能视为 wrapper-managed pass 的不完整目录，而不是另一种独立运行模式

### 6.3 trace event

表示原始事实事件，按时间线记录，允许嵌套。

建议字段：

- `schema_version`
- `experiment_id`
- `pass_id`
- `event_id`
- `parent_event_id`
- `scope_id`
- `pid`
- `tid`
- `queue_id`
- `stream`
- `event_type`
- `clock_source`
- `ts_start_ns`
- `ts_end_ns`
- `duration_ns`
- `backend`
- `kernel_name`
- `launch_seq`
- `kernel_occurrence`
- `kernel_signature_hash`
- `task_id`
- `kernel_id`
- `correlation_id`
- `status`
- `attrs`

其中：

- `event_id`
  - 在单个 pass 内唯一
  - 必须视为 opaque id，reporter 不得依赖字符串解析其来源
  - 必须包含 producer/process 实例身份，不能只是 `<stream>-<pid>-<seq>` 这类会在多 DSO / 多子进程下碰撞的局部计数
  - 建议格式为 `<stream>-<pid>-<producer_instance>-evt-<seq>`
- `parent_event_id`
  - 显式表示嵌套关系；无父事件时为空
- `scope_id`
  - 用于把一组语义相关事件绑定到同一个逻辑 scope
  - 同样必须视为 opaque id，并携带 producer/process 实例身份
  - 建议格式为 `<stream>-<pid>-<producer_instance>-scope-<seq>` 或显式传播得到的上游 scope id
- `queue_id`
  - 可选；用于保留未来多 command queue 事实
- `stream` 用于区分来源，例如：
  - `pocl`
  - `vt`
  - `sbt_ptx`
  - `cuda`
  - `sim`
- `clock_source`
  - 首版统一要求为 Linux 同机的 `CLOCK_MONOTONIC`
- `kernel_occurrence`
  - 同一 `kernel_name` 在当前 pass 内第几次出现
- `kernel_signature_hash`
  - 基于 `kernel_name + grid + block + shared_mem + elf/module hash` 计算的观测签名
- `attrs` 承载扩展字段：
  - bytes
  - grid/block
  - shared_mem
  - ELF 路径
  - return code
  - cache hit/miss
  - simulator steps / cycles 等

### 6.4 artifact

表示某个 pass 的外部文件，例如：

- stdout/stderr
- `nsys` 报告
- `ncu` 报告
- profiler 摘要 JSON

### 6.5 report

表示从 experiment 的多个 pass 中离线生成的人类视图与机器可读摘要。

建议至少包含：

- `best_effort`
- `concurrency_detected`
- `measured_pass_count`
- `input_kind`
  - `experiment`
  - `pass`
- `summary`
  - `wall_time_ns`
  - `top_level_total_ns`
  - `buckets`
  - `sub_buckets`
- `timeline`
- `kernels`

约束：

- `concurrency_detected` 的权威值由 reporter 根据 canonical event 重新计算。
- `sub_buckets` 是顶层 bucket 的 drill-down，不得与顶层总和重复累计。
- 当输入是单个 `pass` 时，报告文件写入 `<pass-dir>/reports/`。

## 7. 目录结构

推荐 experiment 目录结构如下：

```text
build/ventus-perf/<experiment-id>/
  experiment.json
  command.sh
  passes/
    warmup-0001/
      pass.begin.json
      pass.json
      events.pocl.jsonl
      events.vt.jsonl
      events.sbt_ptx.jsonl   # optional; Phase 1 may omit this when only parent-side SBT wall time is measured
      events.cuda.jsonl   # optional, only when a CUDA API shim is explicitly enabled
      events.sim.jsonl
      stdout.log
      stderr.log
    measure-0001/
      ...
    measure-0002/
      ...
    nsys-0001/
      ...
      artifacts/nsys/
        report.nsys-rep
        summary.json
    ncu-0001/
      ...
      artifacts/ncu/
        report.ncu-rep
        summary.json
  reports/
    summary.txt
    summary.json
    timeline.json
    kernels.json
    profiler.json   # optional, only when profiler passes are present
```

### 7.1 `VENTUS_PERF_OUT_DIR`

`VENTUS_PERF_OUT_DIR` 定义为“当前 pass 输出目录”。

不定义为：

- 单个日志文件名
- experiment 根目录

理由：

- 一次 pass 会产出多路日志与 artifact
- 多次复测需要天然隔离，避免覆盖
- wrapper 可以用同一个接口驱动所有 runtime producer

补充约束：

- `VENTUS_PERF_OUT_DIR` 由 wrapper 分配并注入。
- 不再支持“用户手工指定若干环境变量后直接运行程序”作为文档承诺的使用方式。

### 7.2 覆盖策略

- 如果 `VENTUS_PERF_OUT_DIR` 已存在且非空，默认报错，不覆盖。
- wrapper 模式始终分配唯一目录。

## 8. 事件关联机制

### 8.1 同进程语义关联

在 PoCL 语义层建立 scope，例如：

- `kernel_arg_pack`
- `kernel_arg_upload`
- `kernel_metadata_upload`
- `kernel_elf_upload`
- `kernel_submit`
- `buffer_write`
- `buffer_read`

当调用落到 `vt_*` 层时，需要尽可能保留上层语义关联。

建议实现：

- 线程局部上下文
- RAII scope guard
- 显式上下文快照传播
- sideband perf context API

避免事后通过时间猜测上层语义。

补充要求：

- 线程局部上下文只能解决“当前线程的语义 scope 继承”
- 如果 PoCL 与 driver 位于不同已链接模块，Phase 1 不得假设它们天然共享同一份 `thread_local` scope 栈
- 跨模块调用边界必须传播正式上下文字段，至少包括：
  - `launch_seq`
  - `kernel_name`
  - `kernel_occurrence`
  - `kernel_signature_hash`
  - 可选 `scope_id` / `parent_event_id`
- 推荐通过独立于 `vt_*` 主调用签名的 sideband API 传播，而不是挤占 `taskID` / `kernelID` 语义槽位
- reporter 不得假设所有关键事件都来自单一线程
- 如未来出现多 host thread / 多 queue，事件层级必须依赖 `event_id` / `parent_event_id` / `scope_id`，而不是只依赖时间邻近

### 8.2 kernel invocation 关联

每个 pass 内为每次 kernel launch 分配单调递增的 `launch_seq`：

- 第一次 launch：`launch_seq=1`
- 第二次 launch：`launch_seq=2`

所有与该次 invocation 相关的事件都携带 `launch_seq`。

重要约束：

- `launch_seq` 只表示“单个 pass 内的局部执行顺序”
- `launch_seq` 不是跨 pass 稳定主键
- reporter 只能用 `launch_seq` 做单 pass 内的事件拼装，不能把它当作 baseline pass 与 profiler pass 之间的精确 invocation 对齐依据

为支持更稳健的聚合，必须记录：

- `kernel_occurrence`
- `kernel_signature_hash`

其中：

- `kernel_occurrence` 仍是本地序号，不承诺跨 pass 稳定
- `kernel_signature_hash` 仅用于聚合与相似性判断，不承诺唯一对应单次 invocation
- 这两个字段属于 trace event 的正式顶层 schema，不允许不同 producer 有的写顶层、有的塞进 `attrs`

### 8.3 跨进程关联

`sbt_ptx` 当前是子进程，因此需要显式传递 perf 上下文。

建议由主进程向子进程传入：

- `VENTUS_PERF_EXPERIMENT_ID`
- `VENTUS_PERF_PASS_ID`
- `VENTUS_PERF_OUT_DIR`
- `VENTUS_PERF_BACKEND`
- `VENTUS_PERF_LAUNCH_SEQ`
- `VENTUS_PERF_PARENT_SCOPE`
- `VENTUS_PERF_PARENT_EVENT_ID`

并统一使用单调时钟纳秒时间戳，以便离线拼接多进程时间线。

这里必须再加一层约束，避免把“单进程 steady_clock duration”和“跨进程绝对时间戳”混用：

- 所有 trace event 写盘时都必须是 `CLOCK_MONOTONIC` 的绝对纳秒时间戳
- `steady_clock` 或其他局部 duration 只能作为实现细节，写盘前必须转为 canonical timestamp
- 用于人类阅读的 wall-clock 时间只能放在 pass manifest 中，不能参与事件时间线拼接

父子进程必须各自产出 anchor event：

- 父进程：
  - `child_process_spawn`
- 子进程：
  - `process_bootstrap`

两者通过：

- `child_pid`
- `parent_pid`
- `parent_event_id`
- 继承的 perf 上下文

做显式连接，而不是仅靠时间相近猜测

### 8.4 `nsys/ncu` 对齐

reporter 优先使用以下键做“聚合相关性”判断，而不是默认承诺精确 invocation 对齐：

- `kernel_name`
- `kernel_signature_hash`
- `grid`
- `block`
- `shared_mem`
- `backend`
- 模块或 ELF hash

报告规则必须更保守：

- 默认只保证按 `kernel_name + kernel_signature_hash` 聚合展示 profiler 结果
- 只有当 profiler pass 明确声明了 `profile_target`，且 replay 后的目标匹配通过时，才允许展示“单次 invocation 对齐”
- 如果目标匹配失败，report 中必须明确标记为 `aggregate_only`，不能给出看似精确的错配 invocation 对照

### 8.5 并发语义与当前保证范围

首版设计必须明确建立在当前代码路径的同步语义之上，而不是假设未来天然成立。

当前可观察事实包括：

- PoCL Ventus 路径基本是 `vt_start()` 后立即 `vt_ready_wait()`
- PTX backend 的 JIT 路径被 `PtxDevice::mu` 串行化
- `vt_ready_wait()` 在 PTX backend 中本质是整个 context 的 `cuCtxSynchronize()`

因此首版 reporter 的闭合归因保证范围定义为：

- 单进程
- 当前 Ventus PoCL 设备实现
- 无显式多 command queue 并行 kernel
- 无多个稳定可观察的 in-flight kernel

一旦 reporter 检测到以下任意情况：

- 不同线程上的重叠 `kernel_prepare` / `kernel_exec_wait`
- 同一设备上重叠的多个 kernel wait window
- 明显的多 queue 并发提交

则 reporter 必须：

- 设置 `concurrency_detected=true`
- 禁止输出“严格闭合”的整次 pass attribution 结论
- 改为输出保守模式 summary：
  - per-thread
  - per-scope
  - per-kernel aggregate
  - 更大的 `uncategorized`

## 9. 归因模型

必须明确区分：

- `trace events`
- `attribution spans`

### 9.1 trace events

特点：

- 原始事实源
- 允许嵌套
- 用于 timeline 与离线再加工

### 9.2 attribution spans

特点：

- 由 report 层生成
- 用于顶层 wall attribution
- 同一层级要求互斥

### 9.3 顶层归因桶

建议固定为：

- `host_overhead`
- `h2d`
- `kernel_prepare`
- `kernel_exec_wait`
- `d2h`
- `teardown`
- `uncategorized`

含义：

- `host_overhead`
  - 用户态或驱动态非传输、非执行等待的开销
- `h2d`
  - 所有 host -> device 拷贝
- `kernel_prepare`
  - 与具体 kernel invocation 绑定的提交前准备
- `kernel_exec_wait`
  - 等待 kernel 执行完成
- `d2h`
  - 所有 device -> host 拷贝
- `teardown`
  - 运行末尾的收尾
- `uncategorized`
  - 无法归类或存在时间洞的部分

### 9.4 二级细分

`kernel_prepare` 可进一步细分为：

- `ventus_elf_to_ptx`
- `cuda_module_jit_load`
- `cuda_function_lookup`
- `cuda_launch_call`

`kernel_exec_wait` 可进一步细分为：

- `simulator_wait`
- `nvidia_wait`

对于 `ptx` / `sbtsim` 的 measured pass，Phase 1 的 machine-readable summary 也必须显式暴露这些 drill-down：

- `summary.sub_buckets.kernel_prepare`
- `summary.sub_buckets.kernel_exec_wait`

避免出现“埋了细粒度事件但 report 契约没有对应出口”的情况。

### 9.4A Phase 1 归因映射规则

为避免不同实现对 `kernel_prepare` / `kernel_exec_wait` 各自脑补，Phase 1 必须固定以下口径：

- reporter 先按 `launch_seq` 聚合同一 invocation 的候选事件；只有缺少 `launch_seq` 时，才允许退回到显式传播的 `scope_id` / `parent_event_id` 做保守拼装
- 顶层 attribution 只对互斥 span 求和，不直接对原始嵌套事件求和
- `h2d` 的原始候选事件包括：
  - `buffer_write`
  - `buffer_copy` 中明确 host -> device 的部分
  - `vt_copy_to_dev`
  - `cuMemcpyHtoD`
- `d2h` 的原始候选事件包括：
  - `buffer_read`
  - `vt_copy_from_dev`
  - `cuMemcpyDtoH`
- `kernel_prepare` 的原始候选事件包括：
  - `kernel_arg_pack`
  - `kernel_arg_upload`
  - `kernel_elf_upload`
  - `kernel_metadata_upload`
  - `kernel_submit`
  - `vt_start`
  - `generate_ptx_via_sbt`
  - `read_generated_ptx`
  - `cuModuleLoadDataEx`
  - `cuModuleGetFunction`
  - `cuLaunchKernel`
- `kernel_exec_wait` 的原始候选事件包括：
  - `kernel_wait`
  - `vt_ready_wait`
  - `cuCtxSynchronize`
- `teardown` 只承载明确发生在 pass 尾部、且不属于上面各桶的收尾事件；其余未覆盖时间进入 `uncategorized`

Phase 1 的 span 生成规则：

- 单次 invocation 的 `kernel_prepare` span 定义为：同一 `launch_seq` 下所有 prepare 候选事件的并集，且必须截断在对应 wait span 之前
- 单次 invocation 的 `kernel_exec_wait` span 定义为：同一 `launch_seq` 下 wait 候选事件的并集
- 当 `vt_ready_wait` 与 `cuCtxSynchronize` 同时存在时：
  - 顶层 `kernel_exec_wait` 只计一次互斥 wait span
  - `cuCtxSynchronize` 作为 `summary.sub_buckets.kernel_exec_wait` 的 drill-down 事实，不得再重复累计到顶层总和
- 当 `vt_start` 与更细粒度的 `generate_ptx_via_sbt` / `cuModuleLoadDataEx` / `cuLaunchKernel` 同时存在时：
  - 顶层 `kernel_prepare` 只计一次互斥 prepare span
  - 更细事件只用于 `summary.sub_buckets.kernel_prepare`

并发检测规则也必须绑定到上述派生 span，而不是直接拿所有原始事件做名字匹配：

- `concurrency_detected=true` 的判断基于派生后的 `kernel_prepare` / `kernel_exec_wait` span 是否发生重叠
- 若原始事件不足以稳定派生这些 span，reporter 必须进入保守模式并扩大 `uncategorized`

### 9.5 归因闭合原则

- 顶层桶的总和尽量逼近 wall time
- 嵌套子事件只用于 drill-down，不重复计入总和

## 10. 各层 producer 与埋点边界

### 10.1 PoCL 语义层

位置：[`pocl/lib/CL/devices/ventus/pocl_ventus.cc`](../../../pocl/lib/CL/devices/ventus/pocl_ventus.cc)

建议埋点：

- `mem_obj_alloc`
- `buffer_write`
- `buffer_read`
- `buffer_copy`
- `buffer_fill`
- `map_mem`
- `unmap_mem`
- `kernel_arg_pack`
- `kernel_arg_upload`
- `kernel_elf_upload`
- `kernel_pds_alloc`
- `kernel_metadata_upload`
- `kernel_submit`
- `kernel_wait`

作用：

- 提供高层、面向 OpenCL 语义的可读标签

### 10.2 Driver `vt_*` ABI 层

位置：

- [`driver/driver/auto_select/ventus.cpp`](../../../driver/driver/auto_select/ventus.cpp)
- [`driver/driver/ptx_device/ventus.cpp`](../../../driver/driver/ptx_device/ventus.cpp)
- [`driver/driver/cyclesim_device/ventus.cpp`](../../../driver/driver/cyclesim_device/ventus.cpp)
- [`driver/driver/rtlsim_device/ventus.cpp`](../../../driver/driver/rtlsim_device/ventus.cpp)
- [`driver/driver/spike_device/ventus.cpp`](../../../driver/driver/spike_device/ventus.cpp)

统一埋点：

- `vt_buf_alloc`
- `vt_buf_free`
- `vt_copy_to_dev`
- `vt_copy_from_dev`
- `vt_upload_kernel_file`
- `vt_start`
- `vt_ready_wait`

作用：

- 提供跨 backend 的统一事件基线
- 对于经 `auto_select` 动态转发的调用链，还负责保证 sideband perf context API 不被中途丢失

### 10.3 `ptx` / `sbtsim` 内部细分

#### `sbt_ptx`

位置：[`sbtsim/tools/sbt_ptx.cpp`](../../../sbtsim/tools/sbt_ptx.cpp)

完整设计下可细化为：

- `cache_lookup`
- `elf_read`
- `symtab_read`
- `decode_cfg_verify`
- `callgraph_collect`
- `emit_ptx`
- `write_ptx`
- `total`

兼容 / 迁移要求：

- 现有 `GPU_SBT_PTX_PROFILE_LOG` 产物视为 legacy 通道
- 新系统的 canonical 事实源必须是 `events.sbt_ptx.jsonl`
- 在迁移阶段可以：
  - 由 `sbt_ptx` 直接同时写 canonical event 与 legacy JSONL
  - 或由 wrapper / adapter 将 legacy JSONL 转换为 canonical event
- 但 reporter 只能消费 canonical event，不能同时混吃两套来源

Phase 1 收敛约束：

- Phase 1 不要求深入改造 `sbt_ptx` 内部源码来产出这些细粒度事件
- Phase 1 只要求父进程在 `ptx_device` 侧把整个子进程转译耗时记录为一个 `generate_ptx_via_sbt` span
- 因此 Phase 1 的 correctness 不依赖 `events.sbt_ptx.jsonl`

#### `ptx_device`

位置：[`driver/driver/ptx_device/ventus.cpp`](../../../driver/driver/ptx_device/ventus.cpp)

建议细化为：

- `jit_cache_hit`
- `jit_cache_miss`
- `generate_ptx_via_sbt`
- `read_generated_ptx`
- `cuModuleLoadDataEx`
- `cuModuleGetFunction`
- `cuLaunchKernel`
- `cuCtxSynchronize`

#### CUDA Driver API

位置：

- [`driver/driver/ptx_device/ventus.cpp`](../../../driver/driver/ptx_device/ventus.cpp)
- [`sbtsim/tools/cuda_trace.cpp`](../../../sbtsim/tools/cuda_trace.cpp)

建议记录：

- `cuMemcpyHtoD`
- `cuMemcpyDtoH`
- `cuMemAlloc`
- `cuMemFree`
- `cuMemsetD8`
- `cuModuleLoadDataEx`
- `cuLaunchKernel`
- `cuCtxSynchronize`

口径要求：

- Phase 1 的 canonical CUDA-facing facts 以 `ptx_device` 直接产出的 `cu*` 事件为主，不要求为了补齐同名事件而强制引入 `cuda_trace` shim
- 如果未来显式启用 `cuda_trace`，则必须保证同一事实只保留一套 canonical 来源，不能让 `ptx_device` 直写事件与 shim 事件在 reporter 里双重入账
- 因此同一 pass 中，`cuModuleLoadDataEx` / `cuLaunchKernel` / `cuCtxSynchronize` 这类事实要么来自 driver 直接埋点，要么来自 shim 适配后的 canonical 事件，不允许两套来源同时作为权威事实参与求和

兼容 / 迁移要求：

- 现有 `cuda_trace` 的 stderr summary 仅作为 debug / 旁路摘要
- canonical 事实源必须是结构化事件文件，而不是 destructor 时打印的一行 summary
- profiler pass 中是否启用 `cuda_trace` shim，必须由 wrapper 按 pass 类型显式决定，不能隐式继承

### 10.4 `cyclesim` / `rtlsim`

#### `cyclesim`

位置：[`driver/driver/cyclesim_device/ventus.cpp`](../../../driver/driver/cyclesim_device/ventus.cpp)

建议记录：

- `vmemcpy_h2d`
- `vmemcpy_d2h`
- `add_kernel`
- `ready_wait_loop`
- `sim_time_ns`
- `step_count`

#### `rtlsim`

位置：[`driver/driver/rtlsim_device/ventus.cpp`](../../../driver/driver/rtlsim_device/ventus.cpp)

建议记录：

- `pmemcpy_h2d`
- `pmemcpy_d2h`
- `add_kernel`
- `ready_wait_loop`
- `flush_tail_steps`
- `sim_time`
- `step_count`

### 10.5 `spike`

位置：[`driver/driver/spike_device/ventus.cpp`](../../../driver/driver/spike_device/ventus.cpp)

建议记录：

- `copy_to_dev`
- `copy_from_dev`
- `run_total`

限制说明：

- `spike` 路径中 `vt_start` 同步执行、`vt_ready_wait` 基本为空，因此不一定能自然拆出独立的 `kernel_exec_wait`。
- 这应在报告中作为 backend 语义差异明确标注。

### 10.6 旧 profiling 通道与 canonical recorder 的关系

必须明确“只有一套权威事实源”，避免做出第二套平行系统。

定义如下：

- canonical facts：
  - `events.*.jsonl`
  - `pass.begin.json`
  - `pass.json`
- legacy / compatibility outputs：
  - `GPU_SBT_PTX_PROFILE_LOG`
  - `cuda_trace` 的 stderr summary
  - 旧脚本自有的 wall-time JSON

迁移规则：

- Phase 1 起，新增 recorder schema 是唯一权威事实源
- 旧输出可以暂时保留，供现有脚本兼容
- 如果旧输出与 canonical facts 数字不一致，reporter 一律以 canonical facts 为准
- 所有旧通道最终都要么：
  - 直接改为产生 canonical event
  - 要么在 wrapper/reporter 前被适配转换

### 10.7 `LD_PRELOAD` 与动态 backend loader 组合规则

wrapper 负责注入 `LD_PRELOAD` 时，必须定义稳定组合规则。

建议规则：

- 保留用户已有 `LD_PRELOAD`
- wrapper 注入的 perf shim 放在前面：
  - `LD_PRELOAD=<required_perf_shims>:<user_existing_ld_preload>`
- 将最终组合结果记录到 `pass.begin.json`

原因：

- 需要确保 recorder 所需 shim 优先拦截
- 同时不能静默吞掉用户已有 preload

针对不同 pass 的规则：

- baseline measured pass
  - 可按实现需要启用 perf 所需 shim，例如 `cuda_trace`
  - 但 Phase 1 不以“必须启用 `cuda_trace`”作为 correctness 前提
- `nsys` / `ncu` pass
  - 默认不额外挂 `cuda_trace` 这类 API 拦截 shim，避免双重拦截与额外扰动
  - 仍保留内部 recorder 与显式事件埋点
  - 如需组合模式，必须由用户显式开启，并在 report 中标明“combined_profiler_mode”

注意：

- auto-select backend 通过 `dlopen()` 动态加载 backend
- 因此 wrapper 不能假设简单的 `LD_PRELOAD` 总能覆盖所有路径
- 任何依赖 shim 的 producer 都必须在 spec 与实现中明确其加载前提与降级行为

## 11. `nsys` / `ncu` 策略

### 11.1 总原则

- `nsys` / `ncu` 只在 `ptx` / `sbtsim` backend 启用。
- 作为独立 profiler pass 运行，不与 baseline measured pass 混在同一次执行中。
- 进入同一个 experiment / report，但不直接参与 baseline wall attribution 求和。

### 11.2 `nsys`

定位：

- 默认的外部 profiler pass
- 用于整体时间线、CUDA API、kernel 与 memcpy 级补充分析

建议：

- 对 `ptx` backend 提供一等支持
- 不作为 `tools/ventus-perf.py run -- ./run` 的默认隐式 pass
- 只有在用户显式请求 `--profile nsys` 时才运行

### 11.3 `ncu`

定位：

- 可选的 targeted deep dive
- 用于单 kernel 的 GPU 微架构指标分析

建议：

- 默认关闭
- 允许只针对特定 kernel 或显式 `profile_target` 启用

推荐 target 语义：

- 默认按 `kernel_name` 选目标
- 如果用户是从 baseline report 中选定某次 invocation，再由 wrapper 把该选择编码成 `profile_target`
- `ncu` pass 执行完成后，reporter 必须再次验证该目标是否解析成功；失败则退回聚合展示

### 11.4 同 report 呈现方式

report 中分开两个语义区：

1. `Baseline Attribution`
2. `Profiler Supplement`

明确标注：

- attribution 来自 measured passes
- `nsys/ncu` 指标来自独立 profiler pass
- 除非 profiler pass 明确声明并验证了 `profile_target`，否则默认只展示聚合级别的 kernel supplement，不做单次 invocation 精确对齐

## 12. 报告视图

### 12.1 默认 summary

默认 summary 应同时：

- 写入 `reports/summary.txt`
- 打印到 stdout

summary 应尽量短，但保证最有用。对于 baseline-only experiment，至少包含：

- experiment 元信息
- command
- backend
- measured pass 数量
- wall time 均值 / 最小 / 最大 / 标准差
- 顶层 attribution 桶
- kernel invocation 摘要

如果 experiment 中包含 profiler pass，则额外包含：

- profiler supplement 摘要

### 12.2 `summary` 视图

面向第一次查看结果的用户，包含：

- experiment 概览
- 顶层 attribution
- 多次 measured 的统计

### 12.3 `timeline` 视图

按事件发生顺序列出完整事件流，例如：

- `memcpy_h2d`
- `kernel_prepare`
- `kernel_launch`
- `kernel_wait`
- `memcpy_d2h`

满足“看到每一笔事件”的需求。

### 12.4 `kernel` 视图

按 invocation 或按 kernel 名汇总：

- prepare
- launch
- wait
- invocation 次数

### 12.5 `profiler` 视图

专门展示 `nsys` / `ncu` 补充分析：

- `nsys` top kernels by GPU time
- `ncu` 的目标 kernel 摘要指标

## 13. 输出格式

首版建议同时产出：

### 面向人

- `reports/summary.txt`

### 面向机器

- `reports/summary.json`
- `reports/timeline.json`
- `reports/kernels.json`
- `reports/profiler.json`（仅当 experiment 中存在 profiler pass 时必需）

理由：

- 人眼先看 `summary.txt`
- 自动化分析、CI、回归平台直接消费 JSON

## 14. 推荐默认行为

### 14.1 wrapper 编排

```bash
python3 tools/ventus-perf.py run -- ./run
```

默认建议：

- `warmup=1`
- `repeat=3`
- 默认不附带任何 profiler pass
- 非 `ptx` backend：不支持 `nsys/ncu`
- `ptx` backend：允许显式开启 `nsys`
- `ncu` 默认关闭，需显式启用

推荐两条分离的用户路径：

- baseline attribution 默认路径：

```bash
python3 tools/ventus-perf.py run -- ./run
```

- PTX backend 的 profiler supplement 路径：

```bash
python3 tools/ventus-perf.py run --profile nsys -- ./run
```

### 14.2 离线查看

```bash
python3 tools/ventus-perf.py report <experiment-dir>
```

默认输出 `summary` 视图。

## 15. 分阶段落地建议

### Phase 1：基础 recorder + baseline report

范围：

- 统一 runtime recorder
- wrapper 注入的 `VENTUS_PERF*` runtime contract
- PoCL 层、`vt_*` 层、`ptx_device` 的关键事件
- 基础 wrapper
- summary / timeline / kernel report

目标：

- 在不引入外部 profiler 的情况下，完成 baseline wall attribution
- 对 `ptx` 路径，Phase 1 以 `ptx_device` 直写的 `cu*` 事件兑现 launch / sync / JIT 细分；`sbt_ptx` 内部细粒度拆分、`cuda_trace` shim 与 `profiler.json` 进入后续 phase

### Phase 2：simulator 特有细分与多 backend 对齐

范围：

- `cyclesim` / `rtlsim` 的 sim time / step 等指标
- `spike` 的限制说明与最小对齐

目标：

- 提供跨 backend 的一致 report 语义

### Phase 3：`nsys` / `ncu` 集成

范围：

- profiler pass 编排
- profiler artifact 摘要提取
- 与 baseline report 的综合展示

目标：

- 在同一个 report 中展示 baseline attribution 与 profiler supplement

### Phase 4：格式与平台扩展

范围：

- protobuf exporter/importer
- CI / 回归平台接入
- 更丰富的 compare 视图

## 16. 风险与注意事项

### 16.1 时间源统一

必须统一采用同机 Linux `CLOCK_MONOTONIC` 绝对纳秒时间戳，以保证多线程、多进程事件可对齐。

`system_clock`、本地 `steady_clock duration` 与人类可读 wall-clock 时间不得直接混入 canonical event timeline。

### 16.2 双计时风险

绝对不能将嵌套 trace event 直接生加总用于 attribution。

### 16.3 `nsys/ncu` 干扰

- 它们只能作为独立 pass
- 不可混入 measured baseline

### 16.4 旧日志并存风险

如果不先钉死 canonical recorder 与 legacy 输出的关系，很容易出现两套数字并存且互相矛盾。

因此实现必须先解决：

- `sbt_ptx` legacy JSONL
- `cuda_trace` summary
- 现有脚本消费路径

与新 schema 的权责边界

### 16.5 backend 差异

`spike`、`rtlsim`、`cyclesim`、`ptx` 的执行语义并不完全相同，报告中必须清晰反映哪些分项是后端特有，哪些是统一口径。

## 17. 当前设计结论

本设计最终采用以下方案：

- 用户入口：wrapper；`VENTUS_PERF*` 环境变量仅作为内部控制面
- 原始记录：per-pass 目录下的结构化 JSONL / JSON
- 组织模型：`experiment -> pass -> event/artifact -> report`
- 关联方式：显式上下文传播 + local `launch_seq` + parent/child anchor + 保守的 profiler target 规则
- 归因方式：`trace events` 与 `attribution spans` 分离
- profiler 策略：`nsys` 作为显式启用的整体时间线 supplement，`ncu` 作为按需深挖
- 展示方式：文件化报告，默认 summary 同时写文件并打印到 stdout

这套设计优先解决：

- 多 backend 统一归因
- `ptx/sbtsim` 路径的细粒度拆分
- 多次复测与外部 profiler 共存
- “记录”和“展示”解耦

后续实现应在此设计基础上，再输出一份明确到文件与测试命令的 implementation plan。
