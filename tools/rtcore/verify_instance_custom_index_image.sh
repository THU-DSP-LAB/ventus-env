#!/usr/bin/env bash
set -euo pipefail

# Prove that the application-provided Vulkan instance custom index remains
# distinct from Ventus' implementation instance ordinal through live rendering.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ENV_ROOT}/tools/rtcore/rt_profile.sh"
RT_PROFILE="$(ventus_rt_execution_profile)"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ENV_ROOT}/build/rt-workload/source}"
APP="${APP:-${ENV_ROOT}/build/rt-workload/build/bin/raytracingbasic}"
RUNNER="${RUNNER:-${ENV_ROOT}/tools/rtcore/run_full_app_spike.sh}"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/instance_custom_index}"
EXPECTED_BASELINE_SHA256="${EXPECTED_BASELINE_SHA256:-dd5b8d1bdad0ba7dc955f1eff2814327470d8755e5220c583491c44a55da191b}"
CUSTOM_INDEX="${CUSTOM_INDEX:-90}"

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
rg -q 'VENTUS_RT_SAMPLE_INSTANCE_CUSTOM_INDEX' "${APP_SOURCE}" ||
  die "generated workload does not expose the instance custom-index control"
rg -q 'gl_InstanceCustomIndexEXT' "${HIT_SOURCE}" ||
  die "generated closest-hit shader does not consume InstanceCustomIndex"

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
  "${RUNNER}"
}

run_case baseline 0
run_case custom "${CUSTOM_INDEX}"

BASELINE_PPM="${OUT_DIR}/baseline/16x16/raytracingbasic_spike.ppm"
CUSTOM_PPM="${OUT_DIR}/custom/16x16/raytracingbasic_spike.ppm"
[[ -f "${BASELINE_PPM}" ]] || die "baseline image not found: ${BASELINE_PPM}"
[[ -f "${CUSTOM_PPM}" ]] || die "custom-index image not found: ${CUSTOM_PPM}"

baseline_sha="$(sha256sum "${BASELINE_PPM}" | awk '{print $1}')"
[[ "${baseline_sha}" == "${EXPECTED_BASELINE_SHA256}" ]] ||
  die "default custom index changed the baseline image: ${baseline_sha}"

python3 - "${BASELINE_PPM}" "${CUSTOM_PPM}" "${CUSTOM_INDEX}" <<'PY'
from collections import Counter
import hashlib
import sys

baseline_path, custom_path, custom_index = sys.argv[1:]
custom_index = int(custom_index, 0)


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
    pixels = data[pos:]
    if len(pixels) != width * height * 3:
        raise SystemExit(f"PPM payload size mismatch: {path}")
    return data, width, height, [
        tuple(pixels[i:i + 3]) for i in range(0, len(pixels), 3)
    ]


baseline_data, width, height, baseline = load_ppm(baseline_path)
custom_data, custom_width, custom_height, custom = load_ppm(custom_path)
if (custom_width, custom_height) != (width, height):
    raise SystemExit("baseline and custom-index image dimensions differ")
if custom_index != 0x5A:
    raise SystemExit(f"verifier expects custom index 0x5a, got {custom_index:#x}")

magenta = (255, 0, 255)
changed = [i for i, pair in enumerate(zip(baseline, custom)) if pair[0] != pair[1]]
if not changed:
    raise SystemExit("custom index did not change any hit pixel")
if any(custom[i] != magenta for i in changed):
    raise SystemExit("a changed pixel did not carry the custom-index marker")
if Counter(custom)[magenta] != len(changed):
    raise SystemExit("custom-index marker appeared outside changed hit pixels")

center = (height // 2) * width + width // 2
if custom[center] != magenta:
    raise SystemExit(
        f"center hit did not observe custom index: {custom[center]} != {magenta}"
    )

print(
    "PASS instance-custom-index-image "
    f"dimensions={width}x{height} custom_index={custom_index:#x} "
    f"changed_hit_pixels={len(changed)} "
    f"baseline_sha256={hashlib.sha256(baseline_data).hexdigest()} "
    f"custom_sha256={hashlib.sha256(custom_data).hexdigest()}"
)
PY
