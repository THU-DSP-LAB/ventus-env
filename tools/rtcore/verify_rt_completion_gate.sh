#!/usr/bin/env bash
set -euo pipefail

# Negative integration gate: a missing checker and a linked image whose first
# frontier was replaced by a store must both fail pipeline creation, before
# the ELF path is handed to the driver. Run against the same rebuilt ICD/LLVM
# as the positive image gates (MESA_BUILD / LLVM_BUILD).
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LLVM_BUILD="${LLVM_BUILD:-${ROOT_DIR}/llvm/build}"
OUT_BASE="${OUT_BASE:-${ROOT_DIR}/artifacts/rtcore-spike/completion-gate-negative}"
mkdir -p "${OUT_BASE}"
OUT_BASE="$(cd "${OUT_BASE}" && pwd)"
ulimit -c 0

die() { echo "error: $*" >&2; exit 1; }

cat >"${OUT_BASE}/corrupt-link.py" <<'PY'
#!/usr/bin/env python3
import os
import struct
import subprocess
import sys
from pathlib import Path

subprocess.run([os.environ['FRONTIER_TEST_REAL_LLD'], *sys.argv[1:]], check=True)
elf = Path(sys.argv[sys.argv.index('-o') + 1])
data = bytearray(elf.read_bytes())
assert data[:7] == b'\x7fELF\x01\x01\x01', 'expected ELF32'
shoff = struct.unpack_from('<I', data, 32)[0]
entsize, count = struct.unpack_from('<HH', data, 46)
for index in range(count):
    section = struct.unpack_from('<10I', data, shoff + index * entsize)
    if section[1] != 1 or not section[2] & 4:
        continue
    for offset in range(section[4], section[4] + section[5], 4):
        word = struct.unpack_from('<I', data, offset)[0]
        if word & 0xFE0FF07F == 0x2A003057:
            struct.pack_into('<I', data, offset, 0x00012023)  # sw zero, 0(sp)
            elf.write_bytes(data)
            print('frontier-test: replaced marker with store', flush=True)
            sys.exit(0)
raise SystemExit('frontier-test: no marker to corrupt')
PY
chmod +x "${OUT_BASE}/corrupt-link.py"

reject_case() {
  local label="$1"
  shift
  if env OUT_DIR="${OUT_BASE}/${label}" RUN_1X1=1 RUN_16X16=0 \
      RUN_160X96=0 RUN_320X192=0 VENTUS_VK_RETAIN_SHADER_ARTIFACTS=0 \
      VENTUS_VK_DUMP_LLVM=0 VENTUS_SPIKE_LOG=1 "$@" \
      "${ROOT_DIR}/tools/rtcore/run_full_app_spike.sh"; then
    die "${label} unexpectedly passed"
  fi
  local case_dir="${OUT_BASE}/${label}/1x1"
  local log="${case_dir}/raytracingshadows.log"
  rg -q 'RT V5 final ELF completion frontier failed' "${log}" ||
    die "${label} did not fail at the final ELF gate"
  if rg -q 'RT pipeline compiled|emitted RT RISC-V kernel' "${log}"; then
    die "${label} handed off a rejected executable"
  fi
  [[ ! -e "${case_dir}/raytracingshadows_spike.ppm" ]] ||
    die "${label} executed and produced an image"
  if find "${case_dir}/elf" -maxdepth 1 -type f -print -quit | rg -q .; then
    die "${label} retained partial artifacts or execution logs"
  fi
  echo "PASS ${label}: rejected before ELF handoff; partial artifacts cleaned"
}

reject_case unavailable-checker \
  VENTUS_VK_RT_COMPLETION_CHECKER="${OUT_BASE}/absent-checker.py"
reject_case corrupted-frontier \
  FRONTIER_TEST_REAL_LLD="${LLVM_BUILD}/bin/ld.lld" \
  VENTUS_VK_LD_LLD="${OUT_BASE}/corrupt-link.py"
rg -q 'frontier-test: replaced marker with store' \
  "${OUT_BASE}/corrupted-frontier/1x1/raytracingshadows.log" ||
  die 'corruption injection did not execute'
rg -q 'non-whitelisted instruction before frontier' \
  "${OUT_BASE}/corrupted-frontier/1x1/raytracingshadows.log" ||
  die 'checker did not identify the premature store'
