#!/usr/bin/env bash
# Fetches Copilot source files once and writes a reusable artifact directory.
#
# Expects:
#   GH_TOKEN    - PAT with read access to SOURCE_REPO
#   SOURCE_REPO - source repository in owner/repo format
#   OUTPUT_DIR  - directory to populate with copilot-files.json and blobs/*.b64
#
# The artifact normalizes known adapter paths so broadcast propagation keeps the
# Cratis AI corpus shape intact. A source repository may already contain copied
# content at an adapter path; propagation should repair that back to an adapter
# when the matching canonical .ai file exists in the source tree.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=github-api-retry.sh
source "${SCRIPT_DIR}/github-api-retry.sh"

source_repo="${SOURCE_REPO:?SOURCE_REPO must be set}"
output_dir="${OUTPUT_DIR:?OUTPUT_DIR must be set}"
blobs_dir="${output_dir}/blobs"

source_tree_has_path() {
  local path="$1"

  echo "$source_tree_raw" | jq -e \
    --arg p "$path" \
    '.tree[] | select(.path == $p)' >/dev/null 2>&1
}

source_tree_has_prefix() {
  local prefix="$1"

  echo "$source_tree_raw" | jq -e \
    --arg p "$prefix" \
    '.tree[] | select(.path | startswith($p))' >/dev/null 2>&1
}

adapter_spec_for_path() {
  local path="$1"
  local file name target

  case "$path" in
    AGENTS.md)
      source_tree_has_path ".ai/rules/general.md" && printf '120000\t.ai/rules/general.md\n'
      ;;
    .github/copilot-instructions.md)
      source_tree_has_path ".ai/rules/general.md" && printf '100644\t../.ai/rules/general.md\n'
      ;;
    .claude/CLAUDE.md)
      source_tree_has_path ".ai/rules/general.md" && printf '120000\t../.ai/rules/general.md\n'
      ;;
    .agents/skills)
      source_tree_has_prefix ".ai/skills/" && printf '120000\t../.ai/skills\n'
      ;;
    .github/prompts)
      source_tree_has_prefix ".ai/prompts/" && printf '120000\t../.ai/prompts\n'
      ;;
    .github/skills)
      source_tree_has_prefix ".ai/skills/" && printf '120000\t../.ai/skills\n'
      ;;
    .claude/agents)
      source_tree_has_prefix ".ai/agents/" && printf '120000\t../.ai/agents\n'
      ;;
    .claude/skills)
      source_tree_has_prefix ".ai/skills/" && printf '120000\t../.ai/skills\n'
      ;;
    .github/instructions/*.instructions.md)
      file="${path##*/}"
      name="${file%.instructions.md}"
      target=".ai/rules/${name}.md"
      source_tree_has_path "$target" && printf '100644\t../../.ai/rules/%s.md\n' "$name"
      ;;
    .claude/rules/*.md)
      file="${path##*/}"
      name="${file%.md}"
      target=".ai/rules/${name}.md"
      source_tree_has_path "$target" && printf '120000\t../../.ai/rules/%s.md\n' "$name"
      ;;
    .github/agents/*.agent.md)
      file="${path##*/}"
      name="${file%.agent.md}"
      target=".ai/agents/${name}.md"
      source_tree_has_path "$target" && printf '120000\t../../.ai/agents/%s.md\n' "$name"
      ;;
    .claude/commands/*.md)
      file="${path##*/}"
      name="${file%.md}"
      target=".ai/prompts/${name}.prompt.md"
      source_tree_has_path "$target" && printf '120000\t../../.ai/prompts/%s.prompt.md\n' "$name"
      ;;
  esac
}

write_synthetic_blob() {
  local content="$1"
  local sha

  sha=$(printf '%s' "$content" | git hash-object --stdin)
  printf '%s' "$content" | base64 | tr -d '\n' > "${blobs_dir}/${sha}.b64"
  printf '%s' "$sha"
}

normalize_adapter_entries() {
  local normalized='[]'
  local file_path file_sha file_mode spec adapter_mode adapter_target adapter_sha

  while IFS=$'\t' read -r file_path file_sha file_mode; do
    [ -z "$file_path" ] && continue

    spec=$(adapter_spec_for_path "$file_path" || true)
    if [ -n "$spec" ]; then
      IFS=$'\t' read -r adapter_mode adapter_target <<< "$spec"
      adapter_sha=$(write_synthetic_blob "$adapter_target")
      echo "Normalized adapter ${file_path} -> ${adapter_target}" >&2
      normalized=$(echo "$normalized" | jq -c \
        --arg p "$file_path" \
        --arg s "$adapter_sha" \
        --arg m "$adapter_mode" \
        '. + [{path: $p, sha: $s, mode: $m}]')
      continue
    fi

    normalized=$(echo "$normalized" | jq -c \
      --arg p "$file_path" \
      --arg s "$file_sha" \
      --arg m "${file_mode:-100644}" \
      '. + [{path: $p, sha: $s, mode: $m}]')
  done <<< "$(echo "$copilot_files" | jq -r '.[] | .path + "\t" + .sha + "\t" + (.mode // "100644")' 2>/dev/null || true)"

  echo "$normalized"
}

mkdir -p "$blobs_dir"

echo "Fetching Copilot instruction files from ${source_repo}..."
source_tree_raw=$(gh_api_with_retry "repos/${source_repo}/git/trees/HEAD?recursive=1")

if [ -z "$source_tree_raw" ]; then
  echo "::error::Could not fetch tree from ${source_repo}"
  exit 1
fi

printf '%s' "$source_tree_raw" > "${output_dir}/source-tree.json"

# .agents/PROJECT.md is never synced.  It is by definition the project-local
# instruction file — the one place a repository records what is true only of
# itself (its endpoints, credentials, deployment recipes, issue tracker) — so
# propagating it overwrites every repository's context with whichever repo
# happened to push last.  Excluded here rather than left to each repository's
# .copilot-sync-ignore, because that file only protects the repo it lives in:
# a single repo without one is enough to clobber all the others.
copilot_files=$(echo "$source_tree_raw" | jq -c \
  '[.tree[] | select(.type == "blob") |
   select(.path | test("^(AGENTS\\.md$|\\.agents(/|$)|\\.github/(copilot-instructions\\.md$|instructions(/|$)|agents(/|$)|skills(/|$)|prompts(/|$)|hooks(/|$))|\\.ai/|\\.claude/)")) |
   select(.path != ".claude/settings.local.json" and .path != ".agents/PROJECT.md") |
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
  copilot_files=$(normalize_adapter_entries)
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
