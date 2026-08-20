#!/usr/bin/env bash
set -euo pipefail

# Ventus RTcore full-app image regression selector.
#
# Background:
#   RT ABI, compiler, and Spike changes require a fresh software-Vulkan versus
#   Spike image comparison. The Python runner owns the artifact schema; this
#   shell entry provides stable case selection for routine runs.
#
# Flow:
#   Validate selected samples, refresh their GLSL SPIR-V assets, then invoke
#   tools/rtcore_regression.py once. Results are written as
#   artifacts/rtcore-regression/<case>/<width>x<height>/.
#
# Usage:
#   tools/rtcore/regression_suite.sh                         # all comparable cases
#   tools/rtcore/regression_suite.sh raytracingintersection
#   tools/rtcore/regression_suite.sh --case raytracingshadows --case raytracingtextures
#   tools/rtcore/regression_suite.sh --list
#
# Maintenance:
#   Keep case eligibility in ventus_rtcore_regression_cases.json. Do not add
#   raytracingbasic here until it has a software image-capture hook.

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ROOT_DIR}/testcases/RTcoreSpikeCase/SaschaWillems_Vulkan}"
OUT_DIR="${OUT_DIR:-${ROOT_DIR}/artifacts/rtcore-regression}"
WIDTH="${WIDTH:-160}"
HEIGHT="${HEIGHT:-96}"
BENCHMARK_FRAMES="${BENCHMARK_FRAMES:-1}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-60}"
GLSLANG="${GLSLANG:-/usr/bin/glslangValidator}"
REFRESH_SHADERS="${REFRESH_SHADERS:-1}"

readonly -a COMPARABLE_CASES=(
  raytracingshadows
  raytracingsbtdata
  raytracingintersection
  raytracingreflections
  raytracingtextures
)
readonly -a ALL_CASES=(
  raytracingshadows
  raytracingsbtdata
  raytracingintersection
  raytracingbasic
  raytracingreflections
  raytracingtextures
)

usage() {
  sed -n '3,23p' "$0" | sed 's/^# \{0,1\}//'
}

list_cases() {
  printf '%-25s %s\n' 'case' 'image-comparison eligibility'
  printf '%-25s %s\n' 'raytracingshadows' 'run'
  printf '%-25s %s\n' 'raytracingsbtdata' 'run'
  printf '%-25s %s\n' 'raytracingintersection' 'run'
  printf '%-25s %s\n' 'raytracingbasic' 'skip: no software image-capture hook'
  printf '%-25s %s\n' 'raytracingreflections' 'run'
  printf '%-25s %s\n' 'raytracingtextures' 'run'
}

is_known_case() {
  local wanted="$1"
  local case_id
  for case_id in "${ALL_CASES[@]}"; do
    [[ "${case_id}" == "${wanted}" ]] && return 0
  done
  return 1
}

is_comparable_case() {
  local wanted="$1"
  local case_id
  for case_id in "${COMPARABLE_CASES[@]}"; do
    [[ "${case_id}" == "${wanted}" ]] && return 0
  done
  return 1
}

declare -a REQUESTED_CASES=()
while (($#)); do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --list)
      list_cases
      exit 0
      ;;
    --all)
      REQUESTED_CASES=("${ALL_CASES[@]}")
      ;;
    --case)
      if (($# < 2)); then
        echo 'error: --case requires a sample id' >&2
        exit 2
      fi
      REQUESTED_CASES+=("$2")
      shift
      ;;
    --)
      shift
      REQUESTED_CASES+=("$@")
      break
      ;;
    -*)
      echo "error: unknown option: $1" >&2
      exit 2
      ;;
    *)
      REQUESTED_CASES+=("$1")
      ;;
  esac
  shift
done

if ((${#REQUESTED_CASES[@]} == 0)); then
  REQUESTED_CASES=("${ALL_CASES[@]}")
fi

if [[ ! -f "${WORKLOAD_DIR}/ventus_rtcore_regression_cases.json" ]]; then
  echo "error: RT regression manifest not found under ${WORKLOAD_DIR}" >&2
  exit 2
fi
if [[ ! -f "${ROOT_DIR}/tools/rtcore_regression.py" ]]; then
  echo "error: regression runner not found: ${ROOT_DIR}/tools/rtcore_regression.py" >&2
  exit 2
fi
if [[ "${REFRESH_SHADERS}" != 0 && ! -x "${GLSLANG}" ]]; then
  echo "error: GLSLANG is not executable: ${GLSLANG}" >&2
  exit 2
fi

declare -A SEEN=()
declare -a RUNNER_ARGS=()
declare -a REFRESH_CASES=()
for case_id in "${REQUESTED_CASES[@]}"; do
  if ! is_known_case "${case_id}"; then
    echo "error: unknown RT regression case: ${case_id}" >&2
    exit 2
  fi
  if [[ -n "${SEEN[${case_id}]:-}" ]]; then
    continue
  fi
  SEEN[${case_id}]=1
  if is_comparable_case "${case_id}"; then
    RUNNER_ARGS+=(--case "${case_id}")
    REFRESH_CASES+=("${case_id}")
  else
    echo "SKIP ${case_id}: no software image-capture hook; no exact-image verdict is possible"
  fi
done

if ((${#RUNNER_ARGS[@]} == 0)); then
  echo 'Summary: 0 image-comparable cases selected (all selected cases were skipped)'
  exit 0
fi

if [[ "${REFRESH_SHADERS}" != 0 ]]; then
  for case_id in "${REFRESH_CASES[@]}"; do
    echo "PREP ${case_id}: refreshing GLSL SPIR-V"
    (
      cd "${WORKLOAD_DIR}/shaders/glsl"
      python3 compileshaders.py --glslang "${GLSLANG}" --sample "${case_id}"
    )
  done
fi

exec python3 "${ROOT_DIR}/tools/rtcore_regression.py" \
  --manifest "${WORKLOAD_DIR}/ventus_rtcore_regression_cases.json" \
  --workload-dir "${WORKLOAD_DIR}" \
  --out-dir "${OUT_DIR}" \
  --include-candidates \
  --hardware-runner spike \
  --width "${WIDTH}" \
  --height "${HEIGHT}" \
  --benchmark-frames "${BENCHMARK_FRAMES}" \
  --timeout-seconds "${TIMEOUT_SECONDS}" \
  "${RUNNER_ARGS[@]}"
