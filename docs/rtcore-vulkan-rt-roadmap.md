# Ventus RTcore Vulkan RT Roadmap

## Scope

The research target is a Vulkan ray tracing subset on Ventus plus an RTcore
traversal/intersection case study. The driver may keep graphics pipeline,
render pass, framebuffer, swapchain, draw, and copy paths as compatibility
no-ops when they are only needed to let RT samples start. That boundary must be
documented in experiments: the platform is not claiming complete Vulkan
graphics support.

RT behavior is not a no-op boundary. The following paths must stay real and
observable:

- acceleration-structure input geometry and build metadata
- shader binding table records and instance SBT offsets
- `vkCmdTraceRaysKHR` dispatch dimensions
- raygen, miss, closest-hit, and any-hit shader behavior
- payload ABI, hit attributes, barycentrics, and storage image writes
- traversal/intersection events inside Spike or the RTcore model

## Reference Levels

Level 0 is a minimal smoke suite. It uses tiny deterministic assets and fixed
small resolutions to catch ABI, SBT, payload, image-format, and hit/miss bugs
quickly. The `raytracingshadows` Level 0 asset is
`assets/models/vulkanscene_shadow_minimal.gltf`; images from it are expected to
look like a dark blue background with a flat colored rectangle. They are not the
real shadow demo.

Level 1 is the real sample suite. It uses the restored upstream SaschaWillems
assets, software Vulkan reference images, and recorded run metadata. The
default `assets/models/vulkanscene_shadow.gltf` is the upstream shadow scene and
is the reference asset for correctness discussions.

Use:

```sh
tools/rtcore/reference_suite.sh
```

The script records a manifest, saves PPM images from `RT_SHADOW_SCREENSHOT`, and
converts every generated PPM in its output tree to PNG.

## Experiment Metrics

Each RTcore experiment should report correctness, bottlenecks, and performance,
not only a rendered image. For each frame or dispatch, collect at least:

- rays launched
- primary-ray hit and miss counts
- shadow-ray launched, occluded, and unoccluded counts
- box tests and triangle tests
- closest-hit, miss, and any-hit shader invocations
- early-termination events for shadow rays
- traversal stack high-water mark or spill count when applicable
- SBT record selection counts
- cycles, dynamic instructions, and memory traffic when available

Comparisons should vary one RTcore design parameter at a time, such as stack
layout, ray-box pipeline, ray-triangle pipeline, BVH memory layout, early
termination, or shadow-ray fast path.
