#!/usr/bin/env bash
set -euo pipefail

# Run the maintained Vulkan RT feature set through the MegaKernel target.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/megakernel_feature_matrix}"

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
die() {
  echo "error: $*" >&2
  exit 1
}

for tool in find sha256sum sort; do
  command -v "${tool}" >/dev/null 2>&1 ||
    die "required tool not found: ${tool}"
done
[[ -n "${OUT_DIR}" && "${OUT_DIR}" != / ]] || die "unsafe OUT_DIR: ${OUT_DIR}"

rm -rf -- "${OUT_DIR}"
mkdir -p "${OUT_DIR}"
MANIFEST="${OUT_DIR}/matrix.tsv"
printf 'profile\tgate\tstatus\timage_count\n' >"${MANIFEST}"

for gate in "${GATES[@]}"; do
  gate_path="${ENV_ROOT}/tools/rtcore/${gate}"
  gate_name="${gate#verify_}"
  gate_name="${gate_name%.sh}"
  case_dir="${OUT_DIR}/${gate_name}"
  [[ -x "${gate_path}" ]] || die "gate is not executable: ${gate_path}"

  echo "RUN profile=megakernel gate=${gate_name}"
  OUT_DIR="${case_dir}" "${gate_path}"

  mapfile -t ppms < <(
    find "${case_dir}" -type f -name '*_spike.ppm' -printf '%P\n' | sort
  )
  printf 'megakernel\t%s\tPASS\t%s\n' \
    "${gate_name}" "${#ppms[@]}" >>"${MANIFEST}"
  for ppm in "${ppms[@]}"; do
    ppm_path="${case_dir}/${ppm}"
    echo "IMAGE gate=${gate_name} ppm=${ppm} sha256=$(sha256sum "${ppm_path}" | awk '{print $1}')"
  done
done

echo "PASS megakernel-feature-matrix gates=${#GATES[@]} manifest=${MANIFEST}"
