#!/usr/bin/env bash
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
# Waits for the latest code run of a pull request workflow for HEAD_SHA and mirrors its result.
# A code run is a run whose title ends with " <CODE_MARKER>" (the caller's run-name appends it).
set -euo pipefail

: "${GH_TOKEN:?}" "${HEAD_SHA:?}" "${WORKFLOW_FILE:?}" "${CODE_MARKER:?}"
api="${GITHUB_API_URL:-https://api.github.com}"
repo="${GITHUB_REPOSITORY:?}"
timeout="${TIMEOUT_SECONDS:-5400}"
poll="${POLL_SECONDS:-30}"
deadline=$((SECONDS + timeout))

find_code_run() {
  # Newest first; follow pages until a code run is found.
  local page=1 body found
  while [ "$page" -le 20 ]; do
    body=$(curl -fsS --retry 3 -H "Authorization: Bearer ${GH_TOKEN}" -H "Accept: application/vnd.github+json" \
      "${api}/repos/${repo}/actions/workflows/${WORKFLOW_FILE}/runs?head_sha=${HEAD_SHA}&event=pull_request&per_page=100&page=${page}")
    found=$(printf '%s' "$body" | jq -c --arg m "$CODE_MARKER" \
      '[.workflow_runs[] | select((.display_title // "") | endswith(" " + $m))] | sort_by(.created_at) | last // empty')
    if [ -n "$found" ]; then
      printf '%s' "$found"
      return 0
    fi
    [ "$(printf '%s' "$body" | jq '.workflow_runs | length')" -ge 100 ] || return 0
    page=$((page + 1))
  done
}

while true; do
  code=$(find_code_run)
  if [ -z "$code" ]; then
    echo "::error title=code run::No code run found for ${HEAD_SHA}; push a commit or re-run the pull request workflow."
    exit 1
  fi
  status=$(printf '%s' "$code" | jq -r '.status')
  url=$(printf '%s' "$code" | jq -r '.html_url')
  if [ "$status" = "completed" ]; then
    conclusion=$(printf '%s' "$code" | jq -r '.conclusion')
    if [ "$conclusion" = "success" ]; then
      echo "Code run ${url} succeeded for ${HEAD_SHA}."
      exit 0
    fi
    echo "::error title=code run::The code run ${url} for ${HEAD_SHA} ended ${conclusion}; fix it or re-run it."
    exit 1
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "::error title=code run::Timed out waiting for ${url} (${status})."
    exit 1
  fi
  echo "Waiting for ${url} (${status})"
  sleep "$poll"
done
