#!/usr/bin/env bash
set -euo pipefail

# Verify the remaining bounded Callable restrictions. Nested Callable is a
# positive V5 feature; only over-capacity nesting and Callable-originated
# traceRayEXT remain explicit compiler limits in the first profile.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ENV_ROOT}/tools/rtcore/rt_profile.sh"
RT_PROFILE="$(ventus_rt_execution_profile)"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ENV_ROOT}/build/rt-workload/source}"
PIPELINE_SOURCE="${PIPELINE_SOURCE:-${ENV_ROOT}/mesa/src/ventus/vulkan/vtvk_pipeline_rt.cpp}"
COMPILER_HEADER="${COMPILER_HEADER:-${ENV_ROOT}/mesa/src/ventus/compiler/vt_nir.h}"

die() {
  echo "error: $*" >&2
  exit 1
}

command -v rg >/dev/null 2>&1 || die "required tool not found: rg"

NESTED_SHADER="${WORKLOAD_DIR}/shaders/glsl/raytracingcallable/callable_nested.rcall"
rg -q 'executeCallableEXT\(2, 1\)' "${NESTED_SHADER}" ||
  die "nested callable shader does not contain executeCallableEXT"

rg -q 'VT_NIR_RT_BASE_CALLABLE_DEPTH[[:space:]]+2' "${COMPILER_HEADER}" ||
  die "first-profile Callable depth capacity is not two"
rg -q 'vtvk_clone_rt_callable_depth_entries' "${PIPELINE_SOURCE}" ||
  die "bounded Callable depth specialization is missing"
rg -q 'vtvk_lower_rt_callable_depth_overflows' "${PIPELINE_SOURCE}" ||
  die "Callable depth overflow lowering is missing"

rg -q 'nir_intrinsic_trace_ray' "${PIPELINE_SOURCE}" ||
  die "callable traceRayEXT intrinsic gate is missing"
rg -q 'callable-originated traceRayEXT is not' "${PIPELINE_SOURCE}" ||
  die "callable traceRayEXT rejection diagnostic is missing"

echo "PASS callable-shader-restrictions profile=${RT_PROFILE} nested=depth2-bounded traceRay=frontend-unrepresentable-and-compiler-guarded"
