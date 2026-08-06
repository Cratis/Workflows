#!/usr/bin/env bash
# Propagates Copilot instruction files from the source repository to a single
# target repository in the Cratis organization, pushing the commit directly to
# the default branch (no PR).
# Called by .github/workflows/propagate-copilot-instructions.yml for each
# matrix job (one per target repository).
#
# Expects:
#   GH_TOKEN      - PAT with Contents (r/w).  The PAT owner must be a bypass
#                   actor on the target repository's branch protection ruleset
#                   so that the direct push to the default branch is allowed.
#   SOURCE_REPO   - source repository in owner/repo format (e.g. Cratis/AI)
#   TARGET_REPO   - target repository name (e.g. Chronicle)
#   COPILOT_SOURCE_FILES_PATH - optional prepared artifact directory containing
#                               copilot-files.json and blobs/*.b64

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=github-api-retry.sh
source "${SCRIPT_DIR}/github-api-retry.sh"

# Extract a SHA from a gh api JSON response.  Returns empty string if:
#   - the response is empty
#   - the jq path does not exist
#   - the value is not a valid 40-char hex SHA
# Usage: sha=$(extract_sha "$response" '.sha')
extract_sha() {
  local response="$1" jq_path="${2:-.sha}"
  local val
  val=$(echo "$response" | jq -r "$jq_path // empty" 2>/dev/null || true)
  # Validate: must look like a git SHA (40 or 64 hex chars)
  if [[ "$val" =~ ^[0-9a-f]{40,64}$ ]]; then
    echo "$val"
  fi
}

source_repo="${SOURCE_REPO:?SOURCE_REPO must be set}"
repo="${TARGET_REPO:?TARGET_REPO must be set}"
source_files_path="${COPILOT_SOURCE_FILES_PATH:-}"
source_blobs_dir=""

# ----------------------------------------------------------------
# Fetch or load Copilot files from the source repository
# ----------------------------------------------------------------
if [ -n "$source_files_path" ]; then
  source_files_path="${source_files_path%/}"
  source_blobs_dir="${source_files_path}/blobs"
  copilot_files_file="${source_files_path}/copilot-files.json"

  echo "Using prepared Copilot source artifact from ${source_files_path}..."

  if [ ! -f "$copilot_files_file" ]; then
    echo "::error::Missing prepared Copilot file list: ${copilot_files_file}"
    exit 1
  fi
  if [ ! -d "$source_blobs_dir" ]; then
    echo "::error::Missing prepared Copilot blob directory: ${source_blobs_dir}"
    exit 1
  fi

  copilot_files=$(jq -c '.' "$copilot_files_file" 2>/dev/null || true)
  if [ -z "$copilot_files" ]; then
    echo "::error::Invalid prepared Copilot file list: ${copilot_files_file}"
    exit 1
  fi
else
  source_files_path=$(mktemp -d)
  source_blobs_dir="${source_files_path}/blobs"

  SOURCE_REPO="$source_repo" \
    OUTPUT_DIR="$source_files_path" \
    bash "${SCRIPT_DIR}/prepare-copilot-source-artifact.sh"

  copilot_files_file="${source_files_path}/copilot-files.json"
  copilot_files=$(jq -c '.' "$copilot_files_file" 2>/dev/null || true)
  if [ -z "$copilot_files" ]; then
    echo "::error::Invalid prepared Copilot file list: ${copilot_files_file}"
    exit 1
  fi
fi

if [ -z "$copilot_files" ] || [ "$copilot_files" = "[]" ]; then
  echo "No Copilot instruction files found in ${source_repo} — nothing to propagate."
  exit 0
fi
echo "✓ Found $(echo "$copilot_files" | jq 'length') Copilot file(s) in ${source_repo}"

echo "Processing Cratis/${repo}..."

# ----------------------------------------------------------------
# 1. Get default branch and HEAD SHA
# ----------------------------------------------------------------
repo_info_error=$(mktemp)
default_branch=$(gh_api_with_retry "repos/Cratis/${repo}" \
  --jq '.default_branch' \
  2>"$repo_info_error" || true)
if [ -z "$default_branch" ]; then
  repo_info_api_error=$(cat "$repo_info_error" 2>/dev/null || true)
  echo "::error::Could not get default branch for ${repo}"
  [ -n "$repo_info_api_error" ] && echo "  API error: $repo_info_api_error"
  rm -f "$repo_info_error"
  exit 1
fi
rm -f "$repo_info_error"

head_sha_error=$(mktemp)
_head_sha_resp=$(gh_api_with_retry "repos/Cratis/${repo}/git/ref/heads/${default_branch}" \
  2>"$head_sha_error" || true)
head_sha=$(extract_sha "$_head_sha_resp" '.object.sha')
if [ -z "$head_sha" ]; then
  head_sha_api_error=$(cat "$head_sha_error" 2>/dev/null || true)
  echo "::error::Could not get HEAD SHA for ${repo} (${default_branch} branch not found)"
  [ -n "$head_sha_api_error" ] && echo "  API error: $head_sha_api_error"
  rm -f "$head_sha_error"
  exit 1
fi
rm -f "$head_sha_error"

# ----------------------------------------------------------------
# 2. Get the commit's tree SHA and current full tree
# ----------------------------------------------------------------
tree_sha_error=$(mktemp)
_tree_sha_resp=$(gh_api_with_retry "repos/Cratis/${repo}/git/commits/${head_sha}" \
  2>"$tree_sha_error" || true)
tree_sha=$(extract_sha "$_tree_sha_resp" '.tree.sha')
if [ -z "$tree_sha" ]; then
  tree_sha_api_error=$(cat "$tree_sha_error" 2>/dev/null || true)
  echo "::error::Could not get tree SHA for ${repo}"
  [ -n "$tree_sha_api_error" ] && echo "  API error: $tree_sha_api_error"
  rm -f "$tree_sha_error"
  exit 1
fi
rm -f "$tree_sha_error"

subtree_error=$(mktemp)
subtree=$(gh_api_with_retry "repos/Cratis/${repo}/git/trees/${tree_sha}?recursive=1" \
  2>"$subtree_error" || true)
if [ -z "$subtree" ]; then
  subtree_api_error=$(cat "$subtree_error" 2>/dev/null || true)
  echo "::error::Could not get tree for ${repo}"
  [ -n "$subtree_api_error" ] && echo "  API error: $subtree_api_error"
  rm -f "$subtree_error"
  exit 1
fi
rm -f "$subtree_error"

# ----------------------------------------------------------------
# 3. Work out which files actually differ in the target
#    (git blob SHAs are content-addressed across repositories, so an
#     identical SHA on both sides means identical content)
#
# Only the differing files are uploaded.  Uploading the whole corpus to every
# repository on every run costs one write call per file per repository, which
# exhausts the API write allowance long before the matrix finishes — the rest
# of the repositories then fail with a rate limit that no amount of retrying
# can clear.
# ----------------------------------------------------------------
# The file list and the target tree are passed to jq as files, never as
# command-line arguments: a recursive tree of a large repository is far past
# the per-argument length limit and would fail with "Argument list too long".
copilot_files_file_json=$(mktemp)
subtree_file_json=$(mktemp)
printf '%s' "$copilot_files" > "$copilot_files_file_json"
printf '%s' "$subtree" > "$subtree_file_json"

changed_files=$(jq -n \
  --slurpfile src "$copilot_files_file_json" \
  --slurpfile tgt "$subtree_file_json" \
  '(($tgt[0].tree // []) | map({key: .path, value: .}) | from_entries) as $existing
   | $src[0]
   | map(
       (.mode // "100644") as $m
       | select(($existing[.path].sha // "") != .sha
                or ($existing[.path].mode // "") != $m)
       | {path: .path, sha: .sha, mode: $m}
     )' 2>/dev/null || true)

if [ -z "$changed_files" ]; then
  rm -f "$copilot_files_file_json" "$subtree_file_json"
  echo "::error::Could not determine which files differ in ${repo}"
  exit 1
fi

if [ "$changed_files" = "[]" ]; then
  rm -f "$copilot_files_file_json" "$subtree_file_json"
  echo "ℹ No changes needed for ${repo} (files already up to date)"
  exit 0
fi

echo "→ $(echo "$changed_files" | jq 'length') file(s) differ in ${repo}"

# ----------------------------------------------------------------
# 3b. Find target entries whose type collides with the source shape
#
# The corpus mixes files, directories and symlinks at the same paths across
# repositories: `.github/instructions` is a folder symlink in one repository
# and a real directory in another, `.github/agents` is a directory of per-file
# adapters here and a folder symlink there.  A tree entry cannot replace an
# entry of the opposite kind — the Git Data API rejects the whole tree with
# `GitRPC::BadObjectState (HTTP 422)` — so the colliding entries are removed
# in a preparatory commit before the sync commit adds the new shape.
# ----------------------------------------------------------------
changed_files_file_json=$(mktemp)
printf '%s' "$changed_files" > "$changed_files_file_json"

conflict_deletions=$(jq -n \
  --slurpfile changed "$changed_files_file_json" \
  --slurpfile tgt "$subtree_file_json" \
  '($changed[0] | map(.path)) as $paths
   | [ ($tgt[0].tree // [])[]
       | select(.type == "blob")
       | . as $entry
       | select(
           ($paths | any(. as $p | $entry.path | startswith($p + "/")))
           or ($paths | any(. as $p | $p | startswith($entry.path + "/")))
         )
       | {path: $entry.path, mode: $entry.mode}
     ]
   | unique_by(.path)' 2>/dev/null || true)

rm -f "$copilot_files_file_json" "$subtree_file_json" "$changed_files_file_json"

if [ -z "$conflict_deletions" ]; then
  conflict_deletions='[]'
fi

if [ "$conflict_deletions" != "[]" ]; then
  echo "→ $(echo "$conflict_deletions" | jq 'length') colliding path(s) in ${repo} will be removed first:"
  echo "$conflict_deletions" | jq -r '.[] | "    " + .path'
fi

# ----------------------------------------------------------------
# 4. Create blobs in the target repository for the differing files
# ----------------------------------------------------------------
new_tree_json=$(jq -n --arg base_tree "$tree_sha" \
  '{"base_tree": $base_tree, "tree": []}')

while IFS=$'\t' read -r src_path src_sha src_mode; do
  [ -z "$src_path" ] && continue

  if [ -n "$source_blobs_dir" ]; then
    blob_file="${source_blobs_dir}/${src_sha}.b64"
    if [ ! -f "$blob_file" ]; then
      echo "::error::Prepared source artifact is missing blob for ${src_path} (${src_sha})"
      exit 1
    fi
    clean_b64=$(tr -d '\n' < "$blob_file")
  else
    # Fetch blob content from source repo (returned as base64 by API).
    # NOTE: zero-byte files return {"content":"","encoding":"base64"} — the
    # content field is legitimately empty.  We must check whether the API call
    # itself succeeded (non-empty JSON response), not whether content is empty.
    blob_error=$(mktemp)
    blob_resp=$(gh_api_with_retry "repos/${source_repo}/git/blobs/${src_sha}" \
      2>"$blob_error" || true)
    blob_api_error=$(cat "$blob_error" 2>/dev/null || true)
    rm -f "$blob_error"

    if [ -z "$blob_resp" ]; then
      echo "::error::Could not fetch blob for ${src_path} from ${source_repo}"
      [ -n "$blob_api_error" ] && echo "  API error: $blob_api_error"
      exit 1
    fi

    # Extract content; empty string is valid for zero-byte files
    blob_content=$(echo "$blob_resp" | jq -r '.content' 2>/dev/null || true)

    # Strip embedded newlines that the API inserts into base64 output
    clean_b64=$(echo "$blob_content" | tr -d '\n')
  fi

  target_blob_error=$(mktemp)
  _target_blob_resp=$(jq -n \
    --arg content "$clean_b64" \
    '{"content": $content, "encoding": "base64"}' | \
    gh_api_with_retry -X POST "repos/Cratis/${repo}/git/blobs" \
    --input - \
    2>"$target_blob_error" || true)
  target_blob_api_error=$(cat "$target_blob_error" 2>/dev/null || true)
  rm -f "$target_blob_error"
  target_blob_sha=$(extract_sha "$_target_blob_resp")

  if [ -z "$target_blob_sha" ]; then
    echo "::error::Could not create blob for ${src_path} in ${repo}"
    [ -n "$target_blob_api_error" ] && echo "  API error: $target_blob_api_error"
    exit 1
  fi

  new_tree_json=$(echo "$new_tree_json" | jq \
    --arg p "$src_path" \
    --arg s "$target_blob_sha" \
    --arg m "$src_mode" \
    '.tree += [{path: $p, mode: $m, type: "blob", sha: $s}]')
done <<< "$(echo "$changed_files" | jq -r '.[] | .path + "\t" + .sha + "\t" + (.mode // "100644")' 2>/dev/null || true)"

# ----------------------------------------------------------------
# 5. Create new tree(s) and commit(s)
#
# These helpers write their diagnostics to stderr: they run inside command
# substitution, so anything on stdout would be captured as the returned SHA
# instead of being reported.
# ----------------------------------------------------------------
create_tree_or_fail() {
  local payload="$1" label="$2"
  local error_file resp sha api_error
  error_file=$(mktemp)
  resp=$(printf '%s' "$payload" | \
    gh_api_with_retry -X POST "repos/Cratis/${repo}/git/trees" \
    --input - 2>"$error_file" || true)
  sha=$(extract_sha "$resp")
  if [ -z "$sha" ]; then
    api_error=$(cat "$error_file" 2>/dev/null || true)
    echo "::error::Could not create tree for ${repo} (${label})" >&2
    [ -n "$api_error" ] && echo "  API error: $api_error" >&2
    rm -f "$error_file"
    exit 1
  fi
  rm -f "$error_file"
  printf '%s' "$sha"
}

create_commit_or_fail() {
  local tree="$1" parent="$2" message="$3"
  local error_file resp sha api_error
  error_file=$(mktemp)
  resp=$(jq -n \
    --arg msg "$message" \
    --arg tree "$tree" \
    --arg parent "$parent" \
    '{"message": $msg, "tree": $tree, "parents": [$parent]}' | \
    gh_api_with_retry -X POST "repos/Cratis/${repo}/git/commits" \
    --input - 2>"$error_file" || true)
  sha=$(extract_sha "$resp")
  if [ -z "$sha" ]; then
    api_error=$(cat "$error_file" 2>/dev/null || true)
    echo "::error::Could not create commit for ${repo} (${message})" >&2
    [ -n "$api_error" ] && echo "  API error: $api_error" >&2
    rm -f "$error_file"
    exit 1
  fi
  rm -f "$error_file"
  printf '%s' "$sha"
}

parent_sha="$head_sha"
base_tree_sha="$tree_sha"

if [ "$conflict_deletions" != "[]" ]; then
  deletion_tree_json=$(jq -n \
    --arg base_tree "$base_tree_sha" \
    --argjson deletions "$conflict_deletions" \
    '{base_tree: $base_tree,
      tree: ($deletions | map({path: .path, mode: .mode, type: "blob", sha: null}))}')

  deletion_tree_sha=$(create_tree_or_fail "$deletion_tree_json" "removing colliding paths")
  parent_sha=$(create_commit_or_fail "$deletion_tree_sha" "$parent_sha" \
    "Remove adapter paths that collide with the Copilot instruction shape")
  base_tree_sha="$deletion_tree_sha"

  echo "✓ Created commit ${parent_sha} in ${repo} removing colliding paths"

  new_tree_json=$(echo "$new_tree_json" | jq \
    --arg base_tree "$base_tree_sha" \
    '.base_tree = $base_tree')
fi

new_tree_sha=$(create_tree_or_fail "$new_tree_json" "syncing Copilot instructions")
new_commit_sha=$(create_commit_or_fail "$new_tree_sha" "$parent_sha" \
  "Sync Copilot instructions from ${source_repo}")

echo "✓ Created commit ${new_commit_sha} in ${repo}"

# ----------------------------------------------------------------
# 6. Push commit directly to the default branch
#
# A fast-forward (non-force) PATCH updates the ref only if the new
# commit is a descendant of the current HEAD — safe against races.
# The PAT owner must be configured as a bypass actor on the target
# repository's branch protection ruleset for this push to succeed.
# ----------------------------------------------------------------
push_error=$(mktemp)
push_result=$(gh_api_with_retry -X PATCH "repos/Cratis/${repo}/git/refs/heads/${default_branch}" \
  -f sha="$new_commit_sha" \
  -F force=false \
  2>"$push_error" || true)
updated_sha=$(extract_sha "$push_result" '.object.sha')

if [ -z "$updated_sha" ]; then
  push_api_error=$(cat "$push_error" 2>/dev/null || true)
  push_msg=$(echo "$push_result" | jq -r '.message // empty' 2>/dev/null || true)
  echo "::error::Could not push commit to ${default_branch} in ${repo}"
  [ -n "$push_api_error" ] && echo "  API error: $push_api_error"
  [ -n "$push_msg" ]       && echo "  GitHub message: $push_msg"
  rm -f "$push_error"
  exit 1
fi
rm -f "$push_error"

echo "✓ Pushed Copilot instructions directly to ${default_branch} in ${repo}"
