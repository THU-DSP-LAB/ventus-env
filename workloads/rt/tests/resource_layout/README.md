# Vulkan RT resource layout and multi-geometry probe

Run from the repository root:

```sh
python3 workloads/rt/tests/resource_layout/run.py
python3 workloads/rt/tests/resource_layout/run.py --backend rtl --rtl-variant basic-YOUR_VARIANT
```

The first command compiles one set of SPIR-V files and runs the same program
against lavapipe and canonical `mesa/build-ventus` with Spike. The RTL command
uses an **existing** isolated runtime and its matching driver/crt0; it does not
rebuild hardware or change `install/`. Replace `basic-YOUR_VARIANT` with the
suffix of your existing `install-rtcore-rtl-<suffix>` and matching
`driver/build-rtcore-rtl-<suffix>` directories. This probe's RTL path requires
a basic-PMU build; it is separate from the five-case image runner.
`--variant` may be repeated to select
`single`, `rebind`, `aabb`, `aabb-rebind`, or the diagnostic `aabb-onegeometry`.
The default four variants exclude the diagnostic control.

Requirements: system C++ compiler/Vulkan loader/glslangValidator, existing Mesa,
LLVM and driver builds. Results, complete renderer logs, SPIR-V hashes and
selected binary hashes are saved under `artifacts/rtcore-binding/<timestamp>/`.
The test exits nonzero on a process failure or any unexpected image/buffer value.

## What it verifies

- Four descriptor sets with bindings 7, 41, 73, 91 and 125, including an
  intentionally unsorted layout. API binding numbers are not device slot indices.
- Storage image, UBO and SSBO arrays; SSBO descriptor copies to another binding;
  dynamically selected image/SSBO array elements.
- Nonzero UBO dynamic offsets, binding a higher set before lower sets, and
  recorded dispatch snapshots when rebinding offsets/push constants between
  two traces in the same command buffer.
- Multiple buffer descriptors sharing the same allocation, with distinct
  offsets. SSBO writes and image writes are checked independently.
- `imageSize` comes from a 6x3 resource despite a 4x2 launch; unaddressed pixels
  and, in single-dispatch variants, the unused image must remain zero.
- Standard buffer/image transfer commands execute at submission in order.
- Two geometries in one BLAS, with two primitives per geometry. Triangle and
  procedural AABB variants both check `GeometryIndex` and local `PrimitiveID`.
- Two translated instances share the same BLAS and use custom indices 7/11.
  This tests actual BLAS reuse rather than duplicating geometry per instance.
- Both `ppGeometries` and `pGeometries`; geometry/range host structures are
  deliberately overwritten after command recording and before submission.
- Descriptor layout support queries, including rejection of a layout beyond
  the current Ventus descriptor capacity.

The analytic expected image encodes geometry ID in red, primitive ID in green,
and instance custom index plus UBO values in blue. SSBO values independently
encode the same IDs as integers. Lavapipe validates the fixture against these
expectations; it is not used to manufacture the expected values.

## Current implementation boundaries

This is a focused integration test, not Vulkan CTS or a conformance claim.
The current descriptor ABI allows four sets and 32 **descriptor elements** per
set; arrays consume multiple elements. Sparse API binding numbers may exceed
32. Supported layout types are AS, storage image, combined image/sampler,
UBO/SSBO and their dynamic buffer variants. Immutable samplers and nonzero
layout/binding flags are rejected. Runtime-sized descriptor arrays,
update-after-bind, nonuniform indexing, separate image/sampler descriptors,
and arbitrary image formats/dimensions are not covered by this probe.

This probe does not cover Ray Query or rewrite application BLAS/TLAS
partitioning. Dated validation results are retained under
`artifacts/rtcore-regression/reports/`, not in the test source directory.
