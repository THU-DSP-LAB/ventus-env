#!/usr/bin/env bash

ventus_rt_execution_profile() {
  local name
  for name in \
    VENTUS_VK_RT_EXECUTION_PROFILE \
    VENTUS_VK_INTERNAL_RT_ORACLE_PROFILE \
    VENTUS_VK_RT_WAVEFRONT_GLOBAL \
    VENTUS_VK_RT_WAVEFRONT_MIRROR; do
    if [[ -v "${name}" ]]; then
      echo "error: ${name} has been removed; use the pipeline pNext contract" >&2
      return 2
    fi
  done

  printf 'megakernel\n'
}
