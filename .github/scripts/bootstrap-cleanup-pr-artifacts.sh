#!/usr/bin/env bash
# Propagates the cleanup-pr-artifacts wrapper workflow to all Cratis
# repositories so that each repository automatically cleans up PR-published
# GitHub Packages when a pull request is closed.
#
# Called by .github/workflows/bootstrap-cleanup-pr-artifacts.yml after checkout.
#
# This script handles both initial bootstrap and ongoing updates:
#   - New repos:            adds .github/workflows/cleanup-pr-artifacts.yml
#   - Already-bootstrapped: updates the wrapper if its content has changed
#   - Up-to-date repos:     skips processing
#
# Expects:
#   GH_TOKEN      - PAT with repo + Workflows permissions; the PAT owner must
#                   be a bypass actor on target repos' branch protection rulesets
#                   so that direct pushes to the default branch are allowed.
#   REPOS_FILE    - path to a JSON file containing the repos array
#                   (written by the "Get all Cratis repositories" step)
#   REPOS_IGNORE  - JSON array of repo names to skip (e.g. '["Workflows"]')

set -euo pipefail

# Extract a SHA from a gh api JSON response.  Returns empty string if:
#   - the response is empty
#   - the jq path does not exist
#   - the value is not a valid 40-char hex SHA
# The regex also accepts 64-char hashes to remain forward-compatible with
# GitHub's planned SHA-256 transition (currently all SHAs are 40-char SHA-1).
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

repos_file="${REPOS_FILE:-$GITHUB_WORKSPACE/repos.json}"
repos_ignore="${REPOS_IGNORE:-[\"Workflows\"]}"
repos=$(cat "$repos_file")

failures_file=$(mktemp)

# Pre-computed base64 content for the wrapper workflow.
# Using base64 avoids heredoc end-markers at column 0.
#
# To verify the encoded content matches the wrapper format, run:
#   echo "<wrapper_b64 value>" | base64 -d
#
# wrapper_b64 decodes to:
#   name: Cleanup PR Artifacts
#
#   on:
#     pull_request:
#       types: [closed]
#
#   jobs:
#     cleanup:
#       uses: Cratis/Workflows/.github/workflows/cleanup-pr-artifacts.yml@main
#       with:
#         pull_request: ${{ github.event.pull_request.number }}
#       secrets: inherit
wrapper_b64="bmFtZTogQ2xlYW51cCBQUiBBcnRpZmFjdHMKCm9uOgogIHB1bGxfcmVxdWVzdDoKICAgIHR5cGVzOiBbY2xvc2VkXQoKam9iczoKICBjbGVhbnVwOgogICAgdXNlczogQ3JhdGlzL1dvcmtmbG93cy8uZ2l0aHViL3dvcmtmbG93cy9jbGVhbnVwLXByLWFydGlmYWN0cy55bWxAbWFpbgogICAgd2l0aDoKICAgICAgcHVsbF9yZXF1ZXN0OiAke3sgZ2l0aHViLmV2ZW50LnB1bGxfcmVxdWVzdC5udW1iZXIgfX0KICAgIHNlY3JldHM6IGluaGVyaXQK"

# ================================================================
# Pre-flight: verify PAT has write permission on target repositories
# ================================================================
probe_repo=$(echo "$repos" | jq -r \
  --argjson ignore "$repos_ignore" \
  '[.[] | select(. as $n | ($ignore | index($n)) == null)][0] // empty')
if [ -n "$probe_repo" ]; then
  probe_perms=$(gh api "repos/Cratis/$probe_repo" --jq '.permissions.push // false' 2>/dev/null || true)
  if [ "$probe_perms" != "true" ]; then
    echo "::error::PAT_WORKFLOWS does not have write (push) access to Cratis/$probe_repo."
    echo "The PAT must be configured with:"
    echo "  • Resource owner: Cratis"
    echo "  • Repository access: All repositories"
    echo "  • Permissions → Contents: Read and write"
    echo "  • Permissions → Workflows: Read and write"
    echo "Update the PAT at: https://github.com/settings/personal-access-tokens"
    exit 1
  fi
  echo "✓ PAT has write access to Cratis/$probe_repo (pre-flight check passed)"
fi

echo "$repos" | jq -r '.[]' | while read -r repo; do
  # Skip repos in the ignore list
  if echo "$repos_ignore" | jq -e --arg r "$repo" 'index($r) != null' >/dev/null 2>&1; then
    echo "Skipping $repo (in ignore list)"
    continue
  fi

  echo "Processing Cratis/$repo..."

  # ----------------------------------------------------------------
  # 1. Get default branch and HEAD SHA
  # ----------------------------------------------------------------
  repo_info_error=$(mktemp)
  default_branch=$(gh api "repos/Cratis/$repo" \
    --jq '.default_branch' 2>"$repo_info_error" || true)
  if [ -z "$default_branch" ]; then
    repo_info_api_error=$(cat "$repo_info_error" 2>/dev/null || true)
    echo "  ⚠ Could not get default branch for $repo, skipping"
    [ -n "$repo_info_api_error" ] && echo "    API error: $repo_info_api_error"
    rm -f "$repo_info_error"
    continue
  fi
  rm -f "$repo_info_error"

  head_sha_error=$(mktemp)
  _head_sha_resp=$(gh api "repos/Cratis/$repo/git/ref/heads/$default_branch" \
    2>"$head_sha_error" || true)
  head_sha=$(extract_sha "$_head_sha_resp" '.object.sha')
  if [ -z "$head_sha" ]; then
    head_sha_api_error=$(cat "$head_sha_error" 2>/dev/null || true)
    echo "  ⚠ Could not get HEAD SHA for $repo ($default_branch branch not found), skipping"
    [ -n "$head_sha_api_error" ] && echo "    API error: $head_sha_api_error"
    rm -f "$head_sha_error"
    continue
  fi
  rm -f "$head_sha_error"

  # ----------------------------------------------------------------
  # 2. Get the commit's tree SHA
  # ----------------------------------------------------------------
  tree_sha_error=$(mktemp)
  _tree_sha_resp=$(gh api "repos/Cratis/$repo/git/commits/$head_sha" \
    2>"$tree_sha_error" || true)
  tree_sha=$(extract_sha "$_tree_sha_resp" '.tree.sha')
  if [ -z "$tree_sha" ]; then
    tree_sha_api_error=$(cat "$tree_sha_error" 2>/dev/null || true)
    echo "  ⚠ Could not get tree SHA for $repo, skipping"
    [ -n "$tree_sha_api_error" ] && echo "    API error: $tree_sha_api_error"
    rm -f "$tree_sha_error"
    continue
  fi
  rm -f "$tree_sha_error"

  # ----------------------------------------------------------------
  # 3. Get the full recursive tree to check existing file
  # ----------------------------------------------------------------
  subtree_error=$(mktemp)
  subtree=$(gh api "repos/Cratis/$repo/git/trees/$tree_sha?recursive=1" \
    2>"$subtree_error" || true)
  if [ -z "$subtree" ]; then
    subtree_api_error=$(cat "$subtree_error" 2>/dev/null || true)
    echo "  ⚠ Could not get tree for $repo, skipping"
    [ -n "$subtree_api_error" ] && echo "    API error: $subtree_api_error"
    rm -f "$subtree_error"
    continue
  fi
  rm -f "$subtree_error"

  # ----------------------------------------------------------------
  # 4. Create blob for the wrapper workflow file
  # ----------------------------------------------------------------
  wrapper_blob_error=$(mktemp)
  _wrapper_blob_resp=$(gh api -X POST "repos/Cratis/$repo/git/blobs" \
    -f content="$wrapper_b64" -f encoding=base64 \
    2>"$wrapper_blob_error" || true)
  wrapper_blob_sha=$(extract_sha "$_wrapper_blob_resp")

  if [ -z "$wrapper_blob_sha" ]; then
    wrapper_err=$(cat "$wrapper_blob_error" 2>/dev/null || true)
    echo "  ⚠ Could not create blob for $repo"
    [ -n "$wrapper_err" ] && echo "    blob error: $wrapper_err"
    rm -f "$wrapper_blob_error"
    echo "$repo" >> "$failures_file"
    continue
  fi
  rm -f "$wrapper_blob_error"

  # ----------------------------------------------------------------
  # 5. Check if the wrapper file already matches (idempotency)
  # ----------------------------------------------------------------
  existing_wrapper=$(echo "$subtree" | jq -r \
    '.tree[] | select(.path == ".github/workflows/cleanup-pr-artifacts.yml") | .sha' \
    2>/dev/null || true)

  if [ "$existing_wrapper" = "$wrapper_blob_sha" ]; then
    echo "  ℹ No changes needed for $repo"
    continue
  fi

  # ----------------------------------------------------------------
  # 6. Build the new tree JSON
  # ----------------------------------------------------------------
  new_tree_json=$(jq -n \
    --arg base_tree "$tree_sha" \
    --arg path ".github/workflows/cleanup-pr-artifacts.yml" \
    --arg sha  "$wrapper_blob_sha" \
    '{
      base_tree: $base_tree,
      tree: [
        {path: $path, mode: "100644", type: "blob", sha: $sha}
      ]
    }')

  # ----------------------------------------------------------------
  # 7. Create the new tree object
  # ----------------------------------------------------------------
  tree_error=$(mktemp)
  _new_tree_resp=$(echo "$new_tree_json" | \
    gh api -X POST "repos/Cratis/$repo/git/trees" \
    --input - 2>"$tree_error" || true)
  new_tree_sha=$(extract_sha "$_new_tree_resp")

  if [ -z "$new_tree_sha" ]; then
    tree_api_error=$(cat "$tree_error" 2>/dev/null || true)
    echo "  ⚠ Could not create tree for $repo"
    if echo "$tree_api_error" | grep -qi '403'; then
      echo "    API error: $tree_api_error"
      echo "    → PAT lacks 'Workflows: Read and write' for this repo."
      echo "    → Update PAT at https://github.com/settings/personal-access-tokens"
    else
      [ -n "$tree_api_error" ] && echo "    API error: $tree_api_error"
    fi
    rm -f "$tree_error"
    echo "$repo" >> "$failures_file"
    continue
  fi
  rm -f "$tree_error"

  # ----------------------------------------------------------------
  # 8. Create the commit
  # ----------------------------------------------------------------
  commit_message="Add cleanup-pr-artifacts workflow"
  if [ -n "$existing_wrapper" ]; then
    commit_message="Update cleanup-pr-artifacts workflow"
  fi

  commit_error=$(mktemp)
  _commit_resp=$(jq -n \
    --arg msg    "$commit_message" \
    --arg tree   "$new_tree_sha" \
    --arg parent "$head_sha" \
    '{"message": $msg, "tree": $tree, "parents": [$parent]}' | \
    gh api -X POST "repos/Cratis/$repo/git/commits" \
    --input - 2>"$commit_error" || true)
  new_commit_sha=$(extract_sha "$_commit_resp")

  if [ -z "$new_commit_sha" ]; then
    commit_api_error=$(cat "$commit_error" 2>/dev/null || true)
    echo "  ⚠ Could not create commit for $repo"
    [ -n "$commit_api_error" ] && echo "    API error: $commit_api_error"
    rm -f "$commit_error"
    echo "$repo" >> "$failures_file"
    continue
  fi
  rm -f "$commit_error"

  # ----------------------------------------------------------------
  # 9. Push commit directly to the default branch
  #
  # A fast-forward (non-force) PATCH updates the ref only if the new
  # commit is a descendant of the current HEAD — safe against races.
  # The PAT owner must be configured as a bypass actor on the target
  # repository's branch protection ruleset for this push to succeed.
  # ----------------------------------------------------------------
  push_error=$(mktemp)
  push_result=$(gh api -X PATCH "repos/Cratis/$repo/git/refs/heads/$default_branch" \
    -f sha="$new_commit_sha" \
    -F force=false \
    2>"$push_error" || true)
  updated_sha=$(extract_sha "$push_result" '.object.sha')

  if [ -z "$updated_sha" ]; then
    push_api_error=$(cat "$push_error" 2>/dev/null || true)
    push_msg=$(echo "$push_result" | jq -r '.message // empty' 2>/dev/null || true)
    echo "  ⚠ Could not push commit to $default_branch in $repo"
    [ -n "$push_api_error" ] && echo "    API error: $push_api_error"
    [ -n "$push_msg" ]       && echo "    GitHub message: $push_msg"
    rm -f "$push_error"
    echo "$repo" >> "$failures_file"
    continue
  fi
  rm -f "$push_error"

  echo "  ✓ Pushed $commit_message directly to $default_branch in $repo"
done

total_failures=$(wc -l < "$failures_file" 2>/dev/null || echo "0")
rm -f "$failures_file"

echo ""
echo "Summary: $total_failures failure(s)"

if [ "$total_failures" -gt 0 ]; then
  echo "::error::$total_failures repo(s) failed. Check the log above for details."
  exit 1
fi
