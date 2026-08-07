#!/usr/bin/env bash
set -euo pipefail

# Fail-closed compatibility gate for the first Spike RTcore-model migration.
# Every source, tool and runtime library comes from this ventus-env checkout;
# the generated PPM must match the frozen known-good image hash exactly.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SPIKE_BUILD="${SPIKE_BUILD:-${ENV_ROOT}/spike/build}"

MESA_BUILD="${MESA_BUILD:-${ENV_ROOT}/mesa/build-ventus}"
LLVM_BUILD="${LLVM_BUILD:-${ENV_ROOT}/llvm/build}"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ENV_ROOT}/build/rt-workload/source}"
APP="${APP:-${ENV_ROOT}/build/rt-workload/build/bin/raytracingshadows}"
RUNNER="${RUNNER:-${ENV_ROOT}/tools/rtcore/run_full_app_spike.sh}"
ICD="${ICD:-${MESA_BUILD}/src/ventus/vulkan/ventus_devenv_icd.x86_64.json}"
DRIVER_LIB="${DRIVER_LIB:-${ENV_ROOT}/driver/build/driver/spike_device/libspike_driver.so}"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/compat_p1_exact_image}"

EXPECTED_PPM_SHA256="${EXPECTED_PPM_SHA256:-f43328945bdeb0dda69b3cc5612212170e5596456450184acfa627db8d5d1212}"
EXPECTED_UPSTREAM_COMMIT="3b843fbf667a89a1cfcc64405e9fc6f9018e03b4"
EXPECTED_ASSET_COMMIT="a27c0e584434d59b7c7a714e9180eefca6f0ec4b"
EXPECTED_GLM_COMMIT="1ad55c5016339b83b7eec98c31007e0aee57d2bf"

CLOSEST_HIT_SOURCE="${CLOSEST_HIT_SOURCE:-${WORKLOAD_DIR}/shaders/glsl/raytracingshadows/closesthit.rchit}"
SHADOW_MISS_SOURCE="${SHADOW_MISS_SOURCE:-${WORKLOAD_DIR}/shaders/glsl/raytracingshadows/shadow.rmiss}"
UPSTREAM_DIR="${UPSTREAM_DIR:-${ENV_ROOT}/workloads/rt/SaschaWillems_Vulkan}"
ASSET_CACHE="${ASSET_CACHE:-${ENV_ROOT}/build/rt-workload/cache/Vulkan-Assets}"

die() {
  echo "error: $*" >&2
  exit 1
}

require_file() {
  [[ -f "$1" ]] || die "required file not found: $1"
}

require_executable() {
  [[ -x "$1" ]] || die "required executable not found: $1"
}

for tool in awk git ldd python3 readlink rg sha256sum; do
  command -v "${tool}" >/dev/null 2>&1 || die "required tool not found: ${tool}"
done

require_file "${SPIKE_BUILD}/config.h"
require_file "${SPIKE_BUILD}/libspike_main.so"
require_executable "${RUNNER}"
require_executable "${APP}"
require_file "${ICD}"
require_file "${DRIVER_LIB}"
require_file "${CLOSEST_HIT_SOURCE}"
require_file "${SHADOW_MISS_SOURCE}"

rg -q '^#define RISCV_ENABLE_COMMITLOG' "${SPIKE_BUILD}/config.h" ||
  die "Spike must be configured with --enable-commitlog"

[[ "$(git -C "${UPSTREAM_DIR}" rev-parse HEAD)" == "${EXPECTED_UPSTREAM_COMMIT}" ]] ||
  die "upstream workload is not pinned to ${EXPECTED_UPSTREAM_COMMIT}"
[[ "$(git -C "${UPSTREAM_DIR}/external/glm" rev-parse HEAD)" == "${EXPECTED_GLM_COMMIT}" ]] ||
  die "workload GLM is not pinned to ${EXPECTED_GLM_COMMIT}"
[[ "$(git -C "${ASSET_CACHE}" rev-parse HEAD)" == "${EXPECTED_ASSET_COMMIT}" ]] ||
  die "asset cache is not pinned to ${EXPECTED_ASSET_COMMIT}"

if rg -n '/home/guanys' \
  "${RUNNER}" \
  "${ENV_ROOT}/workloads/rt/prepare.sh" \
  "${ENV_ROOT}/mesa/src/ventus/vulkan/vtvk_pipeline_rt.cpp" \
  "${ENV_ROOT}/mesa/src/ventus/vulkan/vtvk_driver_bridge.cpp"; then
  die "runtime path still contains an external checkout dependency"
fi

rg -q 'traceRayEXT' "${CLOSEST_HIT_SOURCE}" ||
  die "closest-hit shader no longer launches the shadow ray"
rg -q 'gl_RayFlagsTerminateOnFirstHitEXT' "${CLOSEST_HIT_SOURCE}" ||
  die "shadow ray no longer uses terminate-on-first-hit"
rg -q 'shadowed[[:space:]]*=[[:space:]]*false' "${SHADOW_MISS_SOURCE}" ||
  die "shadow miss shader no longer clears the shadow payload"

resolved_spike="$(
  LD_LIBRARY_PATH="${SPIKE_BUILD}" ldd "${DRIVER_LIB}" |
    awk '$1 == "libspike_main.so" {print $3; exit}'
)"
[[ -n "${resolved_spike}" ]] || die "driver did not resolve libspike_main.so"
resolved_spike="$(readlink -f "${resolved_spike}")"
expected_spike="$(readlink -f "${SPIKE_BUILD}/libspike_main.so")"
[[ "${resolved_spike}" == "${expected_spike}" ]] ||
  die "driver resolved the wrong Spike library: ${resolved_spike}"

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
CCACHE_DIR="${CCACHE_DIR:-/tmp/ventus-rtcore-p1-ccache}" \
XDG_CACHE_HOME="${XDG_CACHE_HOME:-/tmp/ventus-rtcore-p1-cache}" \
MESA_SHADER_CACHE_DISABLE=true \
VENTUS_SPIKE_LOG="${VENTUS_SPIKE_LOG:-0}" \
"${RUNNER}"

PPM="${OUT_DIR}/160x96/raytracingshadows_spike.ppm"
require_file "${PPM}"

python3 - "${PPM}" "${EXPECTED_PPM_SHA256}" <<'PY'
import hashlib
import sys

ppm_path, expected_sha = sys.argv[1:]


def parse_p6(path):
    data = open(path, "rb").read()
    pos = 0

    def token():
        nonlocal pos
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
    if magic != b"P6" or max_value != 255:
        raise SystemExit(f"unsupported PPM header in {path}")
    while pos < len(data) and data[pos] in b" \t\r\n":
        pos += 1
    pixels = data[pos:]
    expected_bytes = width * height * 3
    if len(pixels) != expected_bytes:
        raise SystemExit(
            f"payload size mismatch in {path}: {len(pixels)} != {expected_bytes}"
        )
    return data, width, height, pixels


data, width, height, pixels = parse_p6(ppm_path)
actual_sha = hashlib.sha256(data).hexdigest()
if (width, height) != (160, 96):
    raise SystemExit(f"unexpected image dimensions: {width}x{height}")
if actual_sha != expected_sha:
    raise SystemExit(
        f"image SHA mismatch: expected {expected_sha}, got {actual_sha}"
    )

colors = [tuple(pixels[i:i + 3]) for i in range(0, len(pixels), 3)]
center = colors[(height // 2) * width + width // 2]
corners = [colors[0], colors[width - 1], colors[-width], colors[-1]]
print(
    "PASS exact-image "
    f"dimensions={width}x{height} colors={len(set(colors))} "
    f"center={center} corners={corners} sha256={actual_sha}"
)
PY

VALIDATION_MANIFEST="${OUT_DIR}/compat-validation.txt"
{
  echo "profile=compat-p1-exact-image"
  echo "spike_build=${SPIKE_BUILD}"
  echo "spike_library=$(readlink -f "${SPIKE_BUILD}/libspike_main.so")"
  echo "driver_library=${DRIVER_LIB}"
  echo "app=${APP}"
  echo "app_sha256=$(sha256sum "${APP}" | awk '{print $1}')"
  echo "driver_sha256=$(sha256sum "${DRIVER_LIB}" | awk '{print $1}')"
  echo "upstream_commit=${EXPECTED_UPSTREAM_COMMIT}"
  echo "glm_commit=${EXPECTED_GLM_COMMIT}"
  echo "asset_commit=${EXPECTED_ASSET_COMMIT}"
  echo "output_ppm=${PPM}"
  echo "expected_sha256=${EXPECTED_PPM_SHA256}"
  echo "actual_sha256=$(sha256sum "${PPM}" | awk '{print $1}')"
  echo "comparison=byte-identical"
} >"${VALIDATION_MANIFEST}"

echo "manifest=${VALIDATION_MANIFEST}"
