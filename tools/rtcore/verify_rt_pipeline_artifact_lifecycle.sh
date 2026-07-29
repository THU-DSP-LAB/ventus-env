#!/usr/bin/env bash
set -euo pipefail

# Verify that RT pipeline compilation fails closed and that generated
# executables remain valid only for the lifetime of their owning pipeline.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MESA_BUILD="${MESA_BUILD:-${ROOT_DIR}/mesa/build-ventus}"
DRIVER_LIB="${DRIVER_LIB:-${ROOT_DIR}/driver/build/driver/spike_device/libspike_driver.so}"
RUNNER="${RUNNER:-${ROOT_DIR}/tools/rtcore/run_full_app_spike.sh}"
OUT_BASE="${OUT_BASE:-/tmp/ventus-rt-pipeline-artifact-lifecycle}"
FAIL_OUT="${OUT_BASE}/failure"
CLEAN_OUT="${OUT_BASE}/cleanup"
RETAIN_OUT="${OUT_BASE}/retain"
EXPECTED_1X1_SHA256="8e1b9d83fa583f46b14a33fac42e5cd399afd312cda413d18f391d367b400f78"

die() {
  echo "error: $*" >&2
  exit 1
}

artifact_count() {
  find "$1" -maxdepth 1 -type f \
    \( -name 'ventus_vk_rt_*.ll' \
       -o -name 'ventus_vk_rt_*.ld' \
       -o -name 'ventus_vk_rt_*.o' \
       -o -name 'ventus_vk_rt_*.riscv' \) \
    -printf '.\n' |
    wc -l
}

run_1x1() {
  local profile="$1"
  local out_dir="$2"
  shift 2
  env \
    VENTUS_VK_RT_EXECUTION_PROFILE="${profile}" \
    MESA_BUILD="${MESA_BUILD}" \
    DRIVER_LIB="${DRIVER_LIB}" \
    OUT_DIR="${out_dir}" \
    RUN_1X1=1 \
    RUN_16X16=0 \
    RUN_160X96=0 \
    RUN_320X192=0 \
    "$@" \
    "${RUNNER}"
}

for tool in awk find rg sha256sum wc; do
  command -v "${tool}" >/dev/null 2>&1 ||
    die "required tool not found: ${tool}"
done
[[ -x "${RUNNER}" ]] || die "runner not executable: ${RUNNER}"
[[ -f "${DRIVER_LIB}" ]] || die "driver library not found: ${DRIVER_LIB}"
[[ -f "${MESA_BUILD}/src/ventus/vulkan/ventus_devenv_icd.x86_64.json" ]] ||
  die "Ventus ICD not found under ${MESA_BUILD}"

rm -rf -- "${OUT_BASE}"
mkdir -p "${OUT_BASE}"

if run_1x1 compat "${FAIL_OUT}" \
     VENTUS_VK_LLC=/bin/false \
     VENTUS_VK_RETAIN_SHADER_ARTIFACTS=0 \
     VENTUS_VK_DUMP_LLVM=0; then
  die "RT pipeline creation unexpectedly survived an llc failure"
fi

FAIL_LOG="${FAIL_OUT}/1x1/raytracingshadows.log"
FAIL_ARTIFACT_DIR="${FAIL_OUT}/1x1/elf"
[[ -f "${FAIL_LOG}" ]] || die "failure-injection log not found"
rg -q 'RT llc failed' "${FAIL_LOG}" ||
  die "failure-injection log does not contain the llc error"
if rg -q 'RT pipeline compiled' "${FAIL_LOG}"; then
  die "failed RT executable was reported as a compiled pipeline"
fi
[[ "$(artifact_count "${FAIL_ARTIFACT_DIR}")" == 0 ]] ||
  die "partial RT compiler artifacts survived failed pipeline creation"

for iteration in 1 2; do
  run_1x1 global "${CLEAN_OUT}" \
    VENTUS_VK_RETAIN_SHADER_ARTIFACTS=0 \
    VENTUS_VK_DUMP_LLVM=0
  CLEAN_PPM="${CLEAN_OUT}/1x1/raytracingshadows_spike.ppm"
  CLEAN_ARTIFACT_DIR="${CLEAN_OUT}/1x1/elf"
  CLEAN_SHA256="$(sha256sum "${CLEAN_PPM}" | awk '{print $1}')"
  [[ "${CLEAN_SHA256}" == "${EXPECTED_1X1_SHA256}" ]] ||
    die "cleanup run ${iteration} changed the frozen 1x1 image"
  [[ "$(artifact_count "${CLEAN_ARTIFACT_DIR}")" == 0 ]] ||
    die "pipeline-owned artifacts survived cleanup run ${iteration}"
done

run_1x1 global "${RETAIN_OUT}" \
  VENTUS_VK_RETAIN_SHADER_ARTIFACTS=1 \
  VENTUS_VK_DUMP_LLVM=0
RETAIN_ARTIFACT_DIR="${RETAIN_OUT}/1x1/elf"
for suffix in .ll .emit.ll .ld .o .riscv; do
  find "${RETAIN_ARTIFACT_DIR}" -maxdepth 1 -type f \
    -name "ventus_vk_rt_*${suffix}" -print -quit | rg -q . ||
    die "debug retention did not preserve ${suffix} artifacts"
done

echo "PASS rt-pipeline-artifact-lifecycle clean_count=0 retained_count=$(artifact_count "${RETAIN_ARTIFACT_DIR}")"
