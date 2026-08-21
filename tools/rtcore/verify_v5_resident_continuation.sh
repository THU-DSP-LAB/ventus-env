#!/usr/bin/env bash
set -euo pipefail

# Exercise a real depth-2 closest-hit child trace through the production V5
# MegaKernel ABI and require the frozen V4/V5-equivalent image.

ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LLVM_BUILD="${LLVM_BUILD:-${ENV_ROOT}/llvm/build}"

export VENTUS_VK_RT_ABI_V5=1
export VENTUS_VK_RETAIN_SHADER_ARTIFACTS=1
export OUT_DIR="${OUT_DIR:-${ENV_ROOT}/artifacts/rtcore-spike/v5-resident-continuation}"

"${ENV_ROOT}/tools/rtcore/verify_compat_p1_image.sh"

mapfile -t rt_elfs < <(
  find "${OUT_DIR}/160x96/elf" -maxdepth 1 -type f -name '*.riscv' -print
)
[[ "${#rt_elfs[@]}" -eq 1 ]] || {
  echo "error: expected one retained V5 RT ELF, found ${#rt_elfs[@]}" >&2
  exit 1
}

LLVM_OBJCOPY="${LLVM_OBJCOPY:-${LLVM_BUILD}/bin/llvm-objcopy}"
LLVM_OBJDUMP="${LLVM_OBJDUMP:-${LLVM_BUILD}/bin/llvm-objdump}"
[[ -x "${LLVM_OBJCOPY}" ]] || {
  echo "error: llvm-objcopy not found: ${LLVM_OBJCOPY}" >&2
  exit 1
}
[[ -x "${LLVM_OBJDUMP}" ]] || {
  echo "error: llvm-objdump not found: ${LLVM_OBJDUMP}" >&2
  exit 1
}

resource_bin="${OUT_DIR}/v5-resource-main.bin"
"${LLVM_OBJCOPY}" \
  --dump-section ".ventus.resource.main=${resource_bin}" "${rt_elfs[0]}"

python3 - "${resource_bin}" <<'PY'
import struct
import sys

data = open(sys.argv[1], "rb").read()
if len(data) != 96:
    raise SystemExit(f"unexpected Ventus resource v4 size: {len(data)}")
words = struct.unpack("<24I", data)
version = words[0]
pds_static = words[8]
feature_flags = words[14]
pds_layout = words[15]
private_layout = words[16]
hot_offset = words[17]
hot_stride = words[18]
continuation_offset = words[19]
continuation_bytes = words[20]
reserved = words[23]

if version != 4:
    raise SystemExit(f"unexpected resource version: {version}")
if feature_flags & 0x3 != 0x3:
    raise SystemExit(
        f"missing traversal/trace-continuation flags: 0x{feature_flags:x}"
    )
if (pds_layout, private_layout) != (5, 5):
    raise SystemExit(
        f"unexpected V5 layouts: pds={pds_layout} private={private_layout}"
    )
if hot_stride != 384:
    raise SystemExit(f"unexpected hot-PDS stride: {hot_stride}")
if continuation_offset < hot_offset + hot_stride or continuation_bytes == 0:
    raise SystemExit(
        "continuation range is empty or overlaps the hot PDS: "
        f"hot={hot_offset}+{hot_stride} "
        f"continuation={continuation_offset}+{continuation_bytes}"
    )
if pds_static < continuation_offset + continuation_bytes:
    raise SystemExit(
        f"static PDS extent {pds_static} does not cover the continuation range"
    )
if reserved != 0:
    raise SystemExit(f"resource reserved field is nonzero: 0x{reserved:x}")

print(
    "PASS V5 resource "
    f"flags=0x{feature_flags:x} hot={hot_offset}+{hot_stride} "
    f"continuation={continuation_offset}+{continuation_bytes} "
    f"pds_static={pds_static}"
)
PY

disassembly="${OUT_DIR}/v5-kernel.dis"
"${LLVM_OBJDUMP}" -d "${rt_elfs[0]}" >"${disassembly}"
traverse_count="$(rg -c 'vt\.rt\.traverse' "${disassembly}" || true)"
release_count="$(rg -c 'vt\.rt\.release' "${disassembly}" || true)"
[[ "${traverse_count}" -ge 2 && "${release_count}" -ge 2 ]] || {
  echo "error: generated ELF does not contain primary and child V5 RT paths " \
       "(traverse=${traverse_count}, release=${release_count})" >&2
  exit 1
}
echo "PASS V5 ELF traverse=${traverse_count} release=${release_count} elf=${rt_elfs[0]}"
