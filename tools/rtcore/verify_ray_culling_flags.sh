#!/usr/bin/env bash
set -euo pipefail

# Prove that Vulkan ray and instance culling flags survive the live
# Vulkan -> compiler -> global queue -> Spike RTcore path.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ENV_ROOT}/build/rt-workload/source}"
APP="${APP:-${ENV_ROOT}/build/rt-workload/build/bin/raytracingbasic}"
RUNNER="${RUNNER:-${ENV_ROOT}/tools/rtcore/run_full_app_spike.sh}"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/ray_culling_flags}"
EXPECTED_BASELINE_SHA256="${EXPECTED_BASELINE_SHA256:-dd5b8d1bdad0ba7dc955f1eff2814327470d8755e5220c583491c44a55da191b}"
EXPECTED_CULLED_SHA256="${EXPECTED_CULLED_SHA256:-0605389515009d833fe91e7dcb359d185545d2716f7f7e619794886f91c151cb}"

die() {
  echo "error: $*" >&2
  exit 1
}

[[ -x "${APP}" ]] || die "raytracingbasic executable not found: ${APP}"
[[ -x "${RUNNER}" ]] || die "runner not found: ${RUNNER}"

run_case() {
  local name="$1"
  local ray_flags="$2"
  local instance_flags="$3"
  local case_dir="${OUT_DIR}/${name}"
  local ppm="${case_dir}/16x16/raytracingbasic_spike.ppm"

  APP="${APP}" \
  APP_NAME=raytracingbasic \
  WORKLOAD_DIR="${WORKLOAD_DIR}" \
  OUT_DIR="${case_dir}" \
  RUN_1X1=0 \
  RUN_16X16=1 \
  RUN_160X96=0 \
  RUN_320X192=0 \
  VENTUS_VK_RT_EXECUTION_PROFILE=global \
  VENTUS_RT_SAMPLE_RAY_FLAGS="${ray_flags}" \
  VENTUS_RT_SAMPLE_INSTANCE_FLAGS="${instance_flags}" \
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

# The sample geometry is back-facing from the camera.  These relations verify
# both face selection and the instance-facing flip without depending on a
# second scene.
baseline="$(run_case baseline 1 1)"
cull_disabled="$(run_case cull_disabled 49 1)"
cull_front_no_flip="$(run_case cull_front_no_flip 33 0)"
cull_back_with_flip="$(run_case cull_back_with_flip 17 2)"
cull_back_no_flip="$(run_case cull_back_no_flip 17 0)"
cull_front_with_flip="$(run_case cull_front_with_flip 33 2)"
cull_both="$(run_case cull_both 49 0)"

require_hash baseline "${baseline}" "${EXPECTED_BASELINE_SHA256}"
require_hash cull_disabled "${cull_disabled}" "${baseline}"
require_hash cull_front_no_flip "${cull_front_no_flip}" "${baseline}"
require_hash cull_back_with_flip "${cull_back_with_flip}" "${baseline}"
require_hash cull_back_no_flip "${cull_back_no_flip}" "${EXPECTED_CULLED_SHA256}"
require_hash cull_front_with_flip "${cull_front_with_flip}" "${EXPECTED_CULLED_SHA256}"
require_hash cull_both "${cull_both}" "${EXPECTED_CULLED_SHA256}"

# Geometry is opaque by default.  Instance force flags alter that effective
# opacity, while ray force flags take precedence over instance force flags.
cull_opaque="$(run_case cull_opaque 64 0)"
cull_nonopaque_plain="$(run_case cull_nonopaque_plain 128 0)"
instance_force_nonopaque="$(run_case instance_force_nonopaque 128 8)"
ray_opaque_override="$(run_case ray_opaque_override 65 8)"
ray_nonopaque_override="$(run_case ray_nonopaque_override 130 4)"

require_hash cull_opaque "${cull_opaque}" "${EXPECTED_CULLED_SHA256}"
require_hash cull_nonopaque_plain "${cull_nonopaque_plain}" "${baseline}"
require_hash instance_force_nonopaque \
  "${instance_force_nonopaque}" "${EXPECTED_CULLED_SHA256}"
require_hash ray_opaque_override \
  "${ray_opaque_override}" "${EXPECTED_CULLED_SHA256}"
require_hash ray_nonopaque_override \
  "${ray_nonopaque_override}" "${EXPECTED_CULLED_SHA256}"

printf 'PASS ray-culling-flags baseline=%s culled=%s cases=12\n' \
  "${baseline}" "${EXPECTED_CULLED_SHA256}"
