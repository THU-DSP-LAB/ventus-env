#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MESA_BUILD="${MESA_BUILD:-${ROOT_DIR}/mesa/build-ventus}"
ICD="${ICD:-${MESA_BUILD}/src/ventus/vulkan/ventus_devenv_icd.x86_64.json}"
DRIVER_LIB="${DRIVER_LIB:-${ROOT_DIR}/driver/build/driver/spike_device/libspike_driver.so}"
SPIKE_BUILD="${SPIKE_BUILD:-${ROOT_DIR}/spike/build}"
SOURCE="${ROOT_DIR}/tools/rtcore/multi_geometry_blas_probe.c"
CODEC_SOURCE="${ROOT_DIR}/spike/tests/ventus_rt_vtas_v2_codec_test.cc"
OUT_DIR="${OUT_DIR:-/tmp/ventus-multi-geometry-blas}"
CC="${CC:-cc}"
CXX="${CXX:-c++}"
PKG_CONFIG="${PKG_CONFIG:-pkg-config}"

die() {
  echo "error: $*" >&2
  exit 1
}

for tool in "${CC}" "${CXX}" "${PKG_CONFIG}" rg; do
  command -v "${tool}" >/dev/null 2>&1 ||
    die "required tool not found: ${tool}"
done
[[ -f "${ICD}" ]] || die "Ventus ICD not found: ${ICD}"
[[ -f "${DRIVER_LIB}" ]] || die "Ventus Spike Driver not found: ${DRIVER_LIB}"
[[ -f "${SOURCE}" ]] || die "probe source not found: ${SOURCE}"
[[ -f "${CODEC_SOURCE}" ]] || die "codec source not found: ${CODEC_SOURCE}"
[[ -n "${OUT_DIR}" && "${OUT_DIR}" != / ]] ||
  die "unsafe OUT_DIR: ${OUT_DIR}"

read -r -a vulkan_cflags <<<"$("${PKG_CONFIG}" --cflags vulkan)"
read -r -a vulkan_libs <<<"$("${PKG_CONFIG}" --libs vulkan)"

rm -rf -- "${OUT_DIR}"
mkdir -p "${OUT_DIR}"

"${CC}" -std=c11 -Wall -Wextra -Werror \
  "${vulkan_cflags[@]}" \
  -I"${ROOT_DIR}/mesa/src" \
  "${SOURCE}" \
  -o "${OUT_DIR}/multi_geometry_blas_probe" \
  "${vulkan_libs[@]}" -lm

"${CXX}" -std=c++17 -Wall -Wextra -Werror \
  -I"${ROOT_DIR}/spike/riscv" \
  "${CODEC_SOURCE}" \
  -o "${OUT_DIR}/ventus_rt_vtas_v2_codec_test"

FIXTURE="${OUT_DIR}/vtas-v2-blas.bin"
probe_output="$(
  env \
    VK_ICD_FILENAMES="${ICD}" \
    VENTUS_VK_DRIVER_LIB="${DRIVER_LIB}" \
    VENTUS_VTAS_V2_FIXTURE_OUT="${FIXTURE}" \
    LD_LIBRARY_PATH="${SPIKE_BUILD}:$(dirname "${DRIVER_LIB}"):${LD_LIBRARY_PATH:-}" \
    "${OUT_DIR}/multi_geometry_blas_probe"
)"
printf '%s\n' "${probe_output}"
rg -q '^PASS multi-geometry-blas ' <<<"${probe_output}" ||
  die "multi-geometry BLAS probe did not report PASS"
[[ -s "${FIXTURE}" ]] || die "VTAS V2 producer did not emit a fixture"
"${OUT_DIR}/ventus_rt_vtas_v2_codec_test" "${FIXTURE}"
echo "PASS vtas-v2-producer-consumer fixture=${FIXTURE}"
