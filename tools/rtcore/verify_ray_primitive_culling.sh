#!/usr/bin/env bash
set -euo pipefail

# Prove selective SkipTriangles and SkipAABBs behavior through the live
# Vulkan -> compiler -> selected RT backend -> Spike RTcore path.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ENV_ROOT}/tools/rtcore/rt_profile.sh"
RT_PROFILE="$(ventus_rt_execution_profile)"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ENV_ROOT}/build/rt-workload/source}"
APP_DIR="${APP_DIR:-${ENV_ROOT}/build/rt-workload/build/bin}"
RUNNER="${RUNNER:-${ENV_ROOT}/tools/rtcore/run_full_app_spike.sh}"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/ray_primitive_culling}"
EXPECTED_BASIC_SHA256="${EXPECTED_BASIC_SHA256:-dd5b8d1bdad0ba7dc955f1eff2814327470d8755e5220c583491c44a55da191b}"
EXPECTED_MISS_SHA256="${EXPECTED_MISS_SHA256:-0605389515009d833fe91e7dcb359d185545d2716f7f7e619794886f91c151cb}"

die() {
  echo "error: $*" >&2
  exit 1
}

run_case() {
  local app_name="$1"
  local name="$2"
  local ray_flags="$3"
  local app="${APP_DIR}/${app_name}"
  local case_dir="${OUT_DIR}/${name}"
  local ppm="${case_dir}/16x16/${app_name}_spike.ppm"

  [[ -x "${app}" ]] || die "workload executable not found: ${app}"

  APP="${app}" \
  APP_NAME="${app_name}" \
  WORKLOAD_DIR="${WORKLOAD_DIR}" \
  OUT_DIR="${case_dir}" \
  RUN_1X1=0 \
  RUN_16X16=1 \
  RUN_160X96=0 \
  RUN_320X192=0 \
  VENTUS_RT_SAMPLE_RAY_FLAGS="${ray_flags}" \
  VENTUS_RT_SAMPLE_INSTANCE_FLAGS=1 \
  "${RUNNER}" >/dev/null

  [[ -f "${ppm}" ]] || die "output image not found: ${ppm}"
  sha256sum "${ppm}" | awk '{print $1}'
}

require_hash() {
  local name="$1"
  local actual="$2"
  local expected="$3"
  [[ "${actual}" == "${expected}" ]] ||
    die "${name} image mismatch: expected ${expected}, got ${actual}"
}

basic="$(run_case raytracingbasic basic_baseline 1)"
basic_skip_triangles="$(run_case raytracingbasic basic_skip_triangles 257)"
basic_skip_aabbs="$(run_case raytracingbasic basic_skip_aabbs 513)"
procedural="$(run_case raytracingintersection procedural_baseline 1)"
procedural_skip_triangles="$(
  run_case raytracingintersection procedural_skip_triangles 257
)"
procedural_skip_aabbs="$(
  run_case raytracingintersection procedural_skip_aabbs 513
)"

require_hash basic_baseline "${basic}" "${EXPECTED_BASIC_SHA256}"
require_hash basic_skip_triangles \
  "${basic_skip_triangles}" "${EXPECTED_MISS_SHA256}"
require_hash basic_skip_aabbs "${basic_skip_aabbs}" "${basic}"
require_hash procedural_skip_triangles \
  "${procedural_skip_triangles}" "${procedural}"
require_hash procedural_skip_aabbs \
  "${procedural_skip_aabbs}" "${EXPECTED_MISS_SHA256}"
[[ "${procedural}" != "${EXPECTED_MISS_SHA256}" ]] ||
  die "procedural baseline unexpectedly contains only miss shading"

printf 'PASS ray-primitive-culling basic=%s procedural=%s miss=%s cases=6\n' \
  "${basic}" "${procedural}" "${EXPECTED_MISS_SHA256}"
