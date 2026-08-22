#!/usr/bin/env bash
set -euo pipefail

# Prove a two-live-frame Callable chain through the V5 same-ELF path:
# closest-hit -> Callable group 4 -> Callable group 5.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ENV_ROOT}/build/rt-workload/source}"
APP="${APP:-${ENV_ROOT}/build/rt-workload/build/bin/raytracingcallable}"
RUNNER="${RUNNER:-${ENV_ROOT}/tools/rtcore/run_full_app_spike.sh}"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/nested_callable_shader}"
LLVM_OBJDUMP="${LLVM_OBJDUMP:-${ENV_ROOT}/llvm/build/bin/llvm-objdump}"
EXPECTED_SHA256="${EXPECTED_SHA256:-159a95d4b177fd7d985abd7abbdcceefd8af18ab43a9bdec88476c657478bbc6}"

die() {
  echo "error: $*" >&2
  exit 1
}

for tool in find python3 readelf rg sha256sum; do
  command -v "${tool}" >/dev/null 2>&1 ||
    die "required tool not found: ${tool}"
done

[[ -x "${APP}" ]] || die "raytracingcallable executable not found: ${APP}"
[[ -x "${RUNNER}" ]] || die "runner not found: ${RUNNER}"
[[ -x "${LLVM_OBJDUMP}" ]] || die "llvm-objdump not found: ${LLVM_OBJDUMP}"

NESTED_SHADER="${WORKLOAD_DIR}/shaders/glsl/raytracingcallable/callable_nested.rcall"
LEAF_SHADER="${WORKLOAD_DIR}/shaders/glsl/raytracingcallable/callable_nested_leaf.rcall"
rg -q 'executeCallableEXT\(2, 1\)' "${NESTED_SHADER}" ||
  die "nested Callable does not select the leaf SBT record"
rg -q 'callableDataInEXT.*nestedColor' "${LEAF_SHADER}" ||
  die "nested Callable leaf does not consume the child call-data location"

VENTUS_CALLABLE_NESTED=1 \
VENTUS_VK_RT_ABI_V5=1 \
VENTUS_VK_RT_SBT_MULTI_ENTRY=1 \
VENTUS_VK_RETAIN_SHADER_ARTIFACTS=1 \
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
[[ -f "${PPM}" ]] || die "nested Callable image not found: ${PPM}"
[[ -f "${LOG}" ]] || die "nested Callable log not found: ${LOG}"

[[ "$(rg -c 'NIR after spirv_to_nir stage=callable .*execute_callable=1' "${LOG}")" == 1 ]] ||
  die "frontend did not expose exactly one nested executeCallableEXT"
if rg -q 'NIR before vt_nir_lower_rt_payload stage=callable .*execute_callable=[1-9]' "${LOG}"; then
  die "nested executeCallableEXT survived the bounded entry lowering"
fi
for symbol in vt_callable_g3 vt_callable_g4 vt_callable_g5; do
  rg -q "RT entry_ref=.*symbol=${symbol} target=0x[1-9a-f]" "${LOG}" ||
    die "SBT-visible Callable entry was not resolved: ${symbol}"
done

mapfile -t rt_elfs < <(find "${OUT_DIR}/160x96/elf" -maxdepth 1 -type f -name '*.riscv')
[[ "${#rt_elfs[@]}" == 1 ]] ||
  die "expected one nested Callable RT ELF, found ${#rt_elfs[@]}"
for symbol in vt_callable_g3_depth2 vt_callable_g4_depth2 vt_callable_g5_depth2; do
  readelf -sW "${rt_elfs[0]}" | rg -q "[[:space:]]${symbol}$" ||
    die "bounded depth-2 Callable entry is missing: ${symbol}"
done

root_dis="${OUT_DIR}/callable-root.dis"
guard_dis="${OUT_DIR}/callable-depth2-guard.dis"
"${LLVM_OBJDUMP}" -d --disassemble-symbols=vt_callable_g4 \
  "${rt_elfs[0]}" >"${root_dis}"
"${LLVM_OBJDUMP}" -d --disassemble-symbols=vt_callable_g4_depth2 \
  "${rt_elfs[0]}" >"${guard_dis}"
rg -q '[[:space:]]jalr[[:space:]]' "${root_dis}" ||
  die "depth-1 Callable does not contain a real nested entry call"
if rg -q '[[:space:]]jalr[[:space:]]' "${guard_dis}"; then
  die "depth-2 Callable overflow guard still calls another entry"
fi

actual_sha="$(sha256sum "${PPM}" | awk '{print $1}')"
[[ "${actual_sha}" == "${EXPECTED_SHA256}" ]] ||
  die "nested Callable image hash mismatch: ${actual_sha}"

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

if (width, height) != (160, 96) or len(data) != width * height * 3:
    raise SystemExit("nested Callable PPM dimensions or payload are invalid")

colors = Counter(tuple(data[i:i + 3]) for i in range(0, len(data), 3))
expected = Counter({
    (0, 0, 51): 14976,
    (255, 0, 255): 256,
    (255, 255, 255): 64,
    (0, 0, 0): 64,
})
if colors != expected:
    raise SystemExit(f"unexpected nested Callable colors: {colors}")
PY

echo "PASS nested-callable-shader depth=2 sha256=${actual_sha} elf=${rt_elfs[0]}"
