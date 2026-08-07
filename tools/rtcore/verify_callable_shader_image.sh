#!/usr/bin/env bash
set -euo pipefail

# Prove callable-SBT selection and callableData return through the live Vulkan
# application, Ventus compiler, driver bridge, and Spike-backed image path.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ENV_ROOT}/tools/rtcore/rt_profile.sh"
RT_PROFILE="$(ventus_rt_execution_profile)"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ENV_ROOT}/build/rt-workload/source}"
APP="${APP:-${ENV_ROOT}/build/rt-workload/build/bin/raytracingcallable}"
RUNNER="${RUNNER:-${ENV_ROOT}/tools/rtcore/run_full_app_spike.sh}"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/callable_shader}"
EXPECTED_SHA256="${EXPECTED_SHA256:-d7bdabc9540254754933b8be074b54b6dc98ee48d326d2055a0b5b20a9c4f29c}"

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

HIT_SOURCE="${WORKLOAD_DIR}/shaders/glsl/raytracingcallable/closesthit.rchit"
rg -q 'executeCallableEXT\(gl_GeometryIndexEXT, 0\)' "${HIT_SOURCE}" ||
  die "closest-hit shader does not select callable records by geometry index"
rg -q 'callableDataEXT' "${HIT_SOURCE}" ||
  die "closest-hit shader does not declare outgoing callable data"

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
"${RUNNER}"

PPM="${OUT_DIR}/160x96/raytracingcallable_spike.ppm"
LOG="${OUT_DIR}/160x96/raytracingcallable.log"
[[ -f "${PPM}" ]] || die "callable image not found: ${PPM}"
[[ -f "${LOG}" ]] || die "callable log not found: ${LOG}"

rg -q 'stage=closest hit trace_ray=0 execute_callable=1' "${LOG}" ||
  die "SPIR-V frontend did not expose executeCallableEXT"
ABI_PROBE_LABEL='before vt_nir_lower_rt_payload'
rg -q "${ABI_PROBE_LABEL} stage=closest hit trace_ray=0 execute_callable=0" \
  "${LOG}" || die "Ventus callable lowering did not consume executeCallableEXT"
rg -q 'driver bridge callable SBT .* size=96 stride=32' "${LOG}" ||
  die "driver bridge did not upload the three-record callable SBT"
rg -q 'RT pipeline compiled .* stages=6 groups=6' "${LOG}" ||
  die "callable pipeline did not retain the expected stage/group layout"

actual_sha="$(sha256sum "${PPM}" | awk '{print $1}')"
[[ "${actual_sha}" == "${EXPECTED_SHA256}" ]] ||
  die "callable image hash mismatch: ${actual_sha}"

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
green = (0, 255, 0)
white = (255, 255, 255)
expected = Counter({
    background: 14976,
    black: 160,
    green: 128,
    white: 96,
})
colors = Counter(pixels)
if colors != expected:
    raise SystemExit(f"unexpected callable image colors: {colors}")

def zone(lo, hi):
    return Counter(
        pixels[y * width + x]
        for y in range(height)
        for x in range(lo, hi)
    )

left = zone(0, 70)
middle = zone(70, 91)
right = zone(91, width)
if left[white] != 64 or left[black] != 64 or left[green] != 0:
    raise SystemExit(f"callable SBT index 0 checker mismatch: {left}")
if middle[green] != 128 or middle[white] != 0 or middle[black] != 0:
    raise SystemExit(f"callable SBT index 1 solid-color mismatch: {middle}")
if right[white] != 32 or right[black] != 96 or right[green] != 0:
    raise SystemExit(f"callable SBT index 2 line-pattern mismatch: {right}")

print(
    "PASS callable-shader "
    f"dimensions={width}x{height} "
    "sbt_indices=0,1,2 "
    f"sha256={hashlib.sha256(open(path, 'rb').read()).hexdigest()}"
)
PY
