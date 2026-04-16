#!/usr/bin/env bash
# Main logic for the Bootstrap Copilot Sync workflow.
# Called by .github/workflows/bootstrap-copilot-sync.yml after checkout.
#
# This script handles both initial bootstrap and ongoing updates for all
# Cratis repositories:
#   - New repos: adds wrapper workflows and copies initial Copilot setup from Cratis/AI.
#   - Already-bootstrapped repos: updates wrapper workflows and Copilot files if needed.
#   - Up-to-date repos: skips processing.
#
# Expects:
#   GH_TOKEN          - PAT with repo + Workflows permissions; the PAT owner must
#                       be a bypass actor on target repos' branch protection rulesets
#                       so that direct pushes to the default branch are allowed.
#   REPOS_FILE        - path to a JSON file containing the repos array
#                       (written by the "Get all Cratis repositories" step)

set -euo pipefail

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

repos_file="${REPOS_FILE:-$GITHUB_WORKSPACE/repos.json}"
repos=$(cat "$repos_file")

failures_file=$(mktemp)

# Pre-computed base64 content for wrapper workflows.
# Using base64 avoids heredoc end-markers at column 0,
# which would terminate the YAML block scalar prematurely.
#
# sync_b64 decodes to:
#   name: Sync Copilot Instructions
#   on:
#     workflow_dispatch:
#       inputs:
#         source_repository:
#           description: 'Source repository (owner/repo format)'
#           required: true
#           type: string
#   jobs:
#     sync:
#       uses: Cratis/Workflows/.github/workflows/sync-copilot-instructions.yml@main
#       with:
#         source_repository: ${{ inputs.source_repository }}
#       secrets: inherit
sync_b64="bmFtZTogU3luYyBDb3BpbG90IEluc3RydWN0aW9ucwoKb246CiAgd29ya2Zsb3dfZGlzcGF0Y2g6CiAgICBpbnB1dHM6CiAgICAgIHNvdXJjZV9yZXBvc2l0b3J5OgogICAgICAgIGRlc2NyaXB0aW9uOiAnU291cmNlIHJlcG9zaXRvcnkgKG93bmVyL3JlcG8gZm9ybWF0KScKICAgICAgICByZXF1aXJlZDogdHJ1ZQogICAgICAgIHR5cGU6IHN0cmluZwoKam9iczoKICBzeW5jOgogICAgdXNlczogQ3JhdGlzL1dvcmtmbG93cy8uZ2l0aHViL3dvcmtmbG93cy9zeW5jLWNvcGlsb3QtaW5zdHJ1Y3Rpb25zLnltbEBtYWluCiAgICB3aXRoOgogICAgICBzb3VyY2VfcmVwb3NpdG9yeTogJHt7IGlucHV0cy5zb3VyY2VfcmVwb3NpdG9yeSB9fQogICAgc2VjcmV0czogaW5oZXJpdAo="
#
# propagate_b64 decodes to:
#   name: Propagate Copilot Instructions
#   on:
#     push:
#       branches: ["main"]
#       paths:
#         - ".ai/**"
#         - ".claude/**"
#         - ".github/copilot-instructions.md"
#         - ".github/instructions/**"
#         - ".github/agents/**"
#         - ".github/skills/**"
#         - ".github/prompts/**"
#         - ".github/hooks/**"
#     workflow_dispatch:
#   jobs:
#     propagate:
#       uses: Cratis/Workflows/.github/workflows/propagate-copilot-instructions.yml@main
#       with:
#         event_name: ${{ github.event_name }}
#       secrets: inherit
propagate_b64="bmFtZTogUHJvcGFnYXRlIENvcGlsb3QgSW5zdHJ1Y3Rpb25zCgpvbjoKICBwdXNoOgogICAgYnJhbmNoZXM6IFsibWFpbiJdCiAgICBwYXRoczoKICAgICAgLSAiLmFpLyoqIgogICAgICAtICIuY2xhdWRlLyoqIgogICAgICAtICIuZ2l0aHViL2NvcGlsb3QtaW5zdHJ1Y3Rpb25zLm1kIgogICAgICAtICIuZ2l0aHViL2luc3RydWN0aW9ucy8qKiIKICAgICAgLSAiLmdpdGh1Yi9hZ2VudHMvKioiCiAgICAgIC0gIi5naXRodWIvc2tpbGxzLyoqIgogICAgICAtICIuZ2l0aHViL3Byb21wdHMvKioiCiAgICAgIC0gIi5naXRodWIvaG9va3MvKioiCiAgd29ya2Zsb3dfZGlzcGF0Y2g6Cgpqb2JzOgogIHByb3BhZ2F0ZToKICAgIHVzZXM6IENyYXRpcy9Xb3JrZmxvd3MvLmdpdGh1Yi93b3JrZmxvd3MvcHJvcGFnYXRlLWNvcGlsb3QtaW5zdHJ1Y3Rpb25zLnltbEBtYWluCiAgICB3aXRoOgogICAgICBldmVudF9uYW1lOiAke3sgZ2l0aHViLmV2ZW50X25hbWUgfX0KICAgIHNlY3JldHM6IGluaGVyaXQK"

# Fetch the Copilot setup tree from Cratis/AI once; reused for every repo.
ai_copilot_files=""
ai_tree_error=$(mktemp)
ai_tree_raw=$(gh api "repos/Cratis/AI/git/trees/main?recursive=1" 2>"$ai_tree_error" || true)
if [ -n "$ai_tree_raw" ]; then
  ai_copilot_files=$(echo "$ai_tree_raw" | jq -c \
    '[.tree[] | select(.type == "blob") |
     select(.path | test("^\\.github/(copilot-instructions\\.md$|instructions/|agents/|skills/|prompts/|hooks/)")) |
     {path: .path, sha: .sha}]' 2>/dev/null || true)
fi
if [ -z "$ai_copilot_files" ] || [ "$ai_copilot_files" = "[]" ]; then
  ai_tree_api_error=$(cat "$ai_tree_error" 2>/dev/null || true)
  echo "⚠ No Copilot setup files found in Cratis/AI; second commit will be skipped"
  [ -n "$ai_tree_api_error" ] && echo "  API error: $ai_tree_api_error"
else
  echo "✓ Found $(echo "$ai_copilot_files" | jq 'length') Copilot setup file(s) in Cratis/AI"
fi
rm -f "$ai_tree_error"

# ================================================================
# Pre-flight: verify PAT has write permission on target repositories
# ================================================================
probe_repo=$(echo "$repos" | jq -r '[.[] | select(. != "Workflows")][0] // empty')
if [ -n "$probe_repo" ]; then
  probe_perms=$(gh api "repos/Cratis/$probe_repo" --jq '.permissions.push // false' 2>/dev/null || true)
  if [ "$probe_perms" != "true" ]; then
    echo "::error::PAT_WORKFLOWS does not have write (push) access to Cratis/$probe_repo."
    echo "The fine-grained PAT must be configured with:"
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
  # Skip this repository (Workflows) — it holds the reusable workflows
  if [ "$repo" = "Workflows" ]; then
    echo "Skipping Workflows (this repository)"
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
  # 3. Get the full recursive tree to find instruction files to delete
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
  # 4. Create blobs for the two workflow files
  #    Blobs are raw content — no path-level permission checks here.
  #    The Workflows permission is checked later when creating the tree.
  # ----------------------------------------------------------------
  sync_blob_error=$(mktemp)
  _sync_blob_resp=$(gh api -X POST "repos/Cratis/$repo/git/blobs" \
    -f content="$sync_b64" -f encoding=base64 \
    2>"$sync_blob_error" || true)
  sync_blob_sha=$(extract_sha "$_sync_blob_resp")
  propagate_blob_error=$(mktemp)
  _prop_blob_resp=$(gh api -X POST "repos/Cratis/$repo/git/blobs" \
    -f content="$propagate_b64" -f encoding=base64 \
    2>"$propagate_blob_error" || true)
  propagate_blob_sha=$(extract_sha "$_prop_blob_resp")

  if [ -z "$sync_blob_sha" ] || [ -z "$propagate_blob_sha" ]; then
    echo "  ⚠ Could not create blobs for $repo"
    if [ -z "$sync_blob_sha" ]; then
      sync_err=$(cat "$sync_blob_error" 2>/dev/null || true)
      [ -n "$sync_err" ] && echo "    sync blob error: $sync_err"
    fi
    if [ -z "$propagate_blob_sha" ]; then
      prop_err=$(cat "$propagate_blob_error" 2>/dev/null || true)
      [ -n "$prop_err" ] && echo "    propagate blob error: $prop_err"
    fi
    rm -f "$sync_blob_error" "$propagate_blob_error"
    echo "$repo" >> "$failures_file"
    continue
  fi
  rm -f "$sync_blob_error" "$propagate_blob_error"

  # ----------------------------------------------------------------
  # 5. Check if workflow files already match and no files need removal
  # ----------------------------------------------------------------
  # Retrieve the current blob SHAs of the two managed workflow files
  # (empty string if the files do not yet exist in the repo).
  existing_sync=$(echo "$subtree" | jq -r \
    '.tree[] | select(.path == ".github/workflows/sync-copilot-instructions.yml") | .sha' \
    2>/dev/null || true)
  existing_propagate=$(echo "$subtree" | jq -r \
    '.tree[] | select(.path == ".github/workflows/propagate-copilot-instructions.yml") | .sha' \
    2>/dev/null || true)

  # List all blob paths under .github/ that belong to Copilot instruction
  # artefacts we want to remove: the root instructions file, plus the
  # instructions/, agents/, skills/, prompts/, and hooks/ sub-directories.
  files_to_delete=$(echo "$subtree" | jq -r \
    '.tree[] | select(.type == "blob") |
     select(.path | test("^\\.github/(copilot-instructions\\.md$|instructions/|agents/|skills/|prompts/|hooks/)")) |
     .path' 2>/dev/null || true)

  # Check whether Copilot files from Cratis/AI are already present in
  # this repo with matching blob SHAs (git blob SHAs are content-addressed,
  # so identical content yields identical SHAs across repositories).
  ai_files_up_to_date=true
  if [ -n "$ai_copilot_files" ] && [ "$ai_copilot_files" != "[]" ]; then
    while IFS=' ' read -r ai_chk_path ai_chk_sha; do
      [ -z "$ai_chk_path" ] && continue
      existing_ai_sha=$(echo "$subtree" | jq -r \
        --arg p "$ai_chk_path" \
        '.tree[] | select(.path == $p) | .sha // empty' 2>/dev/null || true)
      if [ "$existing_ai_sha" != "$ai_chk_sha" ]; then
        ai_files_up_to_date=false
        break
      fi
    done <<< "$(echo "$ai_copilot_files" | jq -r '.[] | .path + " " + .sha' 2>/dev/null || true)"
  fi

  # Check whether any copilot files in the repo need to be cleaned up —
  # i.e., files that match the delete pattern but are not part of the
  # expected AI file set (with the correct SHA).  If AI is up-to-date and
  # every file in files_to_delete is already covered by the AI set, there
  # is nothing to delete; otherwise at least one file needs removal.
  #
  # Pre-build tab-separated "path\tsha" lookup tables once to avoid
  # repeated jq invocations inside the loop.
  repo_copilot_shas=$(echo "$subtree" | jq -r \
    '[.tree[] | select(.type == "blob") |
      select(.path | test("^\\.github/(copilot-instructions\\.md$|instructions/|agents/|skills/|prompts/|hooks/)"))] |
      .[] | .path + "\t" + .sha' 2>/dev/null || true)
  ai_path_sha_set=$(echo "$ai_copilot_files" | jq -r '.[] | .path + "\t" + .sha' 2>/dev/null || true)

  has_files_to_clean=false
  while IFS= read -r del_path; do
    [ -z "$del_path" ] && continue
    del_sha=$(printf '%s' "$repo_copilot_shas" | awk -F'\t' -v p="$del_path" '$1==p{print $2;exit}')
    if ! printf '%s' "$ai_path_sha_set" | grep -qF "$del_path"$'\t'"$del_sha"; then
      has_files_to_clean=true
      break
    fi
  done <<< "$files_to_delete"

  if [ "$existing_sync" = "$sync_blob_sha" ] && \
     [ "$existing_propagate" = "$propagate_blob_sha" ] && \
     [ "$has_files_to_clean" = "false" ] && \
     [ "$ai_files_up_to_date" = "true" ]; then
    echo "  ℹ No changes needed for $repo"
    continue
  fi

  # ----------------------------------------------------------------
  # 6. Build the new tree JSON
  #    - Add the two workflow files (with their blob SHAs)
  #    - Delete instruction files by setting sha to null
  # ----------------------------------------------------------------
  new_tree_json=$(jq -n \
    --arg base_tree "$tree_sha" \
    --arg sync_path ".github/workflows/sync-copilot-instructions.yml" \
    --arg sync_sha "$sync_blob_sha" \
    --arg prop_path ".github/workflows/propagate-copilot-instructions.yml" \
    --arg prop_sha "$propagate_blob_sha" \
    '{
      base_tree: $base_tree,
      tree: [
        {path: $sync_path, mode: "100644", type: "blob", sha: $sync_sha},
        {path: $prop_path,  mode: "100644", type: "blob", sha: $prop_sha}
      ]
    }')

  # Append deletion entries for each instruction file found
  while IFS= read -r file; do
    [ -z "$file" ] && continue
    new_tree_json=$(echo "$new_tree_json" | jq \
      --arg p "$file" \
      '.tree += [{path: $p, mode: "100644", type: "blob", sha: null}]')
  done <<< "$files_to_delete"

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
      echo "    → PAT lacks 'Contents: Read and write' for this repo."
      echo "    → Update PAT repository access at https://github.com/settings/personal-access-tokens"
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
  commit_error=$(mktemp)
  _commit_resp=$(jq -n \
    --arg msg  "Bootstrap Copilot sync workflows" \
    --arg tree "$new_tree_sha" \
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
  # 9. Copy Copilot setup from Cratis/AI (second commit)
  # ----------------------------------------------------------------
  if [ -n "$ai_copilot_files" ] && [ "$ai_copilot_files" != "[]" ]; then
    ai_second_tree_json=$(jq -n \
      --arg base_tree "$new_tree_sha" \
      '{"base_tree": $base_tree, "tree": []}')

    ai_copy_failed=false
    while IFS=' ' read -r ai_path ai_sha; do
      [ -z "$ai_path" ] && continue

      # Fetch blob content from Cratis/AI (returned as base64 by the API)
      ai_blob_error=$(mktemp)
      ai_blob_content=$(gh api "repos/Cratis/AI/git/blobs/$ai_sha" \
        --jq '.content' 2>"$ai_blob_error" || true)

      if [ -z "$ai_blob_content" ]; then
        ai_blob_api_error=$(cat "$ai_blob_error" 2>/dev/null || true)
        echo "  ⚠ Could not fetch blob for $ai_path from Cratis/AI; skipping second commit"
        [ -n "$ai_blob_api_error" ] && echo "    API error: $ai_blob_api_error"
        rm -f "$ai_blob_error"
        ai_copy_failed=true
        break
      fi
      rm -f "$ai_blob_error"

      # Strip embedded newlines that the API inserts into base64 output
      clean_ai_b64=$(echo "$ai_blob_content" | tr -d '\n')

      target_blob_error=$(mktemp)
      _target_blob_resp=$(gh api -X POST "repos/Cratis/$repo/git/blobs" \
        -f "content=$clean_ai_b64" \
        -f encoding=base64 \
        2>"$target_blob_error" || true)
      target_blob_sha=$(extract_sha "$_target_blob_resp")

      if [ -z "$target_blob_sha" ]; then
        target_blob_api_error=$(cat "$target_blob_error" 2>/dev/null || true)
        echo "  ⚠ Could not create blob for $ai_path in $repo; skipping second commit"
        [ -n "$target_blob_api_error" ] && echo "    API error: $target_blob_api_error"
        rm -f "$target_blob_error"
        ai_copy_failed=true
        break
      fi
      rm -f "$target_blob_error"

      ai_second_tree_json=$(echo "$ai_second_tree_json" | jq \
        --arg p "$ai_path" \
        --arg s "$target_blob_sha" \
        '.tree += [{path: $p, mode: "100644", type: "blob", sha: $s}]')
    done <<< "$(echo "$ai_copilot_files" | jq -r '.[] | .path + " " + .sha' 2>/dev/null || true)"

    if [ "$ai_copy_failed" = "false" ]; then
      ai_second_tree_error=$(mktemp)
      _ai_tree_resp=$(echo "$ai_second_tree_json" | \
        gh api -X POST "repos/Cratis/$repo/git/trees" \
        --input - 2>"$ai_second_tree_error" || true)
      ai_second_tree_sha=$(extract_sha "$_ai_tree_resp")

      if [ -z "$ai_second_tree_sha" ]; then
        ai_second_tree_api_error=$(cat "$ai_second_tree_error" 2>/dev/null || true)
        echo "  ⚠ Could not create second tree for $repo; will push first commit only"
        [ -n "$ai_second_tree_api_error" ] && echo "    API error: $ai_second_tree_api_error"
      else
        ai_second_commit_error=$(mktemp)
        _ai_commit_resp=$(jq -n \
          --arg msg "Add initial Copilot setup from Cratis/AI" \
          --arg tree "$ai_second_tree_sha" \
          --arg parent "$new_commit_sha" \
          '{"message": $msg, "tree": $tree, "parents": [$parent]}' | \
          gh api -X POST "repos/Cratis/$repo/git/commits" \
          --input - 2>"$ai_second_commit_error" || true)
        ai_second_commit_sha=$(extract_sha "$_ai_commit_resp")

        if [ -z "$ai_second_commit_sha" ]; then
          ai_second_commit_api_error=$(cat "$ai_second_commit_error" 2>/dev/null || true)
          echo "  ⚠ Could not create second commit for $repo; will push first commit only"
          [ -n "$ai_second_commit_api_error" ] && echo "    API error: $ai_second_commit_api_error"
        else
          echo "  ✓ Added Copilot setup from Cratis/AI (second commit)"
          new_commit_sha="$ai_second_commit_sha"
        fi
        rm -f "$ai_second_commit_error"
      fi
      rm -f "$ai_second_tree_error"
    fi
  fi

  # ----------------------------------------------------------------
  # 10. Push commit directly to the default branch
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

  echo "  ✓ Pushed Bootstrap Copilot sync workflows directly to $default_branch in $repo"
done

total_failures=$(wc -l < "$failures_file" 2>/dev/null || echo "0")
rm -f "$failures_file"

echo ""
echo "Summary: $total_failures failure(s)"

if [ "$total_failures" -gt 0 ]; then
  echo "::error::$total_failures repo(s) failed. Check the log above for details."
  exit 1
fi
