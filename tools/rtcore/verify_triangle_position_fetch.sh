#!/usr/bin/env bash
set -euo pipefail

# Prove VK_KHR_ray_tracing_position_fetch through live Vulkan rendering on the
# Ventus Spike-backed path.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ENV_ROOT}/tools/rtcore/rt_profile.sh"
RT_PROFILE="$(ventus_rt_execution_profile)"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ENV_ROOT}/build/rt-workload/source}"
APP="${APP:-${ENV_ROOT}/build/rt-workload/build/bin/raytracingbasic}"
RUNNER="${RUNNER:-${ENV_ROOT}/tools/rtcore/run_full_app_spike.sh}"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/triangle_position_fetch}"
EXPECTED_BASELINE_SHA256="${EXPECTED_BASELINE_SHA256:-dd5b8d1bdad0ba7dc955f1eff2814327470d8755e5220c583491c44a55da191b}"
POSITION_FETCH_INDEX="${POSITION_FETCH_INDEX:-92}"

die() {
  echo "error: $*" >&2
  exit 1
}

for tool in python3 rg sha256sum; do
  command -v "${tool}" >/dev/null 2>&1 || die "required tool not found: ${tool}"
done

[[ -x "${APP}" ]] || die "raytracingbasic executable not found: ${APP}"
[[ -x "${RUNNER}" ]] || die "runner not found: ${RUNNER}"

APP_SOURCE="${WORKLOAD_DIR}/examples/raytracingbasic/raytracingbasic.cpp"
HIT_SOURCE="${WORKLOAD_DIR}/shaders/glsl/raytracingbasic/closesthit.rchit"
rg -q 'VK_KHR_RAY_TRACING_POSITION_FETCH_EXTENSION_NAME' "${APP_SOURCE}" ||
  die "generated workload does not enable the position-fetch extension"
rg -q 'VkPhysicalDeviceRayTracingPositionFetchFeaturesKHR' "${APP_SOURCE}" ||
  die "generated workload does not enable the position-fetch feature"
rg -q 'gl_HitTriangleVertexPositionsEXT' "${HIT_SOURCE}" ||
  die "generated closest-hit shader does not consume triangle positions"

run_case() {
  local name="$1"
  local custom_index="$2"
  APP="${APP}" \
  APP_NAME=raytracingbasic \
  WORKLOAD_DIR="${WORKLOAD_DIR}" \
  OUT_DIR="${OUT_DIR}/${name}" \
  RUN_1X1=0 \
  RUN_16X16=1 \
  RUN_160X96=0 \
  RUN_320X192=0 \
  VENTUS_RT_SAMPLE_INSTANCE_CUSTOM_INDEX="${custom_index}" \
  VENTUS_VK_RT_EXECUTION_PROFILE="${RT_PROFILE}" \
  "${RUNNER}"
}

run_case baseline 0
run_case position-fetch "${POSITION_FETCH_INDEX}"

BASELINE_PPM="${OUT_DIR}/baseline/16x16/raytracingbasic_spike.ppm"
POSITION_FETCH_PPM="${OUT_DIR}/position-fetch/16x16/raytracingbasic_spike.ppm"
[[ -f "${BASELINE_PPM}" ]] || die "baseline image not found: ${BASELINE_PPM}"
[[ -f "${POSITION_FETCH_PPM}" ]] ||
  die "position-fetch image not found: ${POSITION_FETCH_PPM}"

baseline_sha="$(sha256sum "${BASELINE_PPM}" | awk '{print $1}')"
[[ "${baseline_sha}" == "${EXPECTED_BASELINE_SHA256}" ]] ||
  die "disabled position fetch changed the baseline image: ${baseline_sha}"

python3 - "${BASELINE_PPM}" "${POSITION_FETCH_PPM}" "${POSITION_FETCH_INDEX}" <<'PY'
from collections import Counter
import hashlib
import sys

baseline_path, position_fetch_path, position_fetch_index = sys.argv[1:]
position_fetch_index = int(position_fetch_index, 0)


def load_ppm(path):
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

    if token() != b"P6":
        raise SystemExit(f"unsupported PPM format: {path}")
    width = int(token())
    height = int(token())
    if int(token()) != 255:
        raise SystemExit(f"unsupported PPM range: {path}")
    while pos < len(data) and data[pos] in b" \t\r\n":
        pos += 1
    payload = data[pos:]
    if len(payload) != width * height * 3:
        raise SystemExit(f"PPM payload size mismatch: {path}")
    pixels = [tuple(payload[i:i + 3]) for i in range(0, len(payload), 3)]
    return data, width, height, pixels


baseline_data, width, height, baseline = load_ppm(baseline_path)
position_data, position_width, position_height, position = load_ppm(
    position_fetch_path
)
if (position_width, position_height) != (width, height):
    raise SystemExit("baseline and position-fetch image dimensions differ")
if position_fetch_index != 0x5C:
    raise SystemExit(
        f"verifier expects custom index 0x5c, got {position_fetch_index:#x}"
    )

blue = (0, 0, 255)
red = (255, 0, 0)
colors = Counter(position)
changed = [i for i, pair in enumerate(zip(baseline, position)) if pair[0] != pair[1]]
if not changed:
    raise SystemExit("position fetch did not change any hit pixel")
if any(position[i] != blue for i in changed):
    raise SystemExit("a changed pixel did not carry the position-fetch success marker")
if colors[blue] != len(changed):
    raise SystemExit("position-fetch marker appeared outside changed hit pixels")
if colors[red] != 0:
    raise SystemExit(f"{colors[red]} hits reported fetched triangle-position mismatch")

center = (height // 2) * width + width // 2
if position[center] != blue:
    raise SystemExit(
        f"center hit did not observe fetched triangle positions: {position[center]}"
    )

print(
    "PASS triangle-position-fetch "
    f"dimensions={width}x{height} verified_hit_pixels={colors[blue]} "
    f"baseline_sha256={hashlib.sha256(baseline_data).hexdigest()} "
    f"position_fetch_sha256={hashlib.sha256(position_data).hexdigest()}"
)
PY
