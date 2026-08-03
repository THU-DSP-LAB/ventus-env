#!/usr/bin/env bash
set -euo pipefail

# Verify the bounded callable contract: nested callable invocation fails during
# real pipeline compilation, and callable-originated traceRayEXT remains an
# explicit compiler rejection even though glslang cannot produce that stage.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ENV_ROOT}/tools/rtcore/rt_profile.sh"
RT_PROFILE="$(ventus_rt_execution_profile)"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ENV_ROOT}/build/rt-workload/source}"
APP="${APP:-${ENV_ROOT}/build/rt-workload/build/bin/raytracingcallable}"
RUNNER="${RUNNER:-${ENV_ROOT}/tools/rtcore/run_full_app_spike.sh}"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/callable_shader_restrictions}"
PIPELINE_SOURCE="${PIPELINE_SOURCE:-${ENV_ROOT}/mesa/src/ventus/vulkan/vtvk_pipeline_rt.cpp}"

die() {
  echo "error: $*" >&2
  exit 1
}

command -v rg >/dev/null 2>&1 || die "required tool not found: rg"

[[ -x "${APP}" ]] || die "raytracingcallable executable not found: ${APP}"
[[ -x "${RUNNER}" ]] || die "runner not found: ${RUNNER}"

NESTED_SHADER="${WORKLOAD_DIR}/shaders/glsl/raytracingcallable/callable_nested.rcall"
rg -q 'executeCallableEXT\(0, 1\)' "${NESTED_SHADER}" ||
  die "nested callable shader does not contain executeCallableEXT"

if VENTUS_CALLABLE_NEGATIVE=nested \
   APP="${APP}" \
   APP_NAME=raytracingcallable \
   WORKLOAD_DIR="${WORKLOAD_DIR}" \
   OUT_DIR="${OUT_DIR}" \
   RUN_1X1=1 \
   RUN_16X16=0 \
   RUN_160X96=0 \
   RUN_320X192=0 \
   VENTUS_VK_RT_EXECUTION_PROFILE="${RT_PROFILE}" \
   "${RUNNER}"; then
  die "nested callable pipeline unexpectedly compiled"
fi

LOG="${OUT_DIR}/1x1/raytracingcallable.log"
[[ -f "${LOG}" ]] || die "nested callable failure log not found: ${LOG}"
rg -q 'nested callable shader invocation is not supported' "${LOG}" ||
  die "nested callable failure did not reach the Ventus compiler gate"
rg -q 'executeCallableEXT lowering failed' "${LOG}" ||
  die "nested callable failure did not reject pipeline creation"

rg -q 'nir_intrinsic_trace_ray' "${PIPELINE_SOURCE}" ||
  die "callable traceRayEXT intrinsic gate is missing"
rg -q 'callable-originated traceRayEXT is not' "${PIPELINE_SOURCE}" ||
  die "callable traceRayEXT rejection diagnostic is missing"

echo "PASS callable-shader-restrictions profile=${RT_PROFILE} nested=live-rejected traceRay=frontend-unrepresentable-and-compiler-guarded"
