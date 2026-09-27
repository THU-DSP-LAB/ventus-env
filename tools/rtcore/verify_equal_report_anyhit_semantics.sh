#!/usr/bin/env bash
set -euo pipefail

# Exercise the compiler -> procedural report -> any-hit -> committed-hit path
# with three equal-distance reports.  The 1x1 image is the observable semantic
# result; the optional replay capture binds the result to the issued RT stream.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME_ROOT="${RUNTIME_ROOT:-${ENV_ROOT}}"
WORKLOAD_DIR="${WORKLOAD_DIR:-${RUNTIME_ROOT}/build/rt-workload/source}"
APP="${APP:-${RUNTIME_ROOT}/build/rt-workload/build/bin/raytracingintersection}"
RUNNER="${RUNNER:-${RUNTIME_ROOT}/tools/rtcore/run_full_app_spike.sh}"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/equal_report_anyhit}"
GLSLANG_VALIDATOR="${GLSLANG_VALIDATOR:-glslangValidator}"
SHADER_DIR="${WORKLOAD_DIR}/shaders/glsl/raytracingintersection"

die() {
  echo "error: $*" >&2
  exit 1
}

for tool in "${GLSLANG_VALIDATOR}" python3; do
  command -v "${tool}" >/dev/null 2>&1 || die "required tool not found: ${tool}"
done
[[ -x "${APP}" ]] || die "required executable not found: ${APP}"
[[ -x "${RUNNER}" ]] || die "required runner not found: ${RUNNER}"

modules=(raygen.rgen miss.rmiss intersection.rint anyhit.rahit closesthit.rchit)
for module in "${modules[@]}"; do
  [[ -f "${SHADER_DIR}/${module}.spv" ]] ||
    die "required shader module not found: ${SHADER_DIR}/${module}.spv"
done

scratch="$(mktemp -d "${TMPDIR:-/tmp}/ventus-equal-report.XXXXXX")"
restore_modules() {
  local module
  for module in "${modules[@]}"; do
    if [[ -f "${scratch}/original/${module}.spv" ]]; then
      cp "${scratch}/original/${module}.spv" "${SHADER_DIR}/${module}.spv"
    fi
  done
}
trap restore_modules EXIT INT TERM
mkdir -p "${scratch}/original" "${scratch}/shader" "${OUT_DIR}"
for module in "${modules[@]}"; do
  cp "${SHADER_DIR}/${module}.spv" "${scratch}/original/${module}.spv"
done

cat >"${scratch}/shader/raygen.rgen" <<'GLSL'
#version 460
#extension GL_EXT_ray_tracing : require
#extension GL_EXT_shader_image_load_formatted : enable
layout(binding = 0, set = 0) uniform accelerationStructureEXT topLevelAS;
layout(binding = 1, set = 0) uniform image2D image;
layout(binding = 2, set = 0) uniform CameraProperties {
    mat4 viewInverse;
    mat4 projInverse;
    vec4 lightPos;
    uvec4 traceConfig;
} cam;
layout(location = 0) rayPayloadEXT vec3 observation;
void main() {
    const vec2 center = vec2(gl_LaunchIDEXT.xy) + vec2(0.5);
    const vec2 uv = center / vec2(gl_LaunchSizeEXT.xy);
    const vec2 d = uv * 2.0 - 1.0;
    const vec4 origin = cam.viewInverse * vec4(0, 0, 0, 1);
    const vec4 target = cam.projInverse * vec4(d.x, d.y, 1, 1);
    const vec4 direction = cam.viewInverse *
        vec4(normalize(target.xyz / target.w), 0);
    observation = vec3(0.0);
    traceRayEXT(topLevelAS, gl_RayFlagsNoneEXT, 0xff, 0, 0, 0,
                origin.xyz, 0.001, direction.xyz, 10000.0, 0);
    imageStore(image, ivec2(gl_LaunchIDEXT.xy), vec4(observation, 1.0));
}
GLSL

cat >"${scratch}/shader/miss.rmiss" <<'GLSL'
#version 460
#extension GL_EXT_ray_tracing : require
layout(location = 0) rayPayloadInEXT vec3 observation;
void main() { observation = vec3(1.0, 0.0, 1.0); }
GLSL

cat >"${scratch}/shader/intersection.rint" <<'GLSL'
#version 460
#extension GL_EXT_ray_tracing : require
struct Sphere { vec3 center; float radius; vec4 color; };
layout(binding = 3, set = 0) buffer Spheres { Sphere s[]; } spheres;
hitAttributeEXT vec2 reportAttribute;
float sphereHit(Sphere sphere, vec3 origin, vec3 direction) {
    vec3 oc = origin - sphere.center;
    float b = dot(oc, direction);
    float c = dot(oc, oc) - sphere.radius * sphere.radius;
    float h = b * b - c;
    return h < 0.0 ? -1.0 : -b - sqrt(h);
}
void main() {
    float hit = sphereHit(spheres.s[gl_PrimitiveID],
                          gl_WorldRayOriginEXT, gl_WorldRayDirectionEXT);
    if (hit > 0.0) {
        reportAttribute = vec2(0.25, 0.0);
        reportIntersectionEXT(hit, 1);
        reportAttribute = vec2(0.50, 0.0);
        reportIntersectionEXT(hit, 2);
        reportAttribute = vec2(0.75, 0.0);
        reportIntersectionEXT(hit, 3);
    }
}
GLSL

cat >"${scratch}/shader/closesthit.rchit" <<'GLSL'
#version 460
#extension GL_EXT_ray_tracing : require
layout(location = 0) rayPayloadInEXT vec3 observation;
hitAttributeEXT vec2 reportAttribute;
void main() {
    observation.y = reportAttribute.x;
    observation.z = float(gl_HitKindEXT) * 0.25;
}
GLSL

compile_module() {
  local module="$1"
  "${GLSLANG_VALIDATOR}" -V "${scratch}/shader/${module}" \
    -o "${SHADER_DIR}/${module}.spv" --target-env vulkan1.2 >/dev/null
}
compile_module raygen.rgen
compile_module miss.rmiss
compile_module intersection.rint
compile_module closesthit.rchit

run_mode() {
  local mode="$1"
  local expected="$2"
  local case_dir="${OUT_DIR}/${mode}"
  local capture="${case_dir}/rt-replay.capture"
  local action ppm log
  mkdir -p "${case_dir}"
  case "${mode}" in
    accept)
      action=''
      ;;
    ignore)
      action='if (gl_HitKindEXT >= 2) { ignoreIntersectionEXT; }'
      ;;
    terminate)
      action='if (gl_HitKindEXT == 2) { terminateRayEXT; }'
      ;;
    *) die "unknown mode: ${mode}" ;;
  esac
  cat >"${scratch}/shader/anyhit.rahit" <<GLSL
#version 460
#extension GL_EXT_ray_tracing : require
layout(location = 0) rayPayloadInEXT vec3 observation;
hitAttributeEXT vec2 reportAttribute;
void main() {
    observation.x += float(gl_HitKindEXT) / 16.0;
    ${action}
}
GLSL
  compile_module anyhit.rahit

  # One trace emits the candidate and terminal traversal commands.  Keeping
  # exactly those two commands makes the capture directly replayable by the
  # RTL harness without mixing the next presentation frame into the stream.
  ROOT_DIR="${RUNTIME_ROOT}" \
  WORKLOAD_DIR="${WORKLOAD_DIR}" \
  APP="${APP}" \
  APP_NAME=raytracingintersection \
  OUT_DIR="${case_dir}" \
  RUN_1X1=1 RUN_4X4=0 RUN_16X16=0 RUN_160X96=0 RUN_320X192=0 \
  VENTUS_RT_SEMANTIC_SCENARIO=equal_reports \
  VENTUS_RT_RTL_CAPTURE="${capture}" \
  VENTUS_RT_RTL_CAPTURE_LIMIT="${VENTUS_RT_RTL_CAPTURE_LIMIT:-2}" \
  "${RUNNER}"

  ppm="${case_dir}/1x1/raytracingintersection_spike.ppm"
  log="${case_dir}/1x1/raytracingintersection.log"
  [[ -s "${capture}" ]] || die "replay capture was not produced for ${mode}"
  [[ -s "${log}" ]] || die "runtime log was not produced for ${mode}"
  python3 - "${mode}" "${expected}" "${ppm}" "${capture}" \
    "${case_dir}/result.json" <<'PY'
import json
import math
import os
import pathlib
import sys

mode, expected_text, ppm_name, capture_name, result_name = sys.argv[1:]
expected = [float(value) for value in expected_text.split(",")]
data = pathlib.Path(ppm_name).read_bytes()
tokens = []
position = 0
while len(tokens) < 4:
    while data[position:position + 1] in b" \t\r\n":
        position += 1
    if data[position:position + 1] == b"#":
        position = data.index(b"\n", position) + 1
        continue
    end = position
    while data[end:end + 1] not in b" \t\r\n":
        end += 1
    tokens.append(data[position:end])
    position = end
while data[position:position + 1] in b" \t\r\n":
    position += 1
if tokens != [b"P6", b"1", b"1", b"255"]:
    raise SystemExit(f"unexpected PPM header: {tokens!r}")
pixel = list(data[position:position + 3])
if len(pixel) != 3:
    raise SystemExit("missing 1x1 RGB payload")
observed = [channel / 255.0 for channel in pixel]
mismatches = []
for index, (actual, target) in enumerate(zip(observed, expected)):
    if abs(actual - target) > 2.0 / 255.0:
        mismatches.append(
            f"channel {index}: observed {actual}, expected {target}"
        )
if mismatches and os.environ.get("VENTUS_RT_EXPECTED_STRICT", "1") != "0":
    raise SystemExit(f"{mode}: " + "; ".join(mismatches))
capture_lines = pathlib.Path(capture_name).read_text().splitlines()
command_ids = []
for line in capture_lines:
    fields = line.split()
    if fields and fields[0] == "E" and len(fields) > 1:
        command_ids.append(int(fields[1], 0))
result = {
    "schema": "ventus_equal_report_anyhit_v1",
    "scenario": "equal_distance_reports",
    "decision": mode,
    "expected": expected,
    "observed_rgb8": pixel,
    "observed": observed,
    "capture": str(pathlib.Path(capture_name).resolve()),
    "command_ids": command_ids,
    "mismatches": mismatches,
    "passed": not mismatches,
}
pathlib.Path(result_name).write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, sort_keys=True))
PY
}

run_mode accept "0.375,0.75,0.75"
run_mode ignore "0.375,0.25,0.25"
run_mode terminate "0.1875,0.50,0.50"

python3 - "${OUT_DIR}" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
results = [json.loads((root / mode / "result.json").read_text())
           for mode in ("accept", "ignore", "terminate")]
summary = {
    "schema": "ventus_equal_report_anyhit_suite_v1",
    "scenario": "equal_distance_reports",
    "results": results,
    "passed": all(result["passed"] for result in results),
}
(root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, sort_keys=True))
if not summary["passed"] and __import__("os").environ.get(
        "VENTUS_RT_EXPECTED_STRICT", "1") != "0":
    raise SystemExit("equal-report any-hit semantic suite failed")
PY
