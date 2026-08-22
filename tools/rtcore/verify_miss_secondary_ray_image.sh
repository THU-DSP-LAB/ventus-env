#!/usr/bin/env bash
set -euo pipefail

# Prove that an independently selected miss shader can launch a child ray,
# resume, and update the parent payload through the V5 continuation stack.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ENV_ROOT}/build/rt-workload/source}"
APP="${APP:-${ENV_ROOT}/build/rt-workload/build/bin/raytracingshadows}"
RUNNER="${RUNNER:-${ENV_ROOT}/tools/rtcore/run_full_app_spike.sh}"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/miss_secondary_ray}"
LLVM_OBJDUMP="${LLVM_OBJDUMP:-${ENV_ROOT}/llvm/build/bin/llvm-objdump}"
EXPECTED_SHA256="${EXPECTED_SHA256:-87d66fa972ec6af0d9cdab37bd3b1dd096b0b4f5ac731e23cfd94f05ee3cbcc0}"

die() {
  echo "error: $*" >&2
  exit 1
}

for tool in find python3 readelf rg sha256sum; do
  command -v "${tool}" >/dev/null 2>&1 ||
    die "required tool not found: ${tool}"
done
[[ -x "${APP}" ]] || die "raytracingshadows executable not found: ${APP}"
[[ -x "${RUNNER}" ]] || die "runner not found: ${RUNNER}"
[[ -x "${LLVM_OBJDUMP}" ]] || die "llvm-objdump not found: ${LLVM_OBJDUMP}"

MISS_SOURCE="${WORKLOAD_DIR}/shaders/glsl/raytracingshadows/miss_secondary.rmiss"
rg -q 'traceRayEXT' "${MISS_SOURCE}" ||
  die "secondary-ray miss shader no longer contains traceRayEXT"
rg -q 'gl_RayFlagsSkipClosestHitShaderEXT' "${MISS_SOURCE}" ||
  die "secondary-ray probe no longer isolates child traversal outcome"

VENTUS_MISS_SECONDARY=1 \
VENTUS_VK_RT_ABI_V5=1 \
VENTUS_VK_RT_SBT_MULTI_ENTRY=1 \
VENTUS_VK_RETAIN_SHADER_ARTIFACTS=1 \
APP="${APP}" \
APP_NAME=raytracingshadows \
WORKLOAD_DIR="${WORKLOAD_DIR}" \
OUT_DIR="${OUT_DIR}" \
RUN_1X1=0 \
RUN_16X16=1 \
RUN_160X96=0 \
RUN_320X192=0 \
VENTUS_VK_NIR_PROBE=1 \
VENTUS_VK_PROBE_LOG=1 \
"${RUNNER}"

PPM="${OUT_DIR}/16x16/raytracingshadows_spike.ppm"
LOG="${OUT_DIR}/16x16/raytracingshadows.log"
[[ -f "${PPM}" ]] || die "miss-secondary image not found: ${PPM}"
[[ -f "${LOG}" ]] || die "miss-secondary log not found: ${LOG}"

rg -q 'NIR after spirv_to_nir stage=miss trace_ray=1' "${LOG}" ||
  die "frontend did not expose miss-originated traceRayEXT"
rg -q 'inferred RT payload location 0 for unlocated interface hitValue' \
  "${LOG}" || die "cross-stage payload location inference did not run"
for symbol in vt_miss_g1 vt_miss_g2 vt_closest_hit_g3; do
  rg -q "RT entry_ref=.*symbol=${symbol} target=0x[1-9a-f]" "${LOG}" ||
    die "SBT-visible terminal entry was not resolved: ${symbol}"
done

mapfile -t rt_elfs < <(find "${OUT_DIR}/16x16/elf" -maxdepth 1 -type f -name '*.riscv')
[[ "${#rt_elfs[@]}" == 1 ]] ||
  die "expected one miss-secondary RT ELF, found ${#rt_elfs[@]}"
for symbol in vt_miss_g1_trace_depth2 vt_miss_g2_trace_depth2 \
              vt_closest_hit_g3_trace_depth2; do
  readelf -sW "${rt_elfs[0]}" | rg -q "[[:space:]]${symbol}$" ||
    die "bounded trace-depth entry is missing: ${symbol}"
done

disassembly="${OUT_DIR}/miss-secondary.dis"
"${LLVM_OBJDUMP}" -d "${rt_elfs[0]}" >"${disassembly}"
traverse_count="$(rg -c 'vt\.rt\.traverse' "${disassembly}" || true)"
release_count="$(rg -c 'vt\.rt\.release' "${disassembly}" || true)"
[[ "${traverse_count}" -ge 3 && "${release_count}" -ge 2 ]] ||
  die "ELF lacks root and independent-stage trace paths: traverse=${traverse_count} release=${release_count}"

actual_sha="$(sha256sum "${PPM}" | awk '{print $1}')"
[[ "${actual_sha}" == "${EXPECTED_SHA256}" ]] ||
  die "miss-secondary image hash mismatch: ${actual_sha}"

python3 - "${PPM}" <<'PY'
from collections import Counter
import sys

with open(sys.argv[1], "rb") as stream:
    if stream.readline().strip() != b"P6":
        raise SystemExit("unsupported PPM format")
    width, height = map(int, stream.readline().split())
    if int(stream.readline()) != 255:
        raise SystemExit("unsupported PPM range")
    data = stream.read()
if (width, height) != (16, 16) or len(data) != width * height * 3:
    raise SystemExit("miss-secondary PPM dimensions or payload are invalid")
colors = Counter(tuple(data[i:i + 3]) for i in range(0, len(data), 3))
if colors[(102, 0, 102)] != 135:
    raise SystemExit(f"child-hit marker count is not stable: {colors}")
PY

echo "PASS miss-secondary-ray traverse=${traverse_count} release=${release_count} sha256=${actual_sha}"
