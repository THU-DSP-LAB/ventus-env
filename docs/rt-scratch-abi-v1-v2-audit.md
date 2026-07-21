# Ventus RT scratch ABI V1/V2 审计

状态：`implementation evidence`；不是 Ventus V0.2 target freeze

更新日期：2026-07-21

公共逻辑 ABI、Ventus target mapping 与 scratch V2 compatibility 的规范化关系由
`/home/liuql/projects/RTcore/docs/architecture/ventus/abi/` 拥有。本文只审计当前源码。

## 结论

当前可复现出图链路使用 ABI V2，不建议回退到 V1。V2 与 V1 不是 bit-level
兼容布局，但在当前 `raytracingshadows` 功能范围内保持了相同的 shader 可见结果，
并通过 Spike 语义测试和 160x96 精确图片门禁。

V2 现在还包含执行期标签 `0x56540002`。Mesa runtime 将它放在 dispatch args
offset 148，compiler 在每次 `vt.rt.traverse` 前把它写入 active lane 的 PDS
offset 92；Spike 在整个 warp 的任何 RT 状态发生变化前校验全部 active lane。
因此，新版 runtime、compiler 或 Spike 与不匹配组件混用时会 fail closed，而不会
按错误的 PDS offset 静默执行。

V2 的核心变化不是删除光追语义，而是调整状态所有权：ray、control、candidate、
committed hit、attribute 和 payload 仍由每 lane 的 PDS ABI 承载；暂停 BVH 遍历所需
的 cursor、frontier 和 stack 改由 Spike RTCore 功能模型私有保存。

## 布局差异

| 项目 | ABI V1 | ABI V2 |
| --- | ---: | ---: |
| compile-time version | 1 | 2 |
| compiler private/local reservation | 4096 B | 4096 B |
| RT ABI region | 1008 B | 384 B |
| per-lane total | 5104 B | 4480 B |
| control base | 96 | 112 |
| candidate hit base | 128 | 144 |
| committed hit base | 208 | 224 |
| hit attribute base | 288 | 304 |
| payload base | 320 | 336 |
| execution ABI tag | 无 | PDS offset 92，值 `0x56540002` |
| traversal cursor/stack | PDS 内 640 B continuation 区 | RTCore 模型私有 context |
| compiler CPS header | 无 | PDS offset 96，16 B |

V2 的 16 B CPS header 字段是 `frame_base`、`active_level`、`fragment_id` 和
`flags`。它服务于未来 compiler/runtime continuation frame，不等同于 RTCore 的
BVH 遍历状态。当前 `pipeline->continuation_frame_size` 没有非零 producer，因此
driver 不分配有效 CPS arena，Spike 也不消费 header。

完整字段和 byte offset 见
`mesa/src/ventus/RT_SBT_MEMORY_ABI.md` 与
`mesa/src/ventus/compiler/vt_rt_abi.h`。

## 已验证语义

- `vt.rt.traverse` 和 `vt.rt.release` 仍按 warp 发射，并按 SIMT active mask 对
  active lane 执行。
- candidate 可以被 any-hit/intersection 路径接受、忽略或终止；接受并终止时结果
  保留为 hit，而不是被无条件覆盖成 terminated。
- release 和 simulator reset 会回收 RTCore 私有 context。
- runtime、compiler 和 Spike 通过 exact V2 tag 建立执行期握手；tag 缺失或不匹配
  会在整 warp preflight 阶段被拒绝，且不增加计数、不创建 context、不写 lane 结果。
- triangle list、TLAS/BLAS triangle、procedural AABB、closest candidate、hit/miss
  和多 lane mask 有独立语义测试。
- 完整 Vulkan 应用经 Mesa Ventus ICD、生成 ELF、driver、Spike 和 readback 后，
  已有五条 160x96 exact-image gate：triangle + derived shadow、procedural
  AABB/intersection、SBT record data、raygen 顺序 reflections 与 textured any-hit。

验证入口：

```bash
cd spike
python3 tests/ventus_extra_stage2_static.py
g++ -std=c++17 -Wall -Wextra -Werror -Iriscv \
  tests/ventus_rt_semantics.cc -o /tmp/ventus_rt_semantics
/tmp/ventus_rt_semantics
cd ..

mesa/build-ventus/src/ventus/tests/vt_rt_minimal_loop_test \
  mesa/build-ventus/src/ventus/tests/vt_rt_minimal_raygen.spv

tools/rtcore/verify_compat_p1_image.sh
tools/rtcore/verify_procedural_aabb_image.sh
tools/rtcore/verify_sbt_record_data_image.sh
tools/rtcore/verify_iterative_reflections_image.sh
tools/rtcore/verify_textured_any_hit_image.sh
```

## 尚未闭合的风险

1. CPS arena 只是 carrier 骨架。compiler 尚未计算 frame layout/size，也没有生成
   frame push/pop；这不能作为 nested-ray continuation 已实现的证据。
2. Spike 当前只有一个逻辑 SM0 RTCore 实例，不模拟多 SM placement、容量、周期、
   backpressure 或访存争用。
3. V2 只证明当前 Spike 功能链路；RTL 必须独立实现并验证相同的状态所有权、lane
   identity、release 和 reset 语义。
4. ABI tag 尚未写入 ELF metadata；新版执行链会拒绝旧 shader/runtime，但旧 Spike
   本身不能被追溯强化。
5. 当前 private context 只按 physical PDS address索引，没有统一
   SM/warp/lane/invocation/owner/generation/transaction identity。
6. Resume 仍读取 compatibility control words，没有 target active-mask subset 与
   mask-shrink；release 也没有 terminal lifecycle validation。
7. Control、candidate 与 committed state 仍是 shader-visible compatibility ABI，
   尚未完成公共 handoff 与 software-invisible private authority 的目标分界。
8. Derived shadow 与 iterative reflections 是必须保留的功能证据，但没有实现 fresh
   child state、named return 或通用 continuation。

这些问题不要求回退 ABI。明确的 ABI profile/version handoff 和 fail-closed
检查已经完成；启用非零 CPS frame 前仍须实现并验证 frame layout/save/restore，
不能把当前零值骨架描述成完整 continuation 实现。
