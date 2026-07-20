# Ventus RT scratch ABI V1/V2 审计

## 结论

当前可复现出图链路使用 ABI V2，不建议回退到 V1。V2 与 V1 不是 bit-level
兼容布局，但在当前 `raytracingshadows` 功能范围内保持了相同的 shader 可见结果，
并通过 Spike 语义测试和 160x96 精确图片门禁。

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
- triangle list、TLAS/BLAS triangle、procedural AABB、closest candidate、hit/miss
  和多 lane mask 有独立语义测试。
- 完整 Vulkan 应用经 Mesa Ventus ICD、生成 ELF、driver、Spike 和 readback 后，
  160x96 PPM 的 SHA-256 为
  `f43328945bdeb0dda69b3cc5612212170e5596456450184acfa627db8d5d1212`。

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
```

## 尚未闭合的风险

1. `VT_RT_ABI_VERSION` 仍是构建期常量，dispatch args 和 ELF metadata 没有运行时
   版本字段，因此混用 V1/V2 component 时不会 fail-closed 拒绝。
2. CPS arena 只是 carrier 骨架。compiler 尚未计算 frame layout/size，也没有生成
   frame push/pop；这不能作为 nested-ray continuation 已实现的证据。
3. Spike 当前只有一个逻辑 SM0 RTCore 实例，不模拟多 SM placement、容量、周期、
   backpressure 或访存争用。
4. V2 只证明当前 Spike 功能链路；RTL 必须独立实现并验证相同的状态所有权、lane
   identity、release 和 reset 语义。

这些问题不要求回退 ABI。后续应先增加明确的 ABI profile/version handoff 和
fail-closed 检查，再启用非零 CPS frame；不能把当前零值骨架描述成完整 continuation
实现。
