DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export VENTUS_INSTALL_PREFIX=${DIR}/install
export PATH=${VENTUS_INSTALL_PREFIX}/bin:$PATH
export LD_LIBRARY_PATH=${VENTUS_INSTALL_PREFIX}/lib:${LD_LIBRARY_PATH:-}
export POCL_DEVICES="ventus"
export OCL_ICD_VENDORS=${VENTUS_INSTALL_PREFIX}/lib/libpocl.so

# see https://pcn2po10nqam.feishu.cn/wiki/XHNXwIdRkiFtZDkCW6Mc58Uon3b
export POCL_ENABLE_UNINIT=1

# remove extra colons
export LD_LIBRARY_PATH=$(echo "$LD_LIBRARY_PATH" | sed -e 's/^:*//' -e 's/:*$//')

# Add ventus indicator to prompt (like conda)
if [ -z "$_VENTUS_ENV_PROMPT_SAVED" ]; then
    export _VENTUS_ENV_PROMPT_SAVED="$PS1"
fi
export PS1="(ventus) $_VENTUS_ENV_PROMPT_SAVED"

# echo "✅ Ventus environment configured successfully!"
# echo "   VENTUS_INSTALL_PREFIX: ${VENTUS_INSTALL_PREFIX}"
# echo "   POCL_DEVICES: ${POCL_DEVICES}"
