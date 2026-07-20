#!/usr/bin/env bash
set -euo pipefail

# Ventus RTcore reference image runner.
#
# Background:
#   The raytracingshadows testcase now has two intentional asset levels:
#   an upstream SaschaWillems scene for real shadow correctness and a tiny
#   Ventus-only scene for fast ABI/SBT/payload smoke checks.
#
# Flow:
#   Run the software Vulkan driver under Xvfb, ask raytracingshadows to dump
#   its storage image with RT_SHADOW_SCREENSHOT, convert every generated PPM in
#   the output tree to PNG, and write a manifest that records command inputs.
#
# Usage:
#   tools/rtcore/reference_suite.sh
#   RUN_LEVEL0=1 RUN_LEVEL1=0 WIDTH=160 HEIGHT=96 tools/rtcore/reference_suite.sh
#   APP=/path/to/raytracingshadows tools/rtcore/reference_suite.sh
#
# Maintenance:
#   Use the same repository-local prepared source and application as the Spike
#   runner so reference and simulated images cannot silently use different
#   workload revisions or overlays.

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ROOT_DIR}/build/rt-workload/source}"
APP="${APP:-${ROOT_DIR}/build/rt-workload/build/bin/raytracingshadows}"
OUT_DIR="${OUT_DIR:-${ROOT_DIR}/artifacts/rtcore-spike/reference_suite/raytracingshadows}"
SOFTWARE_ICD="${SOFTWARE_ICD:-/usr/share/vulkan/icd.d/lvp_icd.json}"
WIDTH="${WIDTH:-160}"
HEIGHT="${HEIGHT:-96}"
BENCHMARK_FRAMES="${BENCHMARK_FRAMES:-1}"
RUN_LEVEL0="${RUN_LEVEL0:-1}"
RUN_LEVEL1="${RUN_LEVEL1:-1}"

UPSTREAM_ASSET="${UPSTREAM_ASSET:-${WORKLOAD_DIR}/assets/models/vulkanscene_shadow.gltf}"
MINIMAL_ASSET="${MINIMAL_ASSET:-${WORKLOAD_DIR}/assets/models/vulkanscene_shadow_minimal.gltf}"

case "${WORKLOAD_DIR}" in
  "${ROOT_DIR}"/build/rt-workload/*) ;;
  *)
    echo "error: WORKLOAD_DIR must use the repository-local prepared workload: ${WORKLOAD_DIR}" >&2
    exit 2
    ;;
esac

for tool in pnmtopng python3 xvfb-run; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "error: required tool not found: ${tool}" >&2
    exit 2
  fi
done

if [[ ! -x "${APP}" ]]; then
  echo "error: raytracingshadows executable not found: ${APP}" >&2
  exit 2
fi

if [[ ! -f "${SOFTWARE_ICD}" ]]; then
  echo "error: software Vulkan ICD not found: ${SOFTWARE_ICD}" >&2
  exit 2
fi

if [[ ! -f "${UPSTREAM_ASSET}" ]]; then
  echo "error: upstream shadow asset not found: ${UPSTREAM_ASSET}" >&2
  exit 2
fi

if [[ ! -f "${MINIMAL_ASSET}" ]]; then
  echo "error: minimal smoke asset not found: ${MINIMAL_ASSET}" >&2
  exit 2
fi

mkdir -p "${OUT_DIR}"
MANIFEST="${OUT_DIR}/manifest.txt"
: >"${MANIFEST}"

sha256_file() {
  python3 - "$1" <<'PY'
import hashlib
import sys

h = hashlib.sha256()
with open(sys.argv[1], "rb") as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
        h.update(chunk)
print(h.hexdigest())
PY
}

write_header() {
  {
    echo "suite=raytracingshadows_reference"
    echo "root=${ROOT_DIR}"
    echo "workload=${WORKLOAD_DIR}"
    echo "app=${APP}"
    echo "software_icd=${SOFTWARE_ICD}"
    echo "width=${WIDTH}"
    echo "height=${HEIGHT}"
    echo "benchmark_frames=${BENCHMARK_FRAMES}"
    echo "upstream_asset=${UPSTREAM_ASSET}"
    echo "upstream_asset_sha256=$(sha256_file "${UPSTREAM_ASSET}")"
    echo "minimal_asset=${MINIMAL_ASSET}"
    echo "minimal_asset_sha256=$(sha256_file "${MINIMAL_ASSET}")"
    echo
  } >>"${MANIFEST}"
}

convert_ppm_tree() {
  local root="$1"
  while IFS= read -r -d '' ppm; do
    local png="${ppm%.ppm}.png"
    pnmtopng "${ppm}" >"${png}"
  done < <(find "${root}" -type f -name '*.ppm' -print0)
}

run_case() {
  local level="$1"
  local label="$2"
  local asset="$3"
  local case_dir="${OUT_DIR}/${label}_${WIDTH}x${HEIGHT}"
  local ppm="${case_dir}/raytracingshadows_${label}_${WIDTH}x${HEIGHT}.ppm"
  local png="${case_dir}/raytracingshadows_${label}_${WIDTH}x${HEIGHT}.png"
  local log="${case_dir}/raytracingshadows_${label}.log"
  local app_status

  rm -rf "${case_dir}"
  mkdir -p "${case_dir}"

  set +e
  (
    cd "${WORKLOAD_DIR}"
    export VK_DRIVER_FILES="${SOFTWARE_ICD}"
    export VK_ICD_FILENAMES="${SOFTWARE_ICD}"
    export VK_LOADER_DRIVERS_SELECT="$(basename "${SOFTWARE_ICD}")"
    export LIBGL_ALWAYS_SOFTWARE=1
    export MESA_LOADER_DRIVER_OVERRIDE=llvmpipe
    export RT_SHADOW_SCREENSHOT="${ppm}"
    if [[ -n "${asset}" ]]; then
      export RT_SHADOW_ASSET="${asset}"
    else
      unset RT_SHADOW_ASSET
    fi
    xvfb-run -a "${APP}" --benchmark --benchmarkframes "${BENCHMARK_FRAMES}" \
      --width "${WIDTH}" --height "${HEIGHT}"
  ) >"${log}" 2>&1
  app_status=$?
  set -e

  if [[ ! -f "${ppm}" ]]; then
    echo "error: expected PPM was not generated: ${ppm}" >&2
    echo "see log: ${log}" >&2
    exit 1
  fi

  pnmtopng "${ppm}" >"${png}"

  {
    echo "[${label}]"
    echo "level=${level}"
    echo "asset=${asset:-upstream-default}"
    echo "ppm=${ppm}"
    echo "png=${png}"
    echo "log=${log}"
    echo "app_exit_status=${app_status}"
    echo
  } >>"${MANIFEST}"

  echo "PASS ${label} png=${png}"
}

write_header

if [[ "${RUN_LEVEL0}" != 0 ]]; then
  run_case "0" "level0_minimal" "${MINIMAL_ASSET}"
fi

if [[ "${RUN_LEVEL1}" != 0 ]]; then
  run_case "1" "level1_upstream" ""
fi

convert_ppm_tree "${OUT_DIR}"
echo "manifest=${MANIFEST}"
