#!/usr/bin/env bash
set -euo pipefail

# Prove that descriptor-set identity survives Vulkan binding, compiler
# lowering, driver upload, and Spike execution.  The generated two-set sample
# must remain pixel-identical to its frozen single-set baseline.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ENV_ROOT}/tools/rtcore/rt_profile.sh"
RT_PROFILE="$(ventus_rt_execution_profile)"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ENV_ROOT}/build/rt-workload/source}"
APP="${APP:-${ENV_ROOT}/build/rt-workload/build/bin/raytracingbasic}"
RUNNER="${RUNNER:-${ENV_ROOT}/tools/rtcore/run_full_app_spike.sh}"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/multiset_exact_image}"
EXPECTED_PPM_SHA256="${EXPECTED_PPM_SHA256:-1d8261975e5d53e8832a2c7acfa8606ff60b2c5705fca61bf7ad2002bd666bfe}"
RAYGEN_SOURCE="${WORKLOAD_DIR}/shaders/glsl/raytracingbasic/raygen.rgen"

die() {
  echo "error: $*" >&2
  exit 1
}

[[ -x "${APP}" ]] || die "raytracingbasic executable not found: ${APP}"
[[ -x "${RUNNER}" ]] || die "runner not found: ${RUNNER}"
[[ -f "${RAYGEN_SOURCE}" ]] || die "raygen source not found: ${RAYGEN_SOURCE}"

rg -q 'binding = 0, set = 0.*accelerationStructureEXT' "${RAYGEN_SOURCE}" ||
  die "AS is not sourced from set 0 binding 0"
rg -q 'binding = 0, set = 1.*image2D' "${RAYGEN_SOURCE}" ||
  die "output image is not sourced from set 1 binding 0"
rg -q 'binding = 1, set = 1.*CameraProperties' "${RAYGEN_SOURCE}" ||
  die "camera UBO is not sourced from set 1 binding 1"

APP="${APP}" \
APP_NAME=raytracingbasic \
WORKLOAD_DIR="${WORKLOAD_DIR}" \
OUT_DIR="${OUT_DIR}" \
RUN_1X1=0 \
RUN_16X16=0 \
RUN_160X96=1 \
RUN_320X192=0 \
VENTUS_VK_PROBE_LOG=1 \
"${RUNNER}"

PPM="${OUT_DIR}/160x96/raytracingbasic_spike.ppm"
LOG="${OUT_DIR}/160x96/raytracingbasic.log"
[[ -f "${PPM}" ]] || die "output image not found: ${PPM}"
[[ -f "${LOG}" ]] || die "runtime log not found: ${LOG}"

actual_sha="$(sha256sum "${PPM}" | awk '{print $1}')"
[[ "${actual_sha}" == "${EXPECTED_PPM_SHA256}" ]] ||
  die "two-set image differs from single-set baseline: ${actual_sha}"

rg -q 'descriptors=3' "${LOG}" ||
  die "runtime did not collect all three descriptors"
rg -q 'descriptor set=0 binding=0 type=1000150000' "${LOG}" ||
  die "set 0 AS descriptor did not reach the driver table"
rg -q 'descriptor set=1 binding=0 type=3' "${LOG}" ||
  die "set 1 output image descriptor did not reach the driver table"
rg -q 'descriptor set=1 binding=1 type=6' "${LOG}" ||
  die "set 1 camera descriptor did not reach the driver table"
rg -q 'vkCmdTraceRaysIndirect2KHR resolved 160x96x1 and SBT regions' "${LOG}" ||
  die "SBT regions and dimensions did not come through the indirect2 command"
[[ "$(rg -c 'indirect AS build wrote Ventus BVH ABI v2 objects=1 geometries=1' \
       "${LOG}")" == 2 ]] ||
  die "BLAS and TLAS were not both built from indirect range records"

printf 'PASS multiset-indirect-as-indirect2-rt-image dimensions=160x96 sha256=%s slots=0,32,33\n' \
  "${actual_sha}"
