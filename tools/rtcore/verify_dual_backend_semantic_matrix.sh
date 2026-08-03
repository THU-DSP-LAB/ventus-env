#!/usr/bin/env bash
set -euo pipefail

# Run backend-neutral Vulkan RT features through both the MegaKernel-compatible
# and Transparent Wavefront execution paths, then require identical images.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/dual_backend_semantic_matrix}"

GATES=(
  verify_callable_shader_image.sh
  verify_callable_shader_record_data.sh
  verify_callable_shader_restrictions.sh
  verify_instance_custom_index_image.sh
  verify_instance_transform_builtins.sh
  verify_iterative_reflections_image.sh
  verify_multiset_rt_image.sh
  verify_procedural_aabb_image.sh
  verify_ray_culling_flags.sh
  verify_ray_primitive_culling.sh
  verify_sbt_record_data_image.sh
  verify_textured_any_hit_image.sh
  verify_triangle_position_fetch.sh
)
PROFILES=(compat global)

die() {
  echo "error: $*" >&2
  exit 1
}

for tool in awk cmp find sha256sum sort; do
  command -v "${tool}" >/dev/null 2>&1 ||
    die "required tool not found: ${tool}"
done
[[ -n "${OUT_DIR}" && "${OUT_DIR}" != / ]] || die "unsafe OUT_DIR: ${OUT_DIR}"

rm -rf -- "${OUT_DIR}"
mkdir -p "${OUT_DIR}"
MANIFEST="${OUT_DIR}/matrix.tsv"
printf 'profile\tgate\tstatus\n' >"${MANIFEST}"

for profile in "${PROFILES[@]}"; do
  for gate in "${GATES[@]}"; do
    gate_path="${ENV_ROOT}/tools/rtcore/${gate}"
    gate_name="${gate#verify_}"
    gate_name="${gate_name%.sh}"
    case_dir="${OUT_DIR}/${profile}/${gate_name}"
    [[ -x "${gate_path}" ]] || die "gate is not executable: ${gate_path}"

    echo "RUN profile=${profile} gate=${gate_name}"
    VENTUS_VK_RT_EXECUTION_PROFILE="${profile}" \
      OUT_DIR="${case_dir}" \
      "${gate_path}"
    printf '%s\t%s\tPASS\n' "${profile}" "${gate_name}" >>"${MANIFEST}"
  done
done

for gate in "${GATES[@]}"; do
  gate_name="${gate#verify_}"
  gate_name="${gate_name%.sh}"
  compat_dir="${OUT_DIR}/compat/${gate_name}"
  global_dir="${OUT_DIR}/global/${gate_name}"

  mapfile -t compat_ppms < <(
    find "${compat_dir}" -type f -name '*_spike.ppm' -printf '%P\n' | sort
  )
  mapfile -t global_ppms < <(
    find "${global_dir}" -type f -name '*_spike.ppm' -printf '%P\n' | sort
  )
  [[ "${compat_ppms[*]}" == "${global_ppms[*]}" ]] ||
    die "${gate_name} produced different PPM sets across backends"

  for ppm in "${compat_ppms[@]}"; do
    compat_ppm="${compat_dir}/${ppm}"
    global_ppm="${global_dir}/${ppm}"
    cmp -s "${compat_ppm}" "${global_ppm}" ||
      die "${gate_name}/${ppm} differs across backends"
    echo "MATCH gate=${gate_name} ppm=${ppm} sha256=$(sha256sum "${compat_ppm}" | awk '{print $1}')"
  done
done

echo "PASS dual-backend-semantic-matrix gates=${#GATES[@]} profiles=${#PROFILES[@]} manifest=${MANIFEST}"
