DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
#export VENTUS_ENV_ROOT=${DIR}
export VENTUS_INSTALL_PREFIX=${DIR}/install
export PATH=${VENTUS_INSTALL_PREFIX}/bin:$PATH
export LD_LIBRARY_PATH=${VENTUS_INSTALL_PREFIX}/lib:${LD_LIBRARY_PATH:-}
export POCL_DEVICES="ventus"
export OCL_ICD_VENDORS=${VENTUS_INSTALL_PREFIX}/lib/libpocl.so

# see https://pcn2po10nqam.feishu.cn/wiki/XHNXwIdRkiFtZDkCW6Mc58Uon3b
export POCL_ENABLE_UNINIT=1

# PTX backend (driver/ptx_device) uses sbt_ptx + Spike's encoding.h.
#export GPU_SBT_ENCODING_H=${VENTUS_INSTALL_PREFIX}/share/ventus/spike/encoding.h
#export GPU_SBT_WANT_FILE=${VENTUS_INSTALL_PREFIX}/share/ventus/spike_want.txt

# remove extra colons
export LD_LIBRARY_PATH=$(echo "$LD_LIBRARY_PATH" | sed -e 's/^:*//' -e 's/:*$//')
