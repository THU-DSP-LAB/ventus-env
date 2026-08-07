#!/usr/bin/env bash
set -euo pipefail

# Prove affine instance matrices and object/world ray builtins through live
# Vulkan rendering on the Ventus Spike-backed path.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ENV_ROOT}/tools/rtcore/rt_profile.sh"
RT_PROFILE="$(ventus_rt_execution_profile)"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ENV_ROOT}/build/rt-workload/source}"
APP="${APP:-${ENV_ROOT}/build/rt-workload/build/bin/raytracingbasic}"
RUNNER="${RUNNER:-${ENV_ROOT}/tools/rtcore/run_full_app_spike.sh}"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/instance_transform_builtins}"
EXPECTED_BASELINE_SHA256="${EXPECTED_BASELINE_SHA256:-dd5b8d1bdad0ba7dc955f1eff2814327470d8755e5220c583491c44a55da191b}"

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
rg -q 'VENTUS_RT_SAMPLE_AFFINE_INSTANCE' "${APP_SOURCE}" ||
  die "generated workload does not expose the affine-instance control"
for builtin in \
  gl_ObjectToWorldEXT \
  gl_WorldToObjectEXT \
  gl_ObjectRayOriginEXT \
  gl_ObjectRayDirectionEXT \
  gl_WorldRayOriginEXT \
  gl_WorldRayDirectionEXT; do
  rg -q "${builtin}" "${HIT_SOURCE}" ||
    die "generated closest-hit shader does not consume ${builtin}"
done

run_case() {
  local name="$1"
  local affine="$2"
  local custom_index="$3"
  APP="${APP}" \
  APP_NAME=raytracingbasic \
  WORKLOAD_DIR="${WORKLOAD_DIR}" \
  OUT_DIR="${OUT_DIR}/${name}" \
  RUN_1X1=0 \
  RUN_16X16=1 \
  RUN_160X96=0 \
  RUN_320X192=0 \
  VENTUS_RT_SAMPLE_AFFINE_INSTANCE="${affine}" \
  VENTUS_RT_SAMPLE_INSTANCE_CUSTOM_INDEX="${custom_index}" \
  "${RUNNER}"
}

run_case baseline 0 0
run_case transformed 1 91

BASELINE_PPM="${OUT_DIR}/baseline/16x16/raytracingbasic_spike.ppm"
TRANSFORMED_PPM="${OUT_DIR}/transformed/16x16/raytracingbasic_spike.ppm"
[[ -f "${BASELINE_PPM}" ]] || die "baseline image not found: ${BASELINE_PPM}"
[[ -f "${TRANSFORMED_PPM}" ]] ||
  die "transformed image not found: ${TRANSFORMED_PPM}"

baseline_sha="$(sha256sum "${BASELINE_PPM}" | awk '{print $1}')"
[[ "${baseline_sha}" == "${EXPECTED_BASELINE_SHA256}" ]] ||
  die "disabled affine instance changed the baseline image: ${baseline_sha}"

python3 - "${BASELINE_PPM}" "${TRANSFORMED_PPM}" <<'PY'
from collections import Counter
import hashlib
import sys

baseline_path, transformed_path = sys.argv[1:]


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
transformed_data, transformed_width, transformed_height, transformed = load_ppm(
    transformed_path
)
if (transformed_width, transformed_height) != (width, height):
    raise SystemExit("baseline and transformed image dimensions differ")

green = (0, 255, 0)
red = (255, 0, 0)
colors = Counter(transformed)
if colors[green] == 0:
    raise SystemExit("no transformed hit observed all matrix and ray builtins")
if colors[red] != 0:
    raise SystemExit(f"{colors[red]} transformed hits reported builtin mismatch")
if baseline_data == transformed_data:
    raise SystemExit("affine instance did not change the rendered image")

center = (height // 2) * width + width // 2
if transformed[center] != green:
    raise SystemExit(
        f"center transformed hit did not pass builtin checks: {transformed[center]}"
    )

print(
    "PASS instance-transform-builtins "
    f"dimensions={width}x{height} verified_hit_pixels={colors[green]} "
    f"baseline_sha256={hashlib.sha256(baseline_data).hexdigest()} "
    f"transformed_sha256={hashlib.sha256(transformed_data).hexdigest()}"
)
PY
