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

## V5 Software Migration

Development for the V5 software contract is isolated on
`feature/rt-v5-software-contract`. The migration keeps the current V3/V4 image
path runnable until all V5 producers and consumers are ready.

Completed foundation:

- Mesa has one shared definition and validator for the 56-byte V3 and 96-byte
  V4 kernel resource records.
- NIR-to-LLVM can attach a structured V5 RT resource trailer to a kernel.
- LLVM object summaries and LLD preserve that trailer and emit the final
  `.ventus.resource.<kernel>` byte image.
- The Mesa runtime accepts existing V3 kernels, validates V4 layout and extent
  fields, and rejects missing or inconsistent RT contracts across pipeline
  entries.

The default RT pipeline does not publish the V5 trailer yet. Activation follows
these implementation slices in order:

1. Replace the V4 scratch layout producer with the frozen 384-byte hot Software
   PDS V5 layout and bounded continuation allocation.
2. Provision the per-CTA shared traversal-private region and expose its stable
   base/offset through launch metadata.
3. Generate the per-lane PDS and 128-byte-aligned private-state addresses, then
   carry both through the NIR intrinsic, LLVM instruction, Spike, and RTL issue
   interfaces.
4. Emit the 68-byte fresh private-state initialization stores, distinguish
   empty from invalid AS roots, and place a visibility `fence` before the hard
   traversal operation.
5. Enable V4 resource publication for V5 kernels and require one matching
   contract across all participating entries.
6. Add terminal release, resident nested-ray reuse, and image regressions before
   retiring the compatibility path.

The dual-source instruction encoding must preserve existing single-source V4
binaries. Its exact compatibility encoding is an ISA decision and must be
settled before slice 3 changes the assembler or decoder.
