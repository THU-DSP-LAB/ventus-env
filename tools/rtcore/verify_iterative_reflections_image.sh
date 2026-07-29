#!/usr/bin/env bash
set -euo pipefail

# End-to-end gate for structured payloads and sequential reflection rays. The
# pinned Vulkan sample must compile through Mesa/Ventus, execute on Spike and
# reproduce the frozen multi-reflection scene.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MESA_BUILD="${MESA_BUILD:-${ENV_ROOT}/mesa/build-ventus}"
LLVM_BUILD="${LLVM_BUILD:-${ENV_ROOT}/llvm/build}"
SPIKE_BUILD="${SPIKE_BUILD:-${ENV_ROOT}/spike/build}"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ENV_ROOT}/build/rt-workload/source}"
APP="${APP:-${ENV_ROOT}/build/rt-workload/build/bin/raytracingreflections}"
RUNNER="${RUNNER:-${ENV_ROOT}/tools/rtcore/run_full_app_spike.sh}"
ICD="${ICD:-${MESA_BUILD}/src/ventus/vulkan/ventus_devenv_icd.x86_64.json}"
DRIVER_LIB="${DRIVER_LIB:-${ENV_ROOT}/driver/build/driver/spike_device/libspike_driver.so}"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/iterative_reflections_exact_image}"

RT_PROFILE=compat
DEFAULT_PPM_SHA256=65480e26a328cd5bb1205d33fc6114837f7cae71cea0754eec9c4b31e5334703
if [[ "${VENTUS_VK_RT_WAVEFRONT_GLOBAL:-0}" == "1" ]]; then
  RT_PROFILE=global
  DEFAULT_PPM_SHA256=ebe1a1a08e3474d1d3e5a6fe942e841363df828248d09833fad0abb57f9fa452
fi
EXPECTED_PPM_SHA256="${EXPECTED_PPM_SHA256:-${DEFAULT_PPM_SHA256}}"
EXPECTED_UPSTREAM_COMMIT="3b843fbf667a89a1cfcc64405e9fc6f9018e03b4"
EXPECTED_GLM_COMMIT="1ad55c5016339b83b7eec98c31007e0aee57d2bf"
EXPECTED_ASSET_SHA256="d20d0fb6ba02333b37f77f4fef576c1748eaa138a588654643196fb859325585"

RAYGEN_SOURCE="${RAYGEN_SOURCE:-${WORKLOAD_DIR}/shaders/glsl/raytracingreflections/raygen.rgen}"
HIT_SOURCE="${HIT_SOURCE:-${WORKLOAD_DIR}/shaders/glsl/raytracingreflections/closesthit.rchit}"
APP_SOURCE="${APP_SOURCE:-${WORKLOAD_DIR}/examples/raytracingreflections/raytracingreflections.cpp}"
SCENE_ASSET="${SCENE_ASSET:-${WORKLOAD_DIR}/assets/models/reflection_scene.gltf}"
UPSTREAM_DIR="${UPSTREAM_DIR:-${ENV_ROOT}/workloads/rt/SaschaWillems_Vulkan}"

die() {
  echo "error: $*" >&2
  exit 1
}

for tool in git python3 readlink rg sha256sum; do
  command -v "${tool}" >/dev/null 2>&1 || die "required tool not found: ${tool}"
done

for file in "${ICD}" "${DRIVER_LIB}" "${RAYGEN_SOURCE}" "${HIT_SOURCE}" \
  "${APP_SOURCE}" "${SCENE_ASSET}"; do
  [[ -f "${file}" ]] || die "required file not found: ${file}"
done
[[ -x "${APP}" ]] || die "required executable not found: ${APP}"
[[ -x "${RUNNER}" ]] || die "required executable not found: ${RUNNER}"

[[ "$(git -C "${UPSTREAM_DIR}" rev-parse HEAD)" == "${EXPECTED_UPSTREAM_COMMIT}" ]] ||
  die "upstream workload is not pinned to ${EXPECTED_UPSTREAM_COMMIT}"
[[ "$(git -C "${UPSTREAM_DIR}/external/glm" rev-parse HEAD)" == "${EXPECTED_GLM_COMMIT}" ]] ||
  die "workload GLM is not pinned to ${EXPECTED_GLM_COMMIT}"
[[ "$(sha256sum "${SCENE_ASSET}" | awk '{print $1}')" == "${EXPECTED_ASSET_SHA256}" ]] ||
  die "reflection scene asset hash mismatch"

rg -q 'struct RayPayload' "${RAYGEN_SOURCE}" ||
  die "raygen shader no longer uses the structured reflection payload"
rg -q 'for \(int i = 0; i < MAX_RECURSION; i\+\+\)' "${RAYGEN_SOURCE}" ||
  die "raygen shader no longer launches rays iteratively"
rg -q 'traceRayEXT\(' "${RAYGEN_SOURCE}" ||
  die "raygen shader no longer launches reflection rays"
rg -q 'direction\.xyz = reflect\(' "${RAYGEN_SOURCE}" ||
  die "raygen shader no longer derives reflected rays"
rg -q 'layout\(binding = 3, set = 0\) buffer Vertices' "${HIT_SOURCE}" ||
  die "closest-hit shader no longer consumes the vertex SSBO"
rg -q 'layout\(binding = 4, set = 0\) buffer Indices' "${HIT_SOURCE}" ||
  die "closest-hit shader no longer consumes the index SSBO"
rg -q 'gl_PrimitiveID' "${HIT_SOURCE}" ||
  die "closest-hit shader no longer indexes hit geometry"
rg -q 'uint32_t maxRecursion = 4;' "${APP_SOURCE}" ||
  die "host specialization no longer requests four reflection iterations"

ROOT_DIR="${ENV_ROOT}" \
MESA_BUILD="${MESA_BUILD}" \
LLVM_BUILD="${LLVM_BUILD}" \
SPIKE_BUILD="${SPIKE_BUILD}" \
WORKLOAD_DIR="${WORKLOAD_DIR}" \
APP="${APP}" \
OUT_DIR="${OUT_DIR}" \
ICD="${ICD}" \
DRIVER_LIB="${DRIVER_LIB}" \
REQUIRED_ASSET="${SCENE_ASSET}" \
RUN_1X1=0 \
RUN_16X16=0 \
RUN_160X96=1 \
RUN_320X192=0 \
CCACHE_DIR="${CCACHE_DIR:-/tmp/ventus-rt-reflections-ccache}" \
XDG_CACHE_HOME="${XDG_CACHE_HOME:-/tmp/ventus-rt-reflections-cache}" \
MESA_SHADER_CACHE_DISABLE=true \
VENTUS_SPIKE_LOG="${VENTUS_SPIKE_LOG:-0}" \
"${RUNNER}"

PPM="${OUT_DIR}/160x96/raytracingreflections_spike.ppm"
LOG="${OUT_DIR}/160x96/raytracingreflections.log"
[[ -f "${PPM}" ]] || die "expected PPM not found: ${PPM}"
[[ -f "${LOG}" ]] || die "expected execution log not found: ${LOG}"
rg -q 'stages=3 groups=3' "${LOG}" ||
  die "runtime log does not prove the three-stage RT pipeline"
rg -q 'descriptors=5' "${LOG}" ||
  die "runtime log does not prove all reflection descriptors were bound"
if rg -q 'PHINode should have one entry|input module cannot be verified|llc: error' "${LOG}"; then
  die "compiler rejected the iterative reflection control flow"
fi

python3 - "${PPM}" "${EXPECTED_PPM_SHA256}" "${RT_PROFILE}" <<'PY'
from collections import Counter
import hashlib
import sys

ppm_path, expected_sha, profile = sys.argv[1:]
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
    raise SystemExit("PPM payload size mismatch")

colors = Counter(
    tuple(pixels[i:i + 3]) for i in range(0, len(pixels), 3)
)
samples = list(colors.elements())
black = colors[(0, 0, 0)]
red = sum(r > 80 and r > g * 2 and r > b * 2 for r, g, b in samples)
green = sum(g > 50 and g > r * 2 and g > b * 1.2 for r, g, b in samples)
blue = sum(b > 30 and b > r * 2 and b > g * 2 for r, g, b in samples)
gold = sum(r > 60 and g > 30 and r > g * 1.3 and b < 30 for r, g, b in samples)
sky = sum(b > 200 and r > 100 and g > 100 for r, g, b in samples)
actual_sha = hashlib.sha256(data).hexdigest()

if len(colors) < 200:
    raise SystemExit(f"image is not color-diverse: {len(colors)} colors")
if black < 3500 or sky < 8000:
    raise SystemExit(f"checkerboard/sky coverage is incomplete: black={black} sky={sky}")
if min(red, green, blue, gold) < 50:
    raise SystemExit(
        "reflected material colors are incomplete: "
        f"red={red} green={green} blue={blue} gold={gold}"
    )
if actual_sha != expected_sha:
    raise SystemExit(
        f"image SHA mismatch: expected {expected_sha}, got {actual_sha}"
    )

print(
    "PASS iterative-reflections-image "
    f"profile={profile} dimensions={width}x{height} "
    f"colors={len(colors)} black={black} "
    f"red={red} green={green} blue={blue} gold={gold} sky={sky} "
    f"sha256={actual_sha}"
)
PY

VALIDATION_MANIFEST="${OUT_DIR}/iterative-reflections-validation.txt"
{
  echo "profile=iterative-reflections-${RT_PROFILE}-exact-image"
  echo "spike_library=$(readlink -f "${SPIKE_BUILD}/libspike_main.so")"
  echo "driver_library=${DRIVER_LIB}"
  echo "app=${APP}"
  echo "app_sha256=$(sha256sum "${APP}" | awk '{print $1}')"
  echo "upstream_commit=${EXPECTED_UPSTREAM_COMMIT}"
  echo "glm_commit=${EXPECTED_GLM_COMMIT}"
  echo "scene_asset=${SCENE_ASSET}"
  echo "scene_asset_sha256=${EXPECTED_ASSET_SHA256}"
  echo "output_ppm=${PPM}"
  echo "expected_sha256=${EXPECTED_PPM_SHA256}"
  echo "actual_sha256=$(sha256sum "${PPM}" | awk '{print $1}')"
  echo "comparison=byte-identical-plus-${RT_PROFILE}-structured-payload-and-reflection-semantic-thresholds"
} >"${VALIDATION_MANIFEST}"

echo "manifest=${VALIDATION_MANIFEST}"
