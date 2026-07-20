#!/usr/bin/env bash
set -euo pipefail

# End-to-end gate for procedural AABB traversal. The pinned Vulkan workload
# must run through the Ventus ICD, compiler, runtime and Spike RT model and
# produce the frozen nontrivial sphere image.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MESA_BUILD="${MESA_BUILD:-${ENV_ROOT}/mesa/build-ventus}"
LLVM_BUILD="${LLVM_BUILD:-${ENV_ROOT}/llvm/build}"
SPIKE_BUILD="${SPIKE_BUILD:-${ENV_ROOT}/spike/build}"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ENV_ROOT}/build/rt-workload/source}"
APP="${APP:-${ENV_ROOT}/build/rt-workload/build/bin/raytracingintersection}"
RUNNER="${RUNNER:-${ENV_ROOT}/tools/rtcore/run_full_app_spike.sh}"
ICD="${ICD:-${MESA_BUILD}/src/ventus/vulkan/ventus_devenv_icd.x86_64.json}"
DRIVER_LIB="${DRIVER_LIB:-${ENV_ROOT}/driver/build/driver/spike_device/libspike_driver.so}"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/procedural_aabb_exact_image}"

EXPECTED_PPM_SHA256="${EXPECTED_PPM_SHA256:-bdb06f654a712e58968c20823c54c44fb51d9ed37e4259720e0c1272dd448a17}"
EXPECTED_UPSTREAM_COMMIT="3b843fbf667a89a1cfcc64405e9fc6f9018e03b4"
EXPECTED_GLM_COMMIT="1ad55c5016339b83b7eec98c31007e0aee57d2bf"

INTERSECTION_SOURCE="${INTERSECTION_SOURCE:-${WORKLOAD_DIR}/shaders/glsl/raytracingintersection/intersection.rint}"
APP_SOURCE="${APP_SOURCE:-${WORKLOAD_DIR}/examples/raytracingintersection/raytracingintersection.cpp}"
UPSTREAM_DIR="${UPSTREAM_DIR:-${ENV_ROOT}/workloads/rt/SaschaWillems_Vulkan}"

die() {
  echo "error: $*" >&2
  exit 1
}

for tool in git python3 readlink rg sha256sum; do
  command -v "${tool}" >/dev/null 2>&1 || die "required tool not found: ${tool}"
done

for file in "${ICD}" "${DRIVER_LIB}" "${INTERSECTION_SOURCE}" "${APP_SOURCE}"; do
  [[ -f "${file}" ]] || die "required file not found: ${file}"
done
[[ -x "${APP}" ]] || die "required executable not found: ${APP}"
[[ -x "${RUNNER}" ]] || die "required executable not found: ${RUNNER}"

[[ "$(git -C "${UPSTREAM_DIR}" rev-parse HEAD)" == "${EXPECTED_UPSTREAM_COMMIT}" ]] ||
  die "upstream workload is not pinned to ${EXPECTED_UPSTREAM_COMMIT}"
[[ "$(git -C "${UPSTREAM_DIR}/external/glm" rev-parse HEAD)" == "${EXPECTED_GLM_COMMIT}" ]] ||
  die "workload GLM is not pinned to ${EXPECTED_GLM_COMMIT}"

rg -q 'reportIntersectionEXT' "${INTERSECTION_SOURCE}" ||
  die "intersection shader no longer reports procedural hits"
rg -q 'VK_GEOMETRY_TYPE_AABBS_KHR' "${APP_SOURCE}" ||
  die "workload no longer builds AABB geometry"
rg -q 'std::default_random_engine rndGenerator\(benchmark.active \? 0' "${APP_SOURCE}" ||
  die "benchmark mode no longer guarantees deterministic geometry"

ROOT_DIR="${ENV_ROOT}" \
MESA_BUILD="${MESA_BUILD}" \
LLVM_BUILD="${LLVM_BUILD}" \
SPIKE_BUILD="${SPIKE_BUILD}" \
WORKLOAD_DIR="${WORKLOAD_DIR}" \
APP="${APP}" \
OUT_DIR="${OUT_DIR}" \
ICD="${ICD}" \
DRIVER_LIB="${DRIVER_LIB}" \
RUN_1X1=0 \
RUN_16X16=0 \
RUN_160X96=1 \
RUN_320X192=0 \
CCACHE_DIR="${CCACHE_DIR:-/tmp/ventus-rt-aabb-ccache}" \
XDG_CACHE_HOME="${XDG_CACHE_HOME:-/tmp/ventus-rt-aabb-cache}" \
MESA_SHADER_CACHE_DISABLE=true \
VENTUS_SPIKE_LOG="${VENTUS_SPIKE_LOG:-0}" \
"${RUNNER}"

PPM="${OUT_DIR}/160x96/raytracingintersection_spike.ppm"
[[ -f "${PPM}" ]] || die "expected PPM not found: ${PPM}"

python3 - "${PPM}" "${EXPECTED_PPM_SHA256}" <<'PY'
from collections import Counter
import hashlib
import sys

ppm_path, expected_sha = sys.argv[1:]
data = open(ppm_path, "rb").read()
pos = 0


def token():
    global pos
    while pos < len(data):
        if data[pos:pos + 1] == b"#":
            pos = data.index(b"\n", pos) + 1
        elif data[pos] in b" \t\r\n":
            pos += 1
        else:
            break
    start = pos
    while pos < len(data) and data[pos] not in b" \t\r\n":
        pos += 1
    return data[start:pos]


magic = token()
width = int(token())
height = int(token())
max_value = int(token())
while pos < len(data) and data[pos] in b" \t\r\n":
    pos += 1
pixels = data[pos:]

if magic != b"P6" or max_value != 255:
    raise SystemExit(f"unsupported PPM header in {ppm_path}")
if (width, height) != (160, 96):
    raise SystemExit(f"unexpected image dimensions: {width}x{height}")
if len(pixels) != width * height * 3:
    raise SystemExit(
        f"payload size mismatch: {len(pixels)} != {width * height * 3}"
    )

colors = Counter(
    tuple(pixels[i:i + 3]) for i in range(0, len(pixels), 3)
)
background = (0, 0, 51)
pixel_count = width * height
non_background = pixel_count - colors[background]
dominant_color, dominant_count = colors.most_common(1)[0]
center = tuple(
    pixels[((height // 2) * width + width // 2) * 3:
           ((height // 2) * width + width // 2 + 1) * 3]
)
actual_sha = hashlib.sha256(data).hexdigest()

if len(colors) < 1000:
    raise SystemExit(f"image is not color-diverse: {len(colors)} colors")
if non_background < 3000:
    raise SystemExit(f"too few procedural-hit pixels: {non_background}")
if dominant_count / pixel_count > 0.80:
    raise SystemExit(
        f"dominant color covers too much of image: {dominant_color} "
        f"{dominant_count}/{pixel_count}"
    )
if center == background:
    raise SystemExit("center ray did not shade procedural geometry")
if actual_sha != expected_sha:
    raise SystemExit(
        f"image SHA mismatch: expected {expected_sha}, got {actual_sha}"
    )

print(
    "PASS procedural-aabb-image "
    f"dimensions={width}x{height} colors={len(colors)} "
    f"non_background={non_background} center={center} "
    f"sha256={actual_sha}"
)
PY

VALIDATION_MANIFEST="${OUT_DIR}/procedural-aabb-validation.txt"
{
  echo "profile=procedural-aabb-exact-image"
  echo "spike_library=$(readlink -f "${SPIKE_BUILD}/libspike_main.so")"
  echo "driver_library=${DRIVER_LIB}"
  echo "app=${APP}"
  echo "app_sha256=$(sha256sum "${APP}" | awk '{print $1}')"
  echo "upstream_commit=${EXPECTED_UPSTREAM_COMMIT}"
  echo "glm_commit=${EXPECTED_GLM_COMMIT}"
  echo "output_ppm=${PPM}"
  echo "expected_sha256=${EXPECTED_PPM_SHA256}"
  echo "actual_sha256=$(sha256sum "${PPM}" | awk '{print $1}')"
  echo "comparison=byte-identical-and-semantic-image-thresholds"
} >"${VALIDATION_MANIFEST}"

echo "manifest=${VALIDATION_MANIFEST}"
