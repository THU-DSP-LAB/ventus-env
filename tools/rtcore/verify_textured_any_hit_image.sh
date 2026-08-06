#!/usr/bin/env bash
set -euo pipefail

# End-to-end gate for RT push constants, buffer device addresses, sampled
# images and any-hit transparency on the Ventus Spike backend.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MESA_BUILD="${MESA_BUILD:-${ENV_ROOT}/mesa/build-ventus}"
LLVM_BUILD="${LLVM_BUILD:-${ENV_ROOT}/llvm/build}"
SPIKE_BUILD="${SPIKE_BUILD:-${ENV_ROOT}/spike/build}"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ENV_ROOT}/build/rt-workload/source}"
APP="${APP:-${ENV_ROOT}/build/rt-workload/build/bin/raytracingtextures}"
RUNNER="${RUNNER:-${ENV_ROOT}/tools/rtcore/run_full_app_spike.sh}"
ICD="${ICD:-${MESA_BUILD}/src/ventus/vulkan/ventus_devenv_icd.x86_64.json}"
DRIVER_LIB="${DRIVER_LIB:-${ENV_ROOT}/driver/build/driver/spike_device/libspike_driver.so}"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/textured_any_hit_exact_image}"

source "${ENV_ROOT}/tools/rtcore/rt_profile.sh"
RT_PROFILE="$(ventus_rt_execution_profile)"
case "${RT_PROFILE}" in
  compat|global)
    DEFAULT_PPM_SHA256=0a11a83f1beca8bd433dd2f2652938596744a42841fd0ce304ac1c5c64fadae1
    ;;
  *)
    echo "error: no textured-any-hit golden for RT profile ${RT_PROFILE}" >&2
    exit 1
    ;;
esac
EXPECTED_PPM_SHA256="${EXPECTED_PPM_SHA256:-${DEFAULT_PPM_SHA256}}"
EXPECTED_UPSTREAM_COMMIT="3b843fbf667a89a1cfcc64405e9fc6f9018e03b4"
EXPECTED_ASSET_SHA256="f27af40f84e22a1f9a423204af5cff1f822fe4c1cbf6a66247f191c842e9078b"

APP_SOURCE="${WORKLOAD_DIR}/examples/raytracingtextures/raytracingtextures.cpp"
HIT_SOURCE="${WORKLOAD_DIR}/shaders/glsl/raytracingtextures/closesthit.rchit"
ANY_HIT_SOURCE="${WORKLOAD_DIR}/shaders/glsl/raytracingtextures/anyhit.rahit"
TEXTURE_ASSET="${WORKLOAD_DIR}/assets/textures/gratefloor_rgba.ktx"
UPSTREAM_DIR="${ENV_ROOT}/workloads/rt/SaschaWillems_Vulkan"

die() {
  echo "error: $*" >&2
  exit 1
}

for tool in git python3 readlink rg sha256sum; do
  command -v "${tool}" >/dev/null 2>&1 || die "required tool not found: ${tool}"
done

for file in "${ICD}" "${DRIVER_LIB}" "${APP_SOURCE}" "${HIT_SOURCE}" \
  "${ANY_HIT_SOURCE}" "${TEXTURE_ASSET}"; do
  [[ -f "${file}" ]] || die "required file not found: ${file}"
done
[[ -x "${APP}" ]] || die "required executable not found: ${APP}"
[[ -x "${RUNNER}" ]] || die "required executable not found: ${RUNNER}"

[[ "$(git -C "${UPSTREAM_DIR}" rev-parse HEAD)" == "${EXPECTED_UPSTREAM_COMMIT}" ]] ||
  die "upstream workload is not pinned to ${EXPECTED_UPSTREAM_COMMIT}"
[[ "$(sha256sum "${TEXTURE_ASSET}" | awk '{print $1}')" == "${EXPECTED_ASSET_SHA256}" ]] ||
  die "texture asset hash mismatch"

rg -q 'VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER' "${APP_SOURCE}" ||
  die "application no longer binds the combined image sampler"
rg -q 'vkCmdPushConstants' "${APP_SOURCE}" ||
  die "application no longer supplies buffer references through push constants"
rg -q 'texture\(image, tri\.uv\)' "${HIT_SOURCE}" ||
  die "closest-hit shader no longer samples the hit texture"
rg -q 'ignoreIntersectionEXT' "${ANY_HIT_SOURCE}" ||
  die "any-hit shader no longer rejects transparent intersections"

ROOT_DIR="${ENV_ROOT}" \
MESA_BUILD="${MESA_BUILD}" \
LLVM_BUILD="${LLVM_BUILD}" \
SPIKE_BUILD="${SPIKE_BUILD}" \
WORKLOAD_DIR="${WORKLOAD_DIR}" \
APP="${APP}" \
APP_NAME=raytracingtextures \
OUT_DIR="${OUT_DIR}" \
ICD="${ICD}" \
DRIVER_LIB="${DRIVER_LIB}" \
REQUIRED_ASSET="${TEXTURE_ASSET}" \
RUN_1X1=0 \
RUN_16X16=0 \
RUN_160X96=1 \
RUN_320X192=0 \
CCACHE_DIR="${CCACHE_DIR:-/tmp/ventus-rt-textures-ccache}" \
XDG_CACHE_HOME="${XDG_CACHE_HOME:-/tmp/ventus-rt-textures-cache}" \
MESA_SHADER_CACHE_DISABLE=true \
VENTUS_SPIKE_LOG="${VENTUS_SPIKE_LOG:-0}" \
"${RUNNER}"

PPM="${OUT_DIR}/160x96/raytracingtextures_spike.ppm"
LOG="${OUT_DIR}/160x96/raytracingtextures.log"
[[ -f "${PPM}" ]] || die "expected PPM not found: ${PPM}"
[[ -f "${LOG}" ]] || die "expected execution log not found: ${LOG}"
rg -q 'stages=4 groups=3' "${LOG}" ||
  die "runtime log does not prove the four-stage RT pipeline"
rg -q 'descriptors=4.*push=16' "${LOG}" ||
  die "runtime log does not prove the complete texture dispatch state"
rg -q 'uploaded opaque RT push constants bytes=16' "${LOG}" ||
  die "runtime log does not prove opaque push-constant upload"
rg -q 'driver bridge executed raygen kernel' "${LOG}" ||
  die "runtime log does not prove Spike execution completed"
if rg -q 'load access fault|bad syscall|failed|unsupported texture operation' "${LOG}"; then
  die "runtime/compiler log reports a rejected or faulting texture path"
fi

python3 - "${PPM}" "${EXPECTED_PPM_SHA256}" "${RT_PROFILE}" <<'PY'
from collections import Counter
import hashlib
import sys

ppm_path, expected_sha, profile = sys.argv[1:]
data = open(ppm_path, "rb").read()
header, pixels = data.split(b"\n", 3)[:3], data.split(b"\n", 3)[3]
magic, dimensions, max_value = header
width, height = map(int, dimensions.split())

if magic != b"P6" or (width, height, int(max_value)) != (160, 96, 255):
    raise SystemExit("unexpected PPM header")
if len(pixels) != width * height * 3:
    raise SystemExit("PPM payload size mismatch")

colors = Counter(tuple(pixels[i:i + 3]) for i in range(0, len(pixels), 3))
background = colors[(0, 0, 51)]
black = colors[(0, 0, 0)]
gray = sum(
    count for (r, g, b), count in colors.items()
    if abs(r - g) < 8 and abs(g - b) < 8 and r > 50
)
actual_sha = hashlib.sha256(data).hexdigest()

coverage_incomplete = (
    len(colors) < 2500 or gray < 3000 or background < 10000 or black != 0
)
if coverage_incomplete:
    raise SystemExit(
        "textured transparency coverage is incomplete: "
        f"profile={profile} colors={len(colors)} background={background} "
        f"black={black} gray={gray}"
    )
if actual_sha != expected_sha:
    raise SystemExit(
        f"image SHA mismatch: expected {expected_sha}, got {actual_sha}"
    )

print(
    "PASS textured-any-hit-image "
    f"profile={profile} dimensions={width}x{height} colors={len(colors)} "
    f"background={background} black={black} gray={gray} sha256={actual_sha}"
)
PY

VALIDATION_MANIFEST="${OUT_DIR}/textured-any-hit-validation.txt"
{
  echo "profile=textured-any-hit-${RT_PROFILE}-exact-image"
  echo "spike_library=$(readlink -f "${SPIKE_BUILD}/libspike_main.so")"
  echo "driver_library=${DRIVER_LIB}"
  echo "app=${APP}"
  echo "app_sha256=$(sha256sum "${APP}" | awk '{print $1}')"
  echo "upstream_commit=${EXPECTED_UPSTREAM_COMMIT}"
  echo "texture_asset=${TEXTURE_ASSET}"
  echo "texture_asset_sha256=${EXPECTED_ASSET_SHA256}"
  echo "output_ppm=${PPM}"
  echo "expected_sha256=${EXPECTED_PPM_SHA256}"
  echo "actual_sha256=$(sha256sum "${PPM}" | awk '{print $1}')"
  echo "comparison=byte-identical-plus-${RT_PROFILE}-texture-and-any-hit-semantic-thresholds"
} >"${VALIDATION_MANIFEST}"

echo "manifest=${VALIDATION_MANIFEST}"
