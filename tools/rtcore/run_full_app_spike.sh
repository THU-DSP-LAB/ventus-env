#!/usr/bin/env bash
set -euo pipefail

# Run a repository-local Vulkan RT workload through the Mesa Ventus ICD and
# the selected Spike build. All tool and library paths are explicit so Mesa
# cannot fall back to another checkout.

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
source "${ROOT_DIR}/tools/rtcore/rt_profile.sh"
RT_EXECUTION_PROFILE="$(ventus_rt_execution_profile)"
MESA_BUILD="${MESA_BUILD:-${ROOT_DIR}/mesa/build-ventus}"
LLVM_BUILD="${LLVM_BUILD:-${ROOT_DIR}/llvm/build}"
SPIKE_BUILD="${SPIKE_BUILD:-${ROOT_DIR}/spike/build}"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ROOT_DIR}/build/rt-workload/source}"
APP="${APP:-${ROOT_DIR}/build/rt-workload/build/bin/raytracingshadows}"
APP_NAME="${APP_NAME:-$(basename "${APP}")}"
OUT_DIR="${OUT_DIR:-${ROOT_DIR}/artifacts/rtcore-spike/ventus_${APP_NAME}_spike}"
ICD="${ICD:-${MESA_BUILD}/src/ventus/vulkan/ventus_devenv_icd.x86_64.json}"
VENTUS_INSTALL_PREFIX="${VENTUS_INSTALL_PREFIX:-${ROOT_DIR}/install}"
REQUIRED_ASSET="${REQUIRED_ASSET:-}"

if [[ -z "${REQUIRED_ASSET}" && "${APP_NAME}" == "raytracingshadows" ]]; then
  REQUIRED_ASSET="${WORKLOAD_DIR}/assets/models/vulkanscene_shadow.gltf"
fi

first_file() {
  local candidate
  for candidate in "$@"; do
    if [[ -f "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  return 1
}

first_executable() {
  local candidate
  for candidate in "$@"; do
    if [[ -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  return 1
}

DRIVER_LIB="${DRIVER_LIB:-${VENTUS_VK_DRIVER_LIB:-$(first_file \
  "${VENTUS_INSTALL_PREFIX}/lib/libspike_driver.so" \
  "${ROOT_DIR}/driver/build/driver/spike_device/libspike_driver.so" || true)}}"
LLC="${VENTUS_VK_LLC:-$(first_executable \
  "${VENTUS_INSTALL_PREFIX}/bin/llc" "${LLVM_BUILD}/bin/llc" || true)}"
LD_LLD="${VENTUS_VK_LD_LLD:-$(first_executable \
  "${VENTUS_INSTALL_PREFIX}/bin/ld.lld" "${LLVM_BUILD}/bin/ld.lld" || true)}"
LLVM_NM="${VENTUS_VK_LLVM_NM:-$(first_executable \
  "${VENTUS_INSTALL_PREFIX}/bin/llvm-nm" "${LLVM_BUILD}/bin/llvm-nm" || true)}"
CRT0="${VENTUS_VK_CRT0:-$(first_file \
  "${VENTUS_INSTALL_PREFIX}/lib/crt0.o" \
  "${ROOT_DIR}/llvm/build-rt-libclc/lib/crt0.o" \
  "${ROOT_DIR}/llvm/build-rt-libclc/riscv32/lib/CMakeFiles/ctr0_obj.dir/crt0.S.o" \
  "${ROOT_DIR}/llvm/build-libclc/lib/crt0.o" \
  "${ROOT_DIR}/llvm/build-libclc/riscv32/lib/CMakeFiles/ctr0_obj.dir/crt0.S.o" || true)}"
LLVM_TOOL_LIB_DIR="${VENTUS_VK_LLVM_TOOL_LIB_DIR:-${LLVM_BUILD}/lib}"

RUN_1X1="${RUN_1X1:-0}"
RUN_16X16="${RUN_16X16:-0}"
RUN_160X96="${RUN_160X96:-1}"
RUN_320X192="${RUN_320X192:-0}"

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

for tool in python3 readlink sha256sum; do
  command -v "${tool}" >/dev/null 2>&1 || die "required tool not found: ${tool}"
done

require_file "${SPIKE_BUILD}/libspike_main.so"
require_executable "${APP}"
require_file "${ICD}"
require_file "${DRIVER_LIB}"
require_executable "${LLC}"
require_executable "${LD_LLD}"
require_executable "${LLVM_NM}"
require_file "${CRT0}"
if [[ -n "${REQUIRED_ASSET}" ]]; then
  require_file "${REQUIRED_ASSET}"
fi

mkdir -p "${OUT_DIR}"
MANIFEST="${OUT_DIR}/manifest.txt"
: >"${MANIFEST}"

run_case() {
  local label="$1"
  local width="$2"
  local height="$3"
  local case_dir="${OUT_DIR}/${label}"
  local elf_dir="${case_dir}/elf"
  local ppm="${case_dir}/${APP_NAME}_spike.ppm"
  local png="${case_dir}/${APP_NAME}_spike.png"
  local log="${case_dir}/${APP_NAME}.log"
  local -a app_command=(
    "${APP}"
    --benchmark
    --benchmarkframes 1
    --width "${width}"
    --height "${height}"
  )

  rm -rf -- "${case_dir}"
  mkdir -p "${elf_dir}"

  (
    cd "${WORKLOAD_DIR}"
    export VK_DRIVER_FILES="${ICD}"
    export VK_ICD_FILENAMES="${ICD}"
    export VENTUS_VK_DRIVER_BACKEND=spike
    export VENTUS_VK_DRIVER_BRIDGE=1
    export VENTUS_VK_EMIT_ELF=1
    export VENTUS_VK_DRIVER_LIB="${DRIVER_LIB}"
    export VENTUS_VK_DUMP_IMAGE="${ppm}"
    export VENTUS_VK_SHADER_ARTIFACT_DIR="${elf_dir}"
    export VENTUS_VK_LLC="${LLC}"
    export VENTUS_VK_LD_LLD="${LD_LLD}"
    export VENTUS_VK_LLVM_NM="${LLVM_NM}"
    export VENTUS_VK_CRT0="${CRT0}"
    export VENTUS_VK_LLVM_TOOL_LIB_DIR="${LLVM_TOOL_LIB_DIR}"
    # compat/global remains a test-harness selector while the ICD exposes
    # execution policy only through the per-pipeline private pNext contract.
    export VENTUS_VK_INTERNAL_RT_ORACLE_PROFILE="${RT_EXECUTION_PROFILE}"
    unset VENTUS_VK_RT_EXECUTION_PROFILE
    unset VENTUS_VK_RT_WAVEFRONT_GLOBAL
    unset VENTUS_VK_RT_WAVEFRONT_MIRROR
    export VENTUS_SPIKE_LOG="${VENTUS_SPIKE_LOG:-0}"
    export LD_LIBRARY_PATH="${SPIKE_BUILD}:$(dirname "${DRIVER_LIB}"):${LLVM_TOOL_LIB_DIR}:${LD_LIBRARY_PATH:-}"
    "${app_command[@]}"
  ) >"${log}" 2>&1

  require_file "${ppm}"
  python3 - "${ppm}" "${width}" "${height}" <<'PY'
import sys

path, expected_width, expected_height = sys.argv[1:]
with open(path, "rb") as stream:
    magic = stream.readline().strip()
    dimensions = stream.readline().strip()
    while dimensions.startswith(b"#"):
        dimensions = stream.readline().strip()
    width, height = map(int, dimensions.split())
    max_value = int(stream.readline().strip())
    pixels = stream.read()

expected = (int(expected_width), int(expected_height), 255)
if magic != b"P6" or (width, height, max_value) != expected:
    raise SystemExit(f"unexpected PPM header in {path}")
if len(pixels) != width * height * 3:
    raise SystemExit(f"unexpected PPM payload size in {path}")
PY

  if command -v pnmtopng >/dev/null 2>&1; then
    pnmtopng "${ppm}" >"${png}"
  fi

  {
    echo "[${label}]"
    echo "width=${width}"
    echo "height=${height}"
    echo "ppm=${ppm}"
    echo "sha256=$(sha256sum "${ppm}" | awk '{print $1}')"
    echo "log=${log}"
    echo "elf_dir=${elf_dir}"
    echo
  } >>"${MANIFEST}"

  echo "PASS ${label} ppm=${ppm}"
}

{
  echo "root=${ROOT_DIR}"
  echo "mesa_build=${MESA_BUILD}"
  echo "spike_library=$(readlink -f "${SPIKE_BUILD}/libspike_main.so")"
  echo "driver_library=$(readlink -f "${DRIVER_LIB}")"
  echo "workload=${WORKLOAD_DIR}"
  echo "app=${APP}"
  echo "app_name=${APP_NAME}"
  echo "rt_execution_profile=${RT_EXECUTION_PROFILE}"
  echo "app_sha256=$(sha256sum "${APP}" | awk '{print $1}')"
  echo "icd=${ICD}"
  echo
} >"${MANIFEST}"

if [[ "${RUN_1X1}" != 0 ]]; then
  run_case 1x1 1 1
fi
if [[ "${RUN_16X16}" != 0 ]]; then
  run_case 16x16 16 16
fi
if [[ "${RUN_160X96}" != 0 ]]; then
  run_case 160x96 160 96
fi
if [[ "${RUN_320X192}" != 0 ]]; then
  run_case 320x192 320 192
fi

echo "manifest=${MANIFEST}"
