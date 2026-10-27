# Per-trace group traversal

`VENTUS_VK_RT_GROUP_WARPS=1`, `2`, `4`, or `8` selects the workgroup warp count
(default: `1`). Primary rays and candidate resumes use ordinary `traverse`.
A secondary trace with a nonzero active trace level uses explicit group
traversal only when software makes the call collective:

```glsl
#define VENTUS_RT_TRACE_FLAG_GROUP_COLLECTIVE_BIT 0x80000000u
#define ventusGroupTraceRayEXT(accel, flags, cullMask, sbtOffset, sbtStride, miss, origin, tmin, direction, tmax, payload) \
   traceRayEXT(accel, (flags) | VENTUS_RT_TRACE_FLAG_GROUP_COLLECTIVE_BIT, cullMask, sbtOffset, sbtStride, miss, origin, tmin, direction, tmax, payload)
```

This helper is a Ventus-private source-level intrinsic.  Mesa consumes the
marker and emits `group_traverse`; the marker is cleared before standard ray
flags reach RT Local SRAM or RTcore.

The call is valid only after software has made it a full workgroup round: every
member warp must execute the call once.  A member with no live rays still
reaches the instruction with an empty mask.  Do not use it for a closest-hit
child or an independently divergent raygen-loop iteration; use ordinary
`traceRayEXT` there.

This explicit ISA collective is separate from the RTcore's automatic sparse
task batching. Ordinary `traverse` requests can be batched by hardware without
this marker or a software workgroup barrier. See
[candidate spill and hardware task batching](candidate-spill-and-task-batching.md).
