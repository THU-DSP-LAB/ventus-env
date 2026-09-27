# Per-trace group traversal

`VENTUS_VK_RT_GROUP_WARPS=2`, `4`, or `8` enables RT Local group traversal.
Primary rays use it automatically.  A secondary round uses it only when
software explicitly makes the call collective:

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
`traceRayEXT` there.  A later compaction/coherence pass may build such a round
and then invoke this helper.
