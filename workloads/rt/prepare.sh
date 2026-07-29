#!/usr/bin/env bash
set -euo pipefail

# Materialize the pinned upstream RT workloads plus the small Ventus-owned
# overlay into a generated build tree. The official submodule is never
# modified. The large upstream asset repository is fetched sparsely so only
# scenes used by maintained RT workloads are checked out.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UPSTREAM_DIR="${RT_WORKLOAD_UPSTREAM:-${ROOT_DIR}/workloads/rt/SaschaWillems_Vulkan}"
OVERLAY_DIR="${RT_WORKLOAD_OVERLAY:-${ROOT_DIR}/workloads/rt/ventus-overlay}"
BUILD_ROOT="${RT_WORKLOAD_BUILD_ROOT:-${ROOT_DIR}/build/rt-workload}"
SOURCE_DIR="${RT_WORKLOAD_SOURCE_DIR:-${BUILD_ROOT}/source}"
ASSET_CACHE="${RT_WORKLOAD_ASSET_CACHE:-${BUILD_ROOT}/cache/Vulkan-Assets}"

UPSTREAM_COMMIT="3b843fbf667a89a1cfcc64405e9fc6f9018e03b4"
ASSET_COMMIT="a27c0e584434d59b7c7a714e9180eefca6f0ec4b"
GLM_COMMIT="1ad55c5016339b83b7eec98c31007e0aee57d2bf"
UPSTREAM_ASSET_SHA256="be6633150d09b951db637fda2254317a94c701c948a57c7819d09403113fde28"
REFLECTION_ASSET_SHA256="d20d0fb6ba02333b37f77f4fef576c1748eaa138a588654643196fb859325585"
TEXTURE_ASSET_SHA256="f27af40f84e22a1f9a423204af5cff1f822fe4c1cbf6a66247f191c842e9078b"
MINIMAL_ASSET_SHA256="3ef66e73927e98910857ac290295d7735347fcf17f6aa599f228cb65d0902981"

die() {
  echo "error: $*" >&2
  exit 1
}

require_file() {
  [[ -f "$1" ]] || die "required file not found: $1"
}

check_sha256() {
  local path="$1"
  local expected="$2"
  local actual
  actual="$(sha256sum "${path}" | awk '{print $1}')"
  [[ "${actual}" == "${expected}" ]] ||
    die "artifact drift for ${path}: expected ${expected}, got ${actual}"
}

for tool in awk cp git patch sha256sum; do
  command -v "${tool}" >/dev/null 2>&1 || die "required tool not found: ${tool}"
done

[[ -d "${UPSTREAM_DIR}" ]] ||
  die "upstream workload is not initialized: git submodule update --init workloads/rt/SaschaWillems_Vulkan"

actual_upstream="$(git -C "${UPSTREAM_DIR}" rev-parse HEAD)"
[[ "${actual_upstream}" == "${UPSTREAM_COMMIT}" ]] ||
  die "unexpected upstream workload commit: ${actual_upstream}; expected ${UPSTREAM_COMMIT}"

if [[ ! -f "${UPSTREAM_DIR}/external/glm/glm/glm.hpp" ]]; then
  git -C "${UPSTREAM_DIR}" submodule update --init --depth 1 external/glm
fi
actual_glm="$(git -C "${UPSTREAM_DIR}/external/glm" rev-parse HEAD)"
[[ "${actual_glm}" == "${GLM_COMMIT}" ]] ||
  die "unexpected GLM commit: ${actual_glm}; expected ${GLM_COMMIT}"

mkdir -p "$(dirname "${ASSET_CACHE}")"
if [[ ! -d "${ASSET_CACHE}/.git" ]]; then
  git clone --filter=blob:none --no-checkout --depth 1 \
    https://github.com/SaschaWillems/Vulkan-Assets.git "${ASSET_CACHE}"
fi

git -C "${ASSET_CACHE}" sparse-checkout init --no-cone
git -C "${ASSET_CACHE}" sparse-checkout set \
  /models/vulkanscene_shadow.gltf \
  /models/reflection_scene.gltf \
  /textures/gratefloor_rgba.ktx
if ! git -C "${ASSET_CACHE}" cat-file -e "${ASSET_COMMIT}^{commit}"; then
  git -C "${ASSET_CACHE}" fetch --depth 1 origin "${ASSET_COMMIT}"
fi
git -C "${ASSET_CACHE}" checkout --detach "${ASSET_COMMIT}"

UPSTREAM_ASSET="${ASSET_CACHE}/models/vulkanscene_shadow.gltf"
REFLECTION_ASSET="${ASSET_CACHE}/models/reflection_scene.gltf"
TEXTURE_ASSET="${ASSET_CACHE}/textures/gratefloor_rgba.ktx"
MINIMAL_ASSET="${OVERLAY_DIR}/vulkanscene_shadow_minimal.gltf"
PATCH_FILES=(
  "${OVERLAY_DIR}/raytracingshadows.patch"
  "${OVERLAY_DIR}/raytracingbasic-multiset.patch"
)
require_file "${UPSTREAM_ASSET}"
require_file "${REFLECTION_ASSET}"
require_file "${TEXTURE_ASSET}"
require_file "${MINIMAL_ASSET}"
for patch_file in "${PATCH_FILES[@]}"; do
  require_file "${patch_file}"
done
check_sha256 "${UPSTREAM_ASSET}" "${UPSTREAM_ASSET_SHA256}"
check_sha256 "${REFLECTION_ASSET}" "${REFLECTION_ASSET_SHA256}"
check_sha256 "${TEXTURE_ASSET}" "${TEXTURE_ASSET_SHA256}"
check_sha256 "${MINIMAL_ASSET}" "${MINIMAL_ASSET_SHA256}"
for patch_file in "${PATCH_FILES[@]}"; do
  git -C "${UPSTREAM_DIR}" apply --check --unidiff-zero "${patch_file}"
done

TEMP_SOURCE="${SOURCE_DIR}.tmp.$$"
case "${TEMP_SOURCE}" in
  "${BUILD_ROOT}"/*) ;;
  *) die "generated source must remain below BUILD_ROOT: ${TEMP_SOURCE}" ;;
esac

rm -rf -- "${TEMP_SOURCE}"
mkdir -p "${TEMP_SOURCE}"
cp -a --reflink=auto "${UPSTREAM_DIR}/." "${TEMP_SOURCE}/"
rm -rf -- "${TEMP_SOURCE}/.git" \
  "${TEMP_SOURCE}/assets/.git" \
  "${TEMP_SOURCE}/external/glm/.git"

for patch_file in "${PATCH_FILES[@]}"; do
  patch --directory="${TEMP_SOURCE}" --strip=1 --forward --batch \
    <"${patch_file}"
done
mkdir -p "${TEMP_SOURCE}/assets/models"
mkdir -p "${TEMP_SOURCE}/assets/textures"
cp -a --reflink=auto "${UPSTREAM_ASSET}" \
  "${TEMP_SOURCE}/assets/models/vulkanscene_shadow.gltf"
cp -a --reflink=auto "${REFLECTION_ASSET}" \
  "${TEMP_SOURCE}/assets/models/reflection_scene.gltf"
cp -a --reflink=auto "${TEXTURE_ASSET}" \
  "${TEMP_SOURCE}/assets/textures/gratefloor_rgba.ktx"
cp -a --reflink=auto "${MINIMAL_ASSET}" \
  "${TEMP_SOURCE}/assets/models/vulkanscene_shadow_minimal.gltf"

case "${SOURCE_DIR}" in
  "${BUILD_ROOT}"/*) ;;
  *) die "generated source must remain below BUILD_ROOT: ${SOURCE_DIR}" ;;
esac
rm -rf -- "${SOURCE_DIR}"
mv "${TEMP_SOURCE}" "${SOURCE_DIR}"

echo "prepared_source=${SOURCE_DIR}"
echo "upstream_commit=${UPSTREAM_COMMIT}"
echo "asset_commit=${ASSET_COMMIT}"
echo "glm_commit=${GLM_COMMIT}"
