DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export VENTUS_INSTALL_PREFIX=${DIR}/install
export PATH=${VENTUS_INSTALL_PREFIX}/bin:$PATH
export LD_LIBRARY_PATH=${VENTUS_INSTALL_PREFIX}/lib:${LD_LIBRARY_PATH:-}
export POCL_DEVICES="ventus"
export OCL_ICD_VENDORS=${VENTUS_INSTALL_PREFIX}/lib/libpocl.so
export SYSTEMC_HOME=${VENTUS_INSTALL_PREFIX}/systemc

# see https://pcn2po10nqam.feishu.cn/wiki/XHNXwIdRkiFtZDkCW6Mc58Uon3b
export POCL_ENABLE_UNINIT=1

# remove extra colons
export LD_LIBRARY_PATH=$(echo "$LD_LIBRARY_PATH" | sed -e 's/^:*//' -e 's/:*$//')

