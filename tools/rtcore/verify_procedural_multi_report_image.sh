#!/usr/bin/env bash
set -euo pipefail

# Prove that an independent intersection entry composes multiple procedural
# reports in shader order.  The second report is farther than the first and
# must not replace the formal committed hit established by the first report.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKLOAD_DIR="${WORKLOAD_DIR:-${ENV_ROOT}/build/rt-workload/source}"
SOURCE="${INTERSECTION_SOURCE:-${WORKLOAD_DIR}/shaders/glsl/raytracingintersection/intersection.rint}"
SPV="${INTERSECTION_SPV:-${SOURCE}.spv}"
GLSLANG_VALIDATOR="${GLSLANG_VALIDATOR:-glslangValidator}"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/procedural_multi_report_exact_image}"

die() {
  echo "error: $*" >&2
  exit 1
}

for tool in cp mktemp python3 "${GLSLANG_VALIDATOR}"; do
  command -v "${tool}" >/dev/null 2>&1 || die "required tool not found: ${tool}"
done
[[ -f "${SOURCE}" ]] || die "intersection source not found: ${SOURCE}"
[[ -f "${SPV}" ]] || die "intersection SPIR-V not found: ${SPV}"

TMP_DIR="$(mktemp -d /tmp/ventus-rt-multi-report.XXXXXX)"
ORIGINAL_SPV="${TMP_DIR}/intersection.original.spv"
MODIFIED_SOURCE="${TMP_DIR}/intersection.rint"
cp -- "${SPV}" "${ORIGINAL_SPV}"

cleanup() {
  cp -- "${ORIGINAL_SPV}" "${SPV}"
  rm -rf -- "${TMP_DIR}"
}
trap cleanup EXIT

python3 - "${SOURCE}" "${MODIFIED_SOURCE}" <<'PY'
import pathlib
import sys

source, output = map(pathlib.Path, sys.argv[1:])
text = source.read_text()
needle = "        reportIntersectionEXT(hit, 0);"
replacement = """        bool firstAccepted = reportIntersectionEXT(hit, 0);
        if (firstAccepted) {
            reportIntersectionEXT(hit + 0.25, 1);
        }"""
if text.count(needle) != 1:
    raise SystemExit("expected one canonical reportIntersectionEXT call")
output.write_text(text.replace(needle, replacement))
PY

"${GLSLANG_VALIDATOR}" --target-env vulkan1.2 -V \
  "${MODIFIED_SOURCE}" -o "${SPV}" >/dev/null

OUT_DIR="${OUT_DIR}" \
INTERSECTION_SOURCE="${MODIFIED_SOURCE}" \
"${ENV_ROOT}/tools/rtcore/verify_procedural_aabb_image.sh"

echo "PASS procedural-multi-report-image first=accepted second=farther-rejected"
