#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
env_root=$(cd -- "$script_dir/.." && pwd)
gpgpu_root="$env_root/gpgpu"

if [[ -n "${VERILATOR:-}" ]]; then
  verilator_bin=$VERILATOR
elif command -v verilator >/dev/null 2>&1; then
  verilator_bin=$(command -v verilator)
else
  echo "error: verilator is not in PATH; set VERILATOR=/path/to/verilator" >&2
  exit 2
fi

scratch=$(mktemp -d /tmp/rtcore-rcp-rtl-sweep.XXXXXX)
cleanup() {
  rm -rf -- "$scratch"
}
trap cleanup EXIT

(
  cd -- "$gpgpu_root"
  ./mill -i 'ventus[6.4.0].runMain' \
    rtcore.RtFp32ReciprocalGen "$scratch/rtl"
)

"$verilator_bin" --cc --exe --build -j "${RTCORE_RCP_JOBS:-4}" -O3 \
  --top-module RtFp32Reciprocal \
  --Mdir "$scratch/obj" \
  -CFLAGS "-O3 -std=c++17 -I$env_root/spike/riscv" \
  "$scratch/rtl/RtFp32Reciprocal.sv" \
  "$script_dir/rtcore-rcp-rtl-sweep.cc"

"$scratch/obj/VRtFp32Reciprocal"
