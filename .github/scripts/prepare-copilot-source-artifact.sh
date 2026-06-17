#!/usr/bin/env bash
# Fetches Copilot source files once and writes a reusable artifact directory.
#
# Expects:
#   GH_TOKEN    - PAT with read access to SOURCE_REPO
#   SOURCE_REPO - source repository in owner/repo format
#   OUTPUT_DIR  - directory to populate with copilot-files.json and blobs/*.b64

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=github-api-retry.sh
source "${SCRIPT_DIR}/github-api-retry.sh"

source_repo="${SOURCE_REPO:?SOURCE_REPO must be set}"
output_dir="${OUTPUT_DIR:?OUTPUT_DIR must be set}"
blobs_dir="${output_dir}/blobs"

mkdir -p "$blobs_dir"

normalize_source_path() {
  python3 - "$1" "$2" <<'PY'
import os.path
import sys

print(os.path.normpath(os.path.join(sys.argv[1], sys.argv[2])))
PY
}

is_path_reference_adapter() {
  case "$1" in
    AGENTS.md | \
    .github/copilot-instructions.md | \
    .github/instructions/* | \
    .github/agents/* | \
    .claude/CLAUDE.md | \
    .claude/rules/* | \
    .claude/commands/*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

looks_like_adapter_target() {
  local target="$1"

  [ -n "$target" ] &&
    [ "${#target}" -le 512 ] &&
    [[ "$target" != /* ]] &&
    [[ "$target" != *$'\n'* ]] &&
    [[ "$target" != *$'\r'* ]]
}

find_real_blob_for_path() {
  local resolved_path="$1"

  echo "$source_tree_raw" | jq -r \
    --arg p "$resolved_path" \
    '.tree[] |
     select(.path == $p and .type == "blob" and .mode != "120000") |
     [.sha, (.mode // "100644")] |
     @tsv' 2>/dev/null | head -1
}

resolve_adapter_entries() {
  local resolved='[]'
  local file_path file_sha file_mode
  local blob_content adapter_target resolved_path real_blob real_sha real_mode

  while IFS=$'\t' read -r file_path file_sha file_mode; do
    [ -z "$file_path" ] && continue

    if [ "$file_mode" = "120000" ] || is_path_reference_adapter "$file_path"; then
      blob_content=$(gh_api_with_retry "repos/${source_repo}/git/blobs/${file_sha}" \
        --jq '.content' 2>/dev/null || true)
      adapter_target=$(printf '%s' "$blob_content" | base64 -d 2>/dev/null || true)

      if looks_like_adapter_target "$adapter_target"; then
        adapter_target=$(printf '%s' "$adapter_target" | tr -d '\r\n')
        resolved_path=$(normalize_source_path "$(dirname "$file_path")" "$adapter_target")
        real_blob=$(find_real_blob_for_path "$resolved_path")

        if [ -n "$real_blob" ]; then
          IFS=$'\t' read -r real_sha real_mode <<< "$real_blob"
          echo "Resolved adapter ${file_path} -> ${resolved_path}" >&2
          resolved=$(echo "$resolved" | jq -c \
            --arg p "$file_path" \
            --arg s "$real_sha" \
            --arg m "${real_mode:-100644}" \
            '. + [{path: $p, sha: $s, mode: $m}]')
          continue
        fi
      fi
    fi

    resolved=$(echo "$resolved" | jq -c \
      --arg p "$file_path" \
      --arg s "$file_sha" \
      --arg m "${file_mode:-100644}" \
      '. + [{path: $p, sha: $s, mode: $m}]')
  done <<< "$(echo "$copilot_files" | jq -r '.[] | .path + "\t" + .sha + "\t" + (.mode // "100644")' 2>/dev/null || true)"

  echo "$resolved"
}

echo "Fetching Copilot instruction files from ${source_repo}..."
source_tree_raw=$(gh_api_with_retry "repos/${source_repo}/git/trees/HEAD?recursive=1")

if [ -z "$source_tree_raw" ]; then
  echo "::error::Could not fetch tree from ${source_repo}"
  exit 1
fi

printf '%s' "$source_tree_raw" > "${output_dir}/source-tree.json"

copilot_files=$(echo "$source_tree_raw" | jq -c \
  '[.tree[] | select(.type == "blob") |
   select(.path | test("^(AGENTS\\.md$|\\.agents(/|$)|\\.github/(copilot-instructions\\.md$|instructions(/|$)|agents(/|$)|skills(/|$)|prompts(/|$)|hooks(/|$))|\\.ai/|\\.claude/)")) |
   select(.path != ".claude/settings.local.json") |
   {path: .path, sha: .sha, mode: .mode}]' 2>/dev/null || true)

if [ -z "$copilot_files" ] || [ "$copilot_files" = "[]" ]; then
  echo "No Copilot instruction files found in ${source_repo}."
  copilot_files="[]"
else
  echo "Found $(echo "$copilot_files" | jq 'length') Copilot file(s) in ${source_repo}"
fi

# shellcheck source=copilot-sync-ignore-filter.sh
source "${SCRIPT_DIR}/copilot-sync-ignore-filter.sh"

if [ "$copilot_files" != "[]" ]; then
  if ! _apply_copilot_sync_ignore; then
    echo "All Copilot files excluded by .copilot-sync-ignore."
    copilot_files="[]"
  fi
fi

if [ "$copilot_files" != "[]" ]; then
  copilot_files=$(resolve_adapter_entries)
fi

printf '%s' "$copilot_files" > "${output_dir}/copilot-files.json"

file_count=$(echo "$copilot_files" | jq 'length')

if [ "$file_count" -gt 0 ]; then
  echo "$copilot_files" | jq -r '.[].sha' | sort -u | while read -r src_sha; do
    [ -z "$src_sha" ] && continue

    blob_file="${blobs_dir}/${src_sha}.b64"
    [ -f "$blob_file" ] && continue

    blob_resp=$(gh_api_with_retry "repos/${source_repo}/git/blobs/${src_sha}")
    if [ -z "$blob_resp" ]; then
      echo "::error::Could not fetch source blob ${src_sha} from ${source_repo}"
      exit 1
    fi

    encoding=$(echo "$blob_resp" | jq -r '.encoding // empty' 2>/dev/null || true)
    if [ "$encoding" != "base64" ]; then
      echo "::error::Unexpected encoding for source blob ${src_sha}: ${encoding:-missing}"
      exit 1
    fi

    blob_content=$(echo "$blob_resp" | jq -r '.content // empty' 2>/dev/null || true)
    printf '%s' "$blob_content" | tr -d '\n' > "$blob_file"
  done
fi

blob_count=$(find "$blobs_dir" -type f -name '*.b64' | wc -l | tr -d ' ')

jq -n \
  --arg source_repository "$source_repo" \
  --argjson file_count "$file_count" \
  --argjson blob_count "$blob_count" \
  '{source_repository: $source_repository, file_count: $file_count, blob_count: $blob_count}' \
  > "${output_dir}/metadata.json"

echo "Prepared Copilot source artifact with ${file_count} file(s) and ${blob_count} blob(s)."
