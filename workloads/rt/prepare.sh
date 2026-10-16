#!/usr/bin/env bash
set -euo pipefail

# Combine pinned upstream sources with maintained five-case RT inputs.
# Copy only tracked testcase files, never local builds or diagnostic shaders.
# Usage: bash workloads/rt/prepare.sh; build-ventus.sh --build rt-workload.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UPSTREAM_DIR="${RT_WORKLOAD_UPSTREAM:-${ROOT_DIR}/workloads/rt/SaschaWillems_Vulkan}"
BUILD_ROOT="${RT_WORKLOAD_BUILD_ROOT:-${ROOT_DIR}/build/rt-workload}"
SOURCE_DIR="${RT_WORKLOAD_SOURCE_DIR:-${BUILD_ROOT}/source}"
INPUT_PREFIX="RTcoreSpikeCase/SaschaWillems_Vulkan"
UPSTREAM_COMMIT="3b843fbf667a89a1cfcc64405e9fc6f9018e03b4"
GLM_COMMIT="1ad55c5016339b83b7eec98c31007e0aee57d2bf"
die() { echo "error: $*" >&2; exit 1; }
for tool in git tar cp realpath glslangValidator; do
  command -v "${tool}" >/dev/null || die "required tool not found: ${tool}"
done
[[ -f "${UPSTREAM_DIR}/CMakeLists.txt" && -e "${UPSTREAM_DIR}/.git" ]] ||
  die "initialize upstream: git submodule update --init workloads/rt/SaschaWillems_Vulkan"
[[ "$(git -C "${UPSTREAM_DIR}" rev-parse HEAD)" == "${UPSTREAM_COMMIT}" ]] ||
  die "upstream must be pinned to ${UPSTREAM_COMMIT}"
if [[ ! -f "${UPSTREAM_DIR}/external/glm/glm/glm.hpp" ]]; then
  git -C "${UPSTREAM_DIR}" submodule update --init --depth 1 external/glm
fi
[[ "$(git -C "${UPSTREAM_DIR}/external/glm" rev-parse HEAD)" == "${GLM_COMMIT}" ]] ||
  die "unexpected GLM revision"
[[ -e "${ROOT_DIR}/testcases/.git" &&
   -f "${ROOT_DIR}/testcases/${INPUT_PREFIX}/ventus_rtcore_regression_cases.json" ]] ||
  die "initialize testcases submodule"
BUILD_ROOT="$(realpath -m "${BUILD_ROOT}")"
SOURCE_DIR="$(realpath -m "${SOURCE_DIR}")"
case "${SOURCE_DIR}" in
  "${BUILD_ROOT}"/*) ;;
  *) die "generated source must remain below BUILD_ROOT" ;;
esac
if [[ -e "${SOURCE_DIR}" ]]; then
  [[ -f "${SOURCE_DIR}/.ventus-rt-inputs" ]] ||
    die "refusing to overwrite unowned source; select a fresh RT_WORKLOAD_BUILD_ROOT"
  [[ "$(<"${SOURCE_DIR}/.ventus-rt-inputs")" == "${UPSTREAM_COMMIT}" ]] ||
    die "generated source has a different upstream revision"
else
  mkdir -p "${SOURCE_DIR}"
  git -C "${UPSTREAM_DIR}" archive "${UPSTREAM_COMMIT}" | tar -x -C "${SOURCE_DIR}"
  mkdir -p "${SOURCE_DIR}/external/glm"
  git -C "${UPSTREAM_DIR}/external/glm" archive "${GLM_COMMIT}" |
    tar -x -C "${SOURCE_DIR}/external/glm"
  printf '%s\n' "${UPSTREAM_COMMIT}" > "${SOURCE_DIR}/.ventus-rt-inputs"
fi
while IFS= read -r -d '' file; do
  relative="${file#${INPUT_PREFIX}/}"
  case "${relative}" in
    assets/models/reflection_scene.gltf|assets/models/vulkanscene*|assets/models/README.ventus.md) ;;
    assets/models/*) continue ;;
  esac
  [[ -f "${ROOT_DIR}/testcases/${file}" ]] || die "missing tracked input: ${file}"
  mkdir -p "${SOURCE_DIR}/$(dirname "${relative}")"
  cp -- "${ROOT_DIR}/testcases/${file}" "${SOURCE_DIR}/${relative}"
done < <(git -C "${ROOT_DIR}/testcases" ls-files -z -- "${INPUT_PREFIX}")
for example in raytracingshadows raytracingsbtdata raytracingintersection raytracingreflections raytracingtextures; do
  for shader in "${SOURCE_DIR}/shaders/glsl/${example}"/*; do
    case "${shader}" in
      *.rgen|*.rchit|*.rmiss|*.rahit|*.rint)
        glslangValidator -V --target-env vulkan1.2 "${shader}" -o "${shader}.spv" ;;
    esac
  done
done
echo "prepared_source=${SOURCE_DIR}"
echo "upstream_commit=${UPSTREAM_COMMIT}"
echo "testcase_commit=$(git -C "${ROOT_DIR}/testcases" rev-parse HEAD)"
