#!/usr/bin/env bash
# Shared gh api wrapper with bounded retry handling for GitHub API rate limits.
#
# Source this file from scripts that already run with set -euo pipefail.
# The wrapper preserves gh api stdout on success and stderr on failure.

_gh_api_retry_after_seconds() {
  local text="$1"
  local retry_after

  retry_after=$(printf '%s\n' "$text" | awk '
    BEGIN { IGNORECASE = 1 }
    /^[[:space:]]*retry-after:[[:space:]]*[0-9]+/ {
      gsub("\r", "")
      print $2
      exit
    }')

  if [[ "$retry_after" =~ ^[0-9]+$ ]]; then
    echo "$retry_after"
    return 0
  fi

  return 1
}

_gh_api_is_rate_limited() {
  local text="$1"

  printf '%s\n' "$text" | grep -qiE \
    'API rate limit exceeded|secondary rate limit|rate_limit|abuse detection|Retry-After|exceeded a secondary rate limit'
}

gh_api_with_retry() {
  local max_attempts="${GH_API_MAX_ATTEMPTS:-8}"
  local base_delay="${GH_API_BASE_DELAY_SECONDS:-15}"
  local max_delay="${GH_API_MAX_DELAY_SECONDS:-300}"
  local attempt=1
  local response=""
  local err=""
  local cached_input_file=""
  local args=("$@")
  local i

  [[ "$max_attempts" =~ ^[0-9]+$ ]] || max_attempts=8
  [[ "$base_delay" =~ ^[0-9]+$ ]] || base_delay=15
  [[ "$max_delay" =~ ^[0-9]+$ ]] || max_delay=300

  # gh api --input - consumes stdin. Cache it once so retries can replay it.
  for ((i = 0; i < ${#args[@]}; i++)); do
    if [ "${args[$i]}" = "--input" ]; then
      local next_index=$((i + 1))
      if [ "$next_index" -lt "${#args[@]}" ] && [ "${args[$next_index]}" = "-" ]; then
        cached_input_file=$(mktemp)
        cat > "$cached_input_file"
        args[$next_index]="$cached_input_file"
        break
      fi
    elif [ "${args[$i]}" = "--input=-" ]; then
      cached_input_file=$(mktemp)
      cat > "$cached_input_file"
      args[$i]="--input=${cached_input_file}"
      break
    fi
  done

  while [ "$attempt" -le "$max_attempts" ]; do
    local out_file
    local err_file
    out_file=$(mktemp)
    err_file=$(mktemp)

    if gh api "${args[@]}" >"$out_file" 2>"$err_file"; then
      cat "$out_file"
      rm -f "$out_file" "$err_file" "$cached_input_file"
      return 0
    fi

    response=$(cat "$out_file" 2>/dev/null || true)
    err=$(cat "$err_file" 2>/dev/null || true)
    rm -f "$out_file" "$err_file"

    if _gh_api_is_rate_limited "$(printf '%s\n%s' "$response" "$err")" && [ "$attempt" -lt "$max_attempts" ]; then
      local wait_seconds
      wait_seconds=$(_gh_api_retry_after_seconds "$(printf '%s\n%s' "$response" "$err")" || true)

      if [ -z "$wait_seconds" ]; then
        wait_seconds=$((attempt * base_delay))
        if [ "$wait_seconds" -gt "$max_delay" ]; then
          wait_seconds="$max_delay"
        fi
      fi

      if [ "$wait_seconds" -lt 1 ]; then
        wait_seconds=1
      fi

      echo "  GitHub API rate limit hit; waiting ${wait_seconds} seconds before retry (attempt ${attempt}/${max_attempts})" >&2
      sleep "$wait_seconds"
      attempt=$((attempt + 1))
      continue
    fi

    [ -n "$response" ] && echo "$response"
    [ -n "$err" ] && echo "$err" >&2
    rm -f "$cached_input_file"
    return 1
  done

  [ -n "$response" ] && echo "$response"
  [ -n "$err" ] && echo "$err" >&2
  rm -f "$cached_input_file"
  return 1
}
