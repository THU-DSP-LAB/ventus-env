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
      compat|global)
        printf '%s\n' "${selector}"
        return 0
        ;;
      mirror)
        echo "error: RT execution profile mirror has been removed" >&2
        return 2
        ;;
      *)
        echo "error: invalid VENTUS_VK_RT_EXECUTION_PROFILE=${selector}" >&2
        return 2
        ;;
    esac
  fi

  if ventus_rt_profile_value_enabled "${VENTUS_VK_RT_WAVEFRONT_MIRROR:-}"; then
    echo "error: VENTUS_VK_RT_WAVEFRONT_MIRROR has been removed" >&2
    return 2
  fi

  if [[ -v VENTUS_VK_RT_WAVEFRONT_GLOBAL ]]; then
    if ventus_rt_profile_value_enabled "${VENTUS_VK_RT_WAVEFRONT_GLOBAL}"; then
      printf 'global\n'
    else
      printf 'compat\n'
    fi
    return 0
  fi

  printf 'global\n'
}
