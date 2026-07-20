#!/usr/bin/env bash
set -euo pipefail

# End-to-end gate for shaderRecordEXT data. The pinned Vulkan workload must
# read distinct raygen, miss and hit colors from application-populated SBT
# records through the Ventus ICD, compiler, runtime and Spike.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MESA_BUILD="${MESA_BUILD:-${ENV_ROOT}/mesa/build-ventus}"
LLVM_BUILD="${LLVM_BUILD:-${ENV_ROOT}/llvm/build}"
SPIKE_BUILD="${SPIKE_BUILD:-${ENV_ROOT}/spike/build}"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ENV_ROOT}/build/rt-workload/source}"
APP="${APP:-${ENV_ROOT}/build/rt-workload/build/bin/raytracingsbtdata}"
RUNNER="${RUNNER:-${ENV_ROOT}/tools/rtcore/run_full_app_spike.sh}"
ICD="${ICD:-${MESA_BUILD}/src/ventus/vulkan/ventus_devenv_icd.x86_64.json}"
DRIVER_LIB="${DRIVER_LIB:-${ENV_ROOT}/driver/build/driver/spike_device/libspike_driver.so}"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/sbt_record_data_exact_image}"

EXPECTED_PPM_SHA256="${EXPECTED_PPM_SHA256:-7d585343b55a01b8fb48ad2ca776de8b43b5766ab6fa51fff9783d52cd139e26}"
EXPECTED_UPSTREAM_COMMIT="3b843fbf667a89a1cfcc64405e9fc6f9018e03b4"
EXPECTED_GLM_COMMIT="1ad55c5016339b83b7eec98c31007e0aee57d2bf"

APP_SOURCE="${APP_SOURCE:-${WORKLOAD_DIR}/examples/raytracingsbtdata/raytracingsbtdata.cpp}"
SHADER_DIR="${SHADER_DIR:-${WORKLOAD_DIR}/shaders/glsl/raytracingsbtdata}"
UPSTREAM_DIR="${UPSTREAM_DIR:-${ENV_ROOT}/workloads/rt/SaschaWillems_Vulkan}"

die() {
  echo "error: $*" >&2
  exit 1
}

for tool in git python3 readlink rg sha256sum; do
  command -v "${tool}" >/dev/null 2>&1 || die "required tool not found: ${tool}"
done

for file in \
  "${ICD}" \
  "${DRIVER_LIB}" \
  "${APP_SOURCE}" \
  "${SHADER_DIR}/raygen.rgen" \
  "${SHADER_DIR}/miss.rmiss" \
  "${SHADER_DIR}/closesthit.rchit"; do
  [[ -f "${file}" ]] || die "required file not found: ${file}"
done
[[ -x "${APP}" ]] || die "required executable not found: ${APP}"
[[ -x "${RUNNER}" ]] || die "required executable not found: ${RUNNER}"

[[ "$(git -C "${UPSTREAM_DIR}" rev-parse HEAD)" == "${EXPECTED_UPSTREAM_COMMIT}" ]] ||
  die "upstream workload is not pinned to ${EXPECTED_UPSTREAM_COMMIT}"
[[ "$(git -C "${UPSTREAM_DIR}/external/glm" rev-parse HEAD)" == "${EXPECTED_GLM_COMMIT}" ]] ||
  die "workload GLM is not pinned to ${EXPECTED_GLM_COMMIT}"

for shader in raygen.rgen miss.rmiss closesthit.rchit; do
  rg -q 'layout\(shaderRecordEXT' "${SHADER_DIR}/${shader}" ||
    die "${shader} no longer reads shader-record data"
done
rg -q 'glm::vec3 color1\(0.5f, 0.5f, 0.5f\)' "${APP_SOURCE}" ||
  die "raygen SBT record color changed"
rg -q 'glm::vec3 color2\(1.f, 1.f, 1.f\)' "${APP_SOURCE}" ||
  die "miss SBT record color changed"
rg -q 'glm::vec3 color3\(1.f, 0.f, 0.f\)' "${APP_SOURCE}" ||
  die "hit SBT record color changed"

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
CCACHE_DIR="${CCACHE_DIR:-/tmp/ventus-rt-sbtdata-ccache}" \
XDG_CACHE_HOME="${XDG_CACHE_HOME:-/tmp/ventus-rt-sbtdata-cache}" \
MESA_SHADER_CACHE_DISABLE=true \
VENTUS_SPIKE_LOG="${VENTUS_SPIKE_LOG:-0}" \
"${RUNNER}"

PPM="${OUT_DIR}/160x96/raytracingsbtdata_spike.ppm"
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
expected_colors = Counter({
    (0, 0, 0): 7359,
    (127, 127, 127): 4000,
    (255, 255, 255): 2946,
    (255, 0, 0): 1055,
})
if colors != expected_colors:
    raise SystemExit(
        f"SBT stage-color distribution mismatch: {colors} != {expected_colors}"
    )


def pixel(x, y):
    start = (y * width + x) * 3
    return tuple(pixels[start:start + 3])


probes = {
    (0, 0): (0, 0, 0),
    (24, 24): (255, 255, 255),
    (40, 24): (127, 127, 127),
    (80, 48): (255, 0, 0),
    (100, 48): (127, 127, 127),
    (159, 95): (0, 0, 0),
}
for coordinate, expected in probes.items():
    actual = pixel(*coordinate)
    if actual != expected:
        raise SystemExit(
            f"unexpected pixel at {coordinate}: {actual} != {expected}"
        )

actual_sha = hashlib.sha256(data).hexdigest()
if actual_sha != expected_sha:
    raise SystemExit(
        f"image SHA mismatch: expected {expected_sha}, got {actual_sha}"
    )

print(
    "PASS sbt-record-data-image "
    f"dimensions={width}x{height} colors={dict(colors)} "
    f"sha256={actual_sha}"
)
PY

VALIDATION_MANIFEST="${OUT_DIR}/sbt-record-data-validation.txt"
{
  echo "profile=sbt-record-data-exact-image"
  echo "spike_library=$(readlink -f "${SPIKE_BUILD}/libspike_main.so")"
  echo "driver_library=${DRIVER_LIB}"
  echo "app=${APP}"
  echo "app_sha256=$(sha256sum "${APP}" | awk '{print $1}')"
  echo "upstream_commit=${EXPECTED_UPSTREAM_COMMIT}"
  echo "glm_commit=${EXPECTED_GLM_COMMIT}"
  echo "output_ppm=${PPM}"
  echo "expected_sha256=${EXPECTED_PPM_SHA256}"
  echo "actual_sha256=$(sha256sum "${PPM}" | awk '{print $1}')"
  echo "comparison=byte-identical-and-stage-color-probes"
} >"${VALIDATION_MANIFEST}"

echo "manifest=${VALIDATION_MANIFEST}"
