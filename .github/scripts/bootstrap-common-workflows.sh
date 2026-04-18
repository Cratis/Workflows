#!/usr/bin/env bash
# Propagates common wrapper workflows to all Cratis repositories so that
# each repository has a standard set of reusable workflows.
#
# Currently bootstraps:
#   - cleanup-pr-artifacts.yml  — cleans up PR-published GitHub Packages
#   - update-packages.yml       — weekly package updates (NuGet + NPM)
#
# Called by .github/workflows/bootstrap-common-workflows.yml after checkout.
#
# This script handles both initial bootstrap and ongoing updates:
#   - New repos:            adds missing wrapper workflows
#   - Already-bootstrapped: updates wrappers if their content has changed
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

# ================================================================
# Wrapper workflow definitions (base64-encoded)
# ================================================================
# To verify the encoded content, run:  echo "<value>" | base64 -d
#
# Each entry: path in target repo -> base64 content

declare -A WRAPPERS

# cleanup-pr-artifacts.yml — triggers on PR close, delegates to reusable workflow
# Decodes to:
#   name: Cleanup PR Artifacts
#   on:
#     pull_request:
#       types: [closed]
#   jobs:
#     cleanup:
#       uses: Cratis/Workflows/.github/workflows/cleanup-pr-artifacts.yml@main
#       with:
#         pull_request: ${{ github.event.pull_request.number }}
#       secrets: inherit
WRAPPERS[".github/workflows/cleanup-pr-artifacts.yml"]="bmFtZTogQ2xlYW51cCBQUiBBcnRpZmFjdHMKCm9uOgogIHB1bGxfcmVxdWVzdDoKICAgIHR5cGVzOiBbY2xvc2VkXQoKam9iczoKICBjbGVhbnVwOgogICAgdXNlczogQ3JhdGlzL1dvcmtmbG93cy8uZ2l0aHViL3dvcmtmbG93cy9jbGVhbnVwLXByLWFydGlmYWN0cy55bWxAbWFpbgogICAgd2l0aDoKICAgICAgcHVsbF9yZXF1ZXN0OiAke3sgZ2l0aHViLmV2ZW50LnB1bGxfcmVxdWVzdC5udW1iZXIgfX0KICAgIHNlY3JldHM6IGluaGVyaXQK"

# update-packages.yml — nightly scheduled + manual trigger, delegates to reusable workflow
# Decodes to:
#   name: Update Packages
#   on:
#     schedule:
#       - cron: '0 6 * * *'
#     workflow_dispatch:
#   jobs:
#     update:
#       uses: Cratis/Workflows/.github/workflows/update-packages.yml@main
#       secrets:
#         PAT_WORKFLOWS: ${{ secrets.PAT_WORKFLOWS }}
WRAPPERS[".github/workflows/update-packages.yml"]="bmFtZTogVXBkYXRlIFBhY2thZ2VzCgpvbjoKICBzY2hlZHVsZToKICAgIC0gY3JvbjogJzAgNiAqICogKicKICB3b3JrZmxvd19kaXNwYXRjaDoKCmpvYnM6CiAgdXBkYXRlOgogICAgdXNlczogQ3JhdGlzL1dvcmtmbG93cy8uZ2l0aHViL3dvcmtmbG93cy91cGRhdGUtcGFja2FnZXMueW1sQG1haW4KICAgIHNlY3JldHM6CiAgICAgIFBBVF9XT1JLRkxPV1M6ICR7eyBzZWNyZXRzLlBBVF9XT1JLRkxPV1MgfX0K"

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
  # 3. Get the full recursive tree to check existing files
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
  # 4. Create blobs and build tree entries for all wrapper workflows
  # ----------------------------------------------------------------
  tree_entries="[]"
  has_changes=false
  commit_parts=()

  for wrapper_path in "${!WRAPPERS[@]}"; do
    wrapper_b64="${WRAPPERS[$wrapper_path]}"
    wrapper_name=$(basename "$wrapper_path" .yml)

    # Create blob
    blob_error=$(mktemp)
    _blob_resp=$(gh api -X POST "repos/Cratis/$repo/git/blobs" \
      -f content="$wrapper_b64" -f encoding=base64 \
      2>"$blob_error" || true)
    blob_sha=$(extract_sha "$_blob_resp")

    if [ -z "$blob_sha" ]; then
      blob_err=$(cat "$blob_error" 2>/dev/null || true)
      echo "  ⚠ Could not create blob for $wrapper_name in $repo"
      [ -n "$blob_err" ] && echo "    blob error: $blob_err"
      rm -f "$blob_error"
      echo "$repo" >> "$failures_file"
      continue 2  # skip entire repo on blob failure
    fi
    rm -f "$blob_error"

    # Check if file already matches
    existing_sha=$(echo "$subtree" | jq -r \
      --arg path "$wrapper_path" \
      '.tree[] | select(.path == $path) | .sha' \
      2>/dev/null || true)

    if [ "$existing_sha" = "$blob_sha" ]; then
      echo "  ℹ $wrapper_name already up-to-date"
      continue
    fi

    has_changes=true
    if [ -n "$existing_sha" ]; then
      commit_parts+=("update $wrapper_name")
    else
      commit_parts+=("add $wrapper_name")
    fi

    # Add tree entry
    tree_entries=$(echo "$tree_entries" | jq \
      --arg path "$wrapper_path" \
      --arg sha  "$blob_sha" \
      '. + [{path: $path, mode: "100644", type: "blob", sha: $sha}]')
  done

  if [ "$has_changes" != "true" ]; then
    echo "  ℹ No changes needed for $repo"
    continue
  fi

  # ----------------------------------------------------------------
  # 5. Create the new tree object
  # ----------------------------------------------------------------
  new_tree_json=$(jq -n \
    --arg base_tree "$tree_sha" \
    --argjson tree "$tree_entries" \
    '{base_tree: $base_tree, tree: $tree}')

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
  # 6. Create the commit
  # ----------------------------------------------------------------
  # Join commit parts: "Add/Update cleanup-pr-artifacts, add update-packages"
  commit_message=$(IFS=', '; echo "Bootstrap common workflows: ${commit_parts[*]}")
  commit_message="${commit_message^}"  # Capitalize first letter

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
  # 7. Push commit directly to the default branch
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

  echo "  ✓ Pushed to $default_branch in $repo: $commit_message"
done

total_failures=$(wc -l < "$failures_file" 2>/dev/null || echo "0")
rm -f "$failures_file"

echo ""
echo "Summary: $total_failures failure(s)"

if [ "$total_failures" -gt 0 ]; then
  echo "::error::$total_failures repo(s) failed. Check the log above for details."
  exit 1
fi
