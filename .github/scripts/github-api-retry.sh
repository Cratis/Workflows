#!/usr/bin/env bash
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
#
# Shared gh api wrapper with bounded retry handling for rate limits and transient failures.
# Source this file from scripts that already run with set -euo pipefail.
# The wrapper preserves gh api stdout on success and stderr on terminal failure.

_gh_api_retry_after_seconds() {
  local text="$1"
  local retry_after
  local reset_epoch
  local now

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

  reset_epoch=$(printf '%s\n' "$text" | awk '
    BEGIN { IGNORECASE = 1 }
    /^[[:space:]]*x-ratelimit-reset:[[:space:]]*[0-9]+/ {
      gsub("\r", "")
      print $2
      exit
    }')
  if [[ "$reset_epoch" =~ ^[0-9]+$ ]]; then
    now=$(date +%s)
    if [ "$reset_epoch" -gt "$now" ]; then
      echo $((reset_epoch - now + 1))
    else
      echo 1
    fi
    return 0
  fi

  return 1
}

_gh_api_is_rate_limited() {
  local text="$1"

  printf '%s\n' "$text" | grep -qiE \
    'API rate limit exceeded|secondary rate limit|rate_limit|abuse detection|Retry-After|x-ratelimit-reset|exceeded a secondary rate limit'
}

_gh_api_is_transient_failure() {
  local text="$1"

  printf '%s\n' "$text" | grep -qiE \
    'HTTP (5[0-9]{2})|status code (5[0-9]{2})|500 Internal Server Error|502 Bad Gateway|503 Service Unavailable|504 Gateway Timeout|connection (reset|refused|closed)|connection timed out|operation timed out|context deadline exceeded|TLS handshake timeout|temporary failure|unexpected EOF|stream error'
}

_gh_api_retry_label() {
  local text="$1"

  if _gh_api_is_rate_limited "$text"; then
    echo "rate limit"
  else
    echo "transient failure"
  fi
}

gh_api_with_retry() {
  local max_attempts="${GH_API_MAX_ATTEMPTS:-8}"
  local base_delay="${GH_API_BASE_DELAY_SECONDS:-15}"
  local max_delay="${GH_API_MAX_DELAY_SECONDS:-300}"
  local max_jitter="${GH_API_MAX_JITTER_SECONDS:-5}"
  local retry_mode="${GH_API_RETRY_MODE:-auto}"
  local attempt=1
  local response=""
  local err=""
  local cached_input_file=""
  local args=("$@")
  local i

  [[ "$max_attempts" =~ ^[0-9]+$ ]] || max_attempts=8
  [[ "$base_delay" =~ ^[0-9]+$ ]] || base_delay=15
  [[ "$max_delay" =~ ^[0-9]+$ ]] || max_delay=300
  [[ "$max_jitter" =~ ^[0-9]+$ ]] || max_jitter=5
  [ "$max_attempts" -gt 0 ] || max_attempts=1

  # gh api --input - consumes stdin. Cache it once so retries can replay it.
  for ((i = 0; i < ${#args[@]}; i++)); do
    if [ "${args[$i]}" = "--input" ]; then
      local next_index=$((i + 1))
      if [ "$next_index" -lt "${#args[@]}" ] && [ "${args[$next_index]}" = "-" ]; then
        cached_input_file=$(mktemp)
        cat > "$cached_input_file"
        args[next_index]="$cached_input_file"
        break
      fi
    elif [ "${args[$i]}" = "--input=-" ]; then
      cached_input_file=$(mktemp)
      cat > "$cached_input_file"
      args[i]="--input=${cached_input_file}"
      break
    fi
  done

  while [ "$attempt" -le "$max_attempts" ]; do
    local out_file
    local err_file
    local combined
    local wait_seconds
    local retry_label
    local jitter=0

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
    combined=$(printf '%s\n%s' "$response" "$err")

    if [ "$retry_mode" != "never" ] &&
       { _gh_api_is_rate_limited "$combined" || _gh_api_is_transient_failure "$combined"; } &&
       [ "$attempt" -lt "$max_attempts" ]; then
      wait_seconds=$(_gh_api_retry_after_seconds "$combined" || true)
      if [ -z "$wait_seconds" ]; then
        wait_seconds=$((attempt * base_delay))
      fi
      if [ "$max_jitter" -gt 0 ]; then
        jitter=$((RANDOM % (max_jitter + 1)))
        wait_seconds=$((wait_seconds + jitter))
      fi
      if [ "$wait_seconds" -gt "$max_delay" ]; then
        wait_seconds="$max_delay"
      fi
      if [ "$wait_seconds" -lt 1 ]; then
        wait_seconds=1
      fi

      retry_label=$(_gh_api_retry_label "$combined")
      echo "  GitHub API $retry_label; waiting ${wait_seconds} seconds before retry (attempt ${attempt}/${max_attempts})" >&2
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
