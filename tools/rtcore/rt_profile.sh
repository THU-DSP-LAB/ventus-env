#!/usr/bin/env bash

ventus_rt_profile_value_enabled() {
  local value="${1:-}"
  [[ -n "${value}" &&
     "${value}" != 0 &&
     "${value}" != false &&
     "${value}" != FALSE &&
     "${value}" != off &&
     "${value}" != OFF ]]
}

ventus_rt_execution_profile() {
  local selector="${VENTUS_VK_RT_EXECUTION_PROFILE:-}"
  if [[ -n "${selector}" ]]; then
    case "${selector}" in
      compat|global|mirror)
        printf '%s\n' "${selector}"
        return 0
        ;;
      *)
        echo "error: invalid VENTUS_VK_RT_EXECUTION_PROFILE=${selector}" >&2
        return 2
        ;;
    esac
  fi

  if [[ -v VENTUS_VK_RT_WAVEFRONT_GLOBAL ]]; then
    if ventus_rt_profile_value_enabled "${VENTUS_VK_RT_WAVEFRONT_GLOBAL}"; then
      printf 'global\n'
    else
      printf 'compat\n'
    fi
    return 0
  fi
  if ventus_rt_profile_value_enabled "${VENTUS_VK_RT_WAVEFRONT_MIRROR:-}"; then
    printf 'mirror\n'
    return 0
  fi

  printf 'global\n'
}
