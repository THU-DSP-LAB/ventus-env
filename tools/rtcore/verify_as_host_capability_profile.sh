#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MESA_BUILD="${MESA_BUILD:-${ROOT_DIR}/mesa/build-ventus}"
ICD="${ICD:-${MESA_BUILD}/src/ventus/vulkan/ventus_devenv_icd.x86_64.json}"
SOURCE="${ROOT_DIR}/tools/rtcore/as_host_capability_probe.c"
OUT_DIR="${OUT_DIR:-/tmp/ventus-as-host-capability-profile}"
CC="${CC:-cc}"
PKG_CONFIG="${PKG_CONFIG:-pkg-config}"

die() {
  echo "error: $*" >&2
  exit 1
}

for tool in "${CC}" "${PKG_CONFIG}" rg; do
  command -v "${tool}" >/dev/null 2>&1 ||
    die "required tool not found: ${tool}"
done
[[ -f "${ICD}" ]] || die "Ventus ICD not found: ${ICD}"
[[ -f "${SOURCE}" ]] || die "probe source not found: ${SOURCE}"
[[ -n "${OUT_DIR}" && "${OUT_DIR}" != / ]] ||
  die "unsafe OUT_DIR: ${OUT_DIR}"

read -r -a vulkan_cflags <<<"$("${PKG_CONFIG}" --cflags vulkan)"
read -r -a vulkan_libs <<<"$("${PKG_CONFIG}" --libs vulkan)"

rm -rf -- "${OUT_DIR}"
mkdir -p "${OUT_DIR}"

"${CC}" -std=c11 -Wall -Wextra -Werror \
  "${vulkan_cflags[@]}" \
  "${SOURCE}" \
  -o "${OUT_DIR}/as_host_capability_probe" \
  "${vulkan_libs[@]}"

probe_output="$(
  env \
    VK_ICD_FILENAMES="${ICD}" \
    "${OUT_DIR}/as_host_capability_probe"
)"
printf '%s\n' "${probe_output}"
rg -q '^PASS as-host-capability-profile ' <<<"${probe_output}" ||
  die "AS host capability probe did not report PASS"
