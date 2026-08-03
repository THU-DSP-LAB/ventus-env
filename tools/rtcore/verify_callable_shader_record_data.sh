#!/usr/bin/env bash
set -euo pipefail

# Prove that callable shaderRecordEXT loads application bytes from the
# dynamically selected callable SBT record, not from the caller's SBT record.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ENV_ROOT}/tools/rtcore/rt_profile.sh"
RT_PROFILE="$(ventus_rt_execution_profile)"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ENV_ROOT}/build/rt-workload/source}"
APP="${APP:-${ENV_ROOT}/build/rt-workload/build/bin/raytracingcallable}"
RUNNER="${RUNNER:-${ENV_ROOT}/tools/rtcore/run_full_app_spike.sh}"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/callable_shader_record_data}"
EXPECTED_SHA256="${EXPECTED_SHA256:-1ff8532e2ba5dc7d39bb89720a66a9ccb355e8d5d30196b848dbb0ff1f54dd05}"

die() {
  echo "error: $*" >&2
  exit 1
}

for tool in python3 rg sha256sum; do
  command -v "${tool}" >/dev/null 2>&1 ||
    die "required tool not found: ${tool}"
done

[[ -x "${APP}" ]] || die "raytracingcallable executable not found: ${APP}"
[[ -x "${RUNNER}" ]] || die "runner not found: ${RUNNER}"

SHADER="${WORKLOAD_DIR}/shaders/glsl/raytracingcallable/callable_record_data.rcall"
rg -q 'layout\(shaderRecordEXT, std430\)' "${SHADER}" ||
  die "dedicated callable does not consume callable SBT record data"
rg -q 'uint recordId' "${SHADER}" ||
  die "dedicated callable does not decode the record selector"

VENTUS_CALLABLE_RECORD_DATA_ALT=1 \
APP="${APP}" \
APP_NAME=raytracingcallable \
WORKLOAD_DIR="${WORKLOAD_DIR}" \
OUT_DIR="${OUT_DIR}" \
RUN_1X1=0 \
RUN_16X16=0 \
RUN_160X96=1 \
RUN_320X192=0 \
VENTUS_VK_NIR_PROBE=1 \
VENTUS_VK_PROBE_LOG=1 \
VENTUS_VK_RT_EXECUTION_PROFILE="${RT_PROFILE}" \
"${RUNNER}"

PPM="${OUT_DIR}/160x96/raytracingcallable_spike.ppm"
LOG="${OUT_DIR}/160x96/raytracingcallable.log"
[[ -f "${PPM}" ]] || die "callable record-data image not found: ${PPM}"
[[ -f "${LOG}" ]] || die "callable record-data log not found: ${LOG}"

rg -q 'driver bridge callable SBT .* size=192 stride=64' "${LOG}" ||
  die "driver bridge did not preserve the 64-byte callable records"
if [[ "${RT_PROFILE}" == "global" ]]; then
  ABI_PROBE_LABEL='before global CPS ABI lowering'
else
  ABI_PROBE_LABEL='before vt_nir_lower_rt_payload'
fi
rg -q "${ABI_PROBE_LABEL} stage=closest hit trace_ray=0 execute_callable=0" \
  "${LOG}" || die "Ventus callable lowering did not consume executeCallableEXT"

actual_sha="$(sha256sum "${PPM}" | awk '{print $1}')"
if [[ -n "${EXPECTED_SHA256}" && "${actual_sha}" != "${EXPECTED_SHA256}" ]]; then
  die "callable record-data image hash mismatch: ${actual_sha}"
fi

python3 - "${PPM}" <<'PY'
from collections import Counter
import hashlib
import sys

path = sys.argv[1]
with open(path, "rb") as stream:
    if stream.readline().strip() != b"P6":
        raise SystemExit("unsupported PPM format")
    width, height = map(int, stream.readline().split())
    if int(stream.readline()) != 255:
        raise SystemExit("unsupported PPM range")
    data = stream.read()

if (width, height) != (160, 96):
    raise SystemExit(f"unexpected image dimensions: {width}x{height}")
if len(data) != width * height * 3:
    raise SystemExit("PPM payload size mismatch")

pixels = [tuple(data[i:i + 3]) for i in range(0, len(data), 3)]
background = (0, 0, 51)
black = (0, 0, 0)
red = (255, 0, 0)
blue = (0, 0, 255)
yellow = (255, 255, 0)
expected = Counter({
    background: 14976,
    red: 128,
    blue: 128,
    yellow: 128,
})
colors = Counter(pixels)
if colors != expected:
    raise SystemExit(f"unexpected callable record-data colors: {colors}")

def zone(lo, hi):
    return Counter(
        pixels[y * width + x]
        for y in range(height)
        for x in range(lo, hi)
    )

left = zone(0, 70)
middle = zone(70, 91)
right = zone(91, width)
if left[red] != 128 or left[black] != 0:
    raise SystemExit(f"callable record 0 red fill mismatch: {left}")
if middle[blue] != 128:
    raise SystemExit(f"callable record 1 blue fill mismatch: {middle}")
if right[yellow] != 128 or right[black] != 0:
    raise SystemExit(f"callable record 2 yellow fill mismatch: {right}")

print(
    "PASS callable-shader-record-data "
    f"dimensions={width}x{height} "
    "record_colors=red,blue,yellow "
    f"sha256={hashlib.sha256(open(path, 'rb').read()).hexdigest()}"
)
PY
