DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
#export VENTUS_ENV_ROOT=${DIR}
export VENTUS_INSTALL_PREFIX="$(realpath "${VENTUS_INSTALL_PREFIX:-${DIR}/install}")"
export PATH=${VENTUS_INSTALL_PREFIX}/bin:$PATH
export LD_LIBRARY_PATH=${VENTUS_INSTALL_PREFIX}/lib:${LD_LIBRARY_PATH:-}
export POCL_DEVICES="ventus"
export OCL_ICD_VENDORS=${VENTUS_INSTALL_PREFIX}/lib/libpocl.so
export CL_ICD_FILENAMES=${VENTUS_INSTALL_PREFIX}/lib/libpocl.so
export VENTUS_SPIKE_LOG=0

# V5 final linked images must pass the matching completion-frontier checker
# before Mesa exposes them to the driver. Missing tools fail closed.
export VENTUS_VK_RT_COMPLETION_CHECKER="${VENTUS_VK_RT_COMPLETION_CHECKER:-${DIR}/tools/rtcore.py}"
export VENTUS_VK_LLVM_OBJDUMP="${VENTUS_VK_LLVM_OBJDUMP:-${VENTUS_INSTALL_PREFIX}/bin/llvm-objdump}"

# see https://pcn2po10nqam.feishu.cn/wiki/XHNXwIdRkiFtZDkCW6Mc58Uon3b
export POCL_ENABLE_UNINIT=1

# remove extra colons
export LD_LIBRARY_PATH=$(echo "$LD_LIBRARY_PATH" | sed -e 's/^:*//' -e 's/:*$//')
