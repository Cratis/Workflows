#!/usr/bin/env bash
# Main logic for the Bootstrap Copilot Sync workflow.
# Called by .github/workflows/bootstrap-copilot-sync.yml after checkout.
# Expects:
#   GH_TOKEN          - PAT with repo + pull_requests write permissions
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

prs_created_file=$(mktemp)
failures_file=$(mktemp)
pr_failures_file=$(mktemp)

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
#         - ".github/copilot-instructions.md"
#         - ".github/instructions/**"
#         - ".github/agents/**"
#         - ".github/skills/**"
#         - ".github/prompts/**"
#     workflow_dispatch:
#   jobs:
#     propagate:
#       uses: Cratis/Workflows/.github/workflows/propagate-copilot-instructions.yml@main
#       with:
#         event_name: ${{ github.event_name }}
#       secrets: inherit
propagate_b64="bmFtZTogUHJvcGFnYXRlIENvcGlsb3QgSW5zdHJ1Y3Rpb25zCgpvbjoKICBwdXNoOgogICAgYnJhbmNoZXM6IFsibWFpbiJdCiAgICBwYXRoczoKICAgICAgLSAiLmdpdGh1Yi9jb3BpbG90LWluc3RydWN0aW9ucy5tZCIKICAgICAgLSAiLmdpdGh1Yi9pbnN0cnVjdGlvbnMvKioiCiAgICAgIC0gIi5naXRodWIvYWdlbnRzLyoqIgogICAgICAtICIuZ2l0aHViL3NraWxscy8qKiIKICAgICAgLSAiLmdpdGh1Yi9wcm9tcHRzLyoqIgogIHdvcmtmbG93X2Rpc3BhdGNoOgoKam9iczoKICBwcm9wYWdhdGU6CiAgICB1c2VzOiBDcmF0aXMvV29ya2Zsb3dzLy5naXRodWIvd29ya2Zsb3dzL3Byb3BhZ2F0ZS1jb3BpbG90LWluc3RydWN0aW9ucy55bWxAbWFpbgogICAgd2l0aDoKICAgICAgZXZlbnRfbmFtZTogJHt7IGdpdGh1Yi5ldmVudF9uYW1lIH19CiAgICBzZWNyZXRzOiBpbmhlcml0Cg=="

pr_body=$'Bootstraps centralized Copilot instruction management for this repository.\n\n### Changes\n\n**Removed** (if present):\n- `.github/copilot-instructions.md`\n- `.github/instructions/` folder\n- `.github/agents/` folder\n- `.github/skills/` folder\n- `.github/prompts/` folder\n\n**Added**:\n- `.github/workflows/sync-copilot-instructions.yml` \u2014 triggered via `workflow_dispatch` to pull Copilot instructions from a source repository and open a PR with the changes.\n- `.github/workflows/propagate-copilot-instructions.yml` \u2014 triggered on push to `main` when Copilot instruction files change, propagating updates to all Cratis repositories.\n\n**Copied from [Cratis/AI](https://github.com/Cratis/AI)**:\n- `.github/copilot-instructions.md`\n- `.github/instructions/` folder (if present)\n- `.github/agents/` folder (if present)\n- `.github/skills/` folder (if present)\n- `.github/prompts/` folder (if present)\n\nThe actual logic lives in [Cratis/Workflows](https://github.com/Cratis/Workflows) so it can be maintained in one place. Copilot instructions will be managed centrally and synced to this repository via the workflows above.'

branch="add-copilot-sync-workflows"

# Fetch the Copilot setup tree from Cratis/AI once; reused for every repo.
ai_copilot_files=""
ai_tree_error=$(mktemp)
ai_tree_raw=$(gh api "repos/Cratis/AI/git/trees/main?recursive=1" 2>"$ai_tree_error" || true)
if [ -n "$ai_tree_raw" ]; then
  ai_copilot_files=$(echo "$ai_tree_raw" | jq -c \
    '[.tree[] | select(.type == "blob") |
     select(.path | test("^\\.github/(copilot-instructions\\.md$|instructions/|agents/|skills/|prompts/)")) |
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
    echo "  • Permissions → Pull requests: Read and write"
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
  # 1. Get default branch, HEAD SHA, and repository node ID
  # ----------------------------------------------------------------
  repo_info_error=$(mktemp)
  repo_info_json=$(gh api "repos/Cratis/$repo" \
    --jq '{default_branch: .default_branch, node_id: .node_id}' 2>"$repo_info_error" || true)
  default_branch=$(echo "$repo_info_json" | jq -r '.default_branch // empty' 2>/dev/null || true)
  repo_node_id=$(echo "$repo_info_json" | jq -r '.node_id // empty' 2>/dev/null || true)
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
  # instructions/, agents/, skills/, and prompts/ sub-directories.
  files_to_delete=$(echo "$subtree" | jq -r \
    '.tree[] | select(.type == "blob") |
     select(.path | test("^\\.github/(copilot-instructions\\.md$|instructions/|agents/|skills/|prompts/)")) |
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

  if [ "$existing_sync" = "$sync_blob_sha" ] && \
     [ "$existing_propagate" = "$propagate_blob_sha" ] && \
     [ -z "$files_to_delete" ] && \
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
        echo "  ⚠ Could not create second tree for $repo; branch will use first commit only"
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
          echo "  ⚠ Could not create second commit for $repo; branch will use first commit only"
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
  # 10. Create or force-update the feature branch (via GraphQL)
  #
  # The REST Git Data API (POST /git/refs) creates low-level refs that
  # are NOT registered in GitHub's branch index.  The Pulls API and
  # GraphQL createPullRequest require the branch to be in the index,
  # which is why every previous attempt got "Head ref must be a branch".
  #
  # GraphQL createRef / updateRef go through the higher-level branch
  # service and properly register the branch.
  # ----------------------------------------------------------------
  branch_error=$(mktemp)
  branch_ok=""

  # Check if the branch already exists via GraphQL
  existing_ref_result=$(gh api graphql \
    -f query='query($owner:String!,$name:String!,$ref:String!){repository(owner:$owner,name:$name){ref(qualifiedName:$ref){id target{oid}}}}' \
    -f owner="Cratis" \
    -f name="$repo" \
    -f ref="refs/heads/$branch" \
    2>/dev/null || true)
  existing_ref_id=$(echo "$existing_ref_result" | jq -r '.data.repository.ref.id // empty' 2>/dev/null || true)

  if [ -n "$existing_ref_id" ] && [ "$existing_ref_id" != "null" ]; then
    # Branch exists — force-update it
    branch_result=$(gh api graphql \
      -f query='mutation($refId:ID!,$oid:GitObjectID!){updateRef(input:{refId:$refId,oid:$oid,force:true}){ref{name target{oid}}}}' \
      -f refId="$existing_ref_id" \
      -f oid="$new_commit_sha" \
      2>"$branch_error" || true)
    branch_ok=$(echo "$branch_result" | jq -r '.data.updateRef.ref.name // empty' 2>/dev/null || true)
  else
    # Branch doesn't exist — create it
    if [ -z "$repo_node_id" ]; then
      echo "  ⚠ No repository node ID for $repo; cannot create branch via GraphQL"
      echo "$repo" >> "$failures_file"
      rm -f "$branch_error"
      continue
    fi
    branch_result=$(gh api graphql \
      -f query='mutation($repoId:ID!,$name:String!,$oid:GitObjectID!){createRef(input:{repositoryId:$repoId,name:$name,oid:$oid}){ref{name target{oid}}}}' \
      -f repoId="$repo_node_id" \
      -f name="refs/heads/$branch" \
      -f oid="$new_commit_sha" \
      2>"$branch_error" || true)
    branch_ok=$(echo "$branch_result" | jq -r '.data.createRef.ref.name // empty' 2>/dev/null || true)
  fi

  if [ -z "$branch_ok" ] || [ "$branch_ok" = "null" ]; then
    branch_api_error=$(cat "$branch_error" 2>/dev/null || true)
    branch_gql_errors=$(echo "$branch_result" | jq -r '(.errors // []) | map(.message) | join("; ")' 2>/dev/null || true)
    echo "  ⚠ Could not create/update branch for $repo (GraphQL)"
    [ -n "$branch_api_error" ] && echo "    stderr: $branch_api_error"
    [ -n "$branch_gql_errors" ] && echo "    GraphQL errors: $branch_gql_errors"
    echo "    Full response: $(echo "$branch_result" | head -c 500)"
    rm -f "$branch_error"
    echo "$repo" >> "$failures_file"
    continue
  fi
  rm -f "$branch_error"

  echo "  ✓ Branch $branch ready for $repo (GraphQL)"

  # ----------------------------------------------------------------
  # 11. Create PR (skip if one already exists for this branch)
  # ----------------------------------------------------------------
  # No polling needed — GraphQL createRef/updateRef registers the
  # branch in the branch index synchronously.

  # Check for an existing open PR for this branch.
  existing_pr=""
  list_pr_error=$(mktemp)
  if api_result=$(gh api "repos/Cratis/$repo/pulls?state=open&head=Cratis:$branch" 2>"$list_pr_error"); then
    existing_pr=$(echo "$api_result" | jq -r '.[0].number // empty' 2>/dev/null || true)
  else
    list_pr_api_error=$(cat "$list_pr_error" 2>/dev/null || true)
    echo "  ⚠ Could not list PRs for $repo"
    [ -n "$list_pr_api_error" ] && echo "    API error: $list_pr_api_error"
  fi
  rm -f "$list_pr_error"

  if [ -z "$existing_pr" ] || [ "$existing_pr" = "null" ]; then
    pr_created=false

    # ------------------------------------------------------------------
    # Strategy 1: GraphQL createPullRequest mutation
    # The raw GraphQL mutation resolves headRefName within the
    # repository identified by repositoryId — no local git checkout
    # needed and no cross-repo branch resolution issues.
    # ------------------------------------------------------------------
    if [ -n "$repo_node_id" ]; then
      pr_error=$(mktemp)
      # Write the body to a temp file so we can pass it cleanly via --input
      # without any shell escaping issues with backticks/newlines/markdown.
      gql_input_file=$(mktemp)
      jq -n \
        --arg query 'mutation($repoId:ID!,$base:String!,$head:String!,$title:String!,$body:String!){createPullRequest(input:{repositoryId:$repoId,baseRefName:$base,headRefName:$head,title:$title,body:$body}){pullRequest{url}}}' \
        --arg repoId "$repo_node_id" \
        --arg base "$default_branch" \
        --arg head "$branch" \
        --arg title "Bootstrap Copilot sync workflows" \
        --arg body "$pr_body" \
        '{query:$query,variables:{repoId:$repoId,base:$base,head:$head,title:$title,body:$body}}' \
        > "$gql_input_file"

      pr_response=$(gh api graphql --input "$gql_input_file" 2>"$pr_error" || true)
      rm -f "$gql_input_file"

      pr_url=$(echo "$pr_response" | jq -r '.data.createPullRequest.pullRequest.url // empty' 2>/dev/null || true)
      if [ -n "$pr_url" ] && [ "$pr_url" != "null" ]; then
        echo "  ✓ Created PR for $repo (GraphQL): $pr_url"
        echo "$repo" >> "$prs_created_file"
        pr_created=true
      else
        gql_err=$(cat "$pr_error" 2>/dev/null || true)
        gql_errors=$(echo "$pr_response" | jq -r '(.errors // []) | map(.message) | join("; ")' 2>/dev/null || true)
        gql_data_errors=$(echo "$pr_response" | jq -r '(.data.createPullRequest.errors // []) | map(.message // .code // "unknown") | join("; ")' 2>/dev/null || true)
        echo "  ℹ GraphQL PR creation failed for $repo"
        [ -n "$gql_err" ] && echo "    stderr: $gql_err"
        [ -n "$gql_errors" ] && echo "    GraphQL errors: $gql_errors"
        [ -n "$gql_data_errors" ] && echo "    Mutation errors: $gql_data_errors"
        echo "    Full response: $(echo "$pr_response" | head -c 800)"

        # Check for "already exists" in GraphQL error
        if echo "$gql_errors$gql_data_errors" | grep -qi "already exists"; then
          echo "  ℹ PR already exists for $repo (detected via GraphQL error)"
          echo "$repo" >> "$prs_created_file"
          pr_created=true
        fi
      fi
      rm -f "$pr_error"
    fi

    # ------------------------------------------------------------------
    # Strategy 2: REST API fallback with JSON body via --input
    # Pass the full payload as JSON file to avoid any shell escaping
    # issues with the PR body (contains markdown, backticks, URLs).
    # ------------------------------------------------------------------
    if [ "$pr_created" = "false" ]; then
      echo "  ℹ Trying REST API fallback for $repo..."
      pr_error=$(mktemp)
      rest_input_file=$(mktemp)

      jq -n \
        --arg title "Bootstrap Copilot sync workflows" \
        --arg body "$pr_body" \
        --arg head "$branch" \
        --arg base "$default_branch" \
        '{title:$title, body:$body, head:$head, base:$base}' \
        > "$rest_input_file"

      pr_response=$(gh api -X POST "repos/Cratis/$repo/pulls" \
        --input "$rest_input_file" \
        2>"$pr_error" || true)
      rm -f "$rest_input_file"

      pr_url=$(echo "$pr_response" | jq -r '.html_url // empty' 2>/dev/null || true)

      if [ -n "$pr_url" ] && [ "$pr_url" != "null" ]; then
        echo "  ✓ Created PR for $repo (REST): $pr_url"
        echo "$repo" >> "$prs_created_file"
        pr_created=true
      else
        rest_err=$(cat "$pr_error" 2>/dev/null || true)
        rest_msg=$(echo "$pr_response" | jq -r '.message // empty' 2>/dev/null || true)
        rest_errors=$(echo "$pr_response" | jq -r '(.errors // []) | map(.message // .code // "unknown") | join("; ")' 2>/dev/null || true)
        echo "  ⚠ REST PR creation also failed for $repo"
        [ -n "$rest_err" ] && echo "    stderr: $rest_err"
        [ -n "$rest_msg" ] && echo "    GitHub message: $rest_msg"
        [ -n "$rest_errors" ] && echo "    Validation errors: $rest_errors"
        echo "    Full response: $(echo "$pr_response" | head -c 800)"

        # Handle known 422 cases gracefully
        if echo "$rest_errors$rest_msg" | grep -qi "already exists"; then
          echo "  ℹ PR already exists for $repo (detected via REST 422)"
          echo "$repo" >> "$prs_created_file"
          pr_created=true
        elif echo "$rest_errors" | grep -qi "no commits between"; then
          echo "  ℹ No diff between $branch and $default_branch for $repo — skipping"
        fi
      fi
      rm -f "$pr_error"
    fi

    if [ "$pr_created" = "false" ]; then
      echo "$repo" >> "$pr_failures_file"
    fi
  else
    echo "  ℹ PR already exists for $repo (#$existing_pr)"
    echo "$repo" >> "$prs_created_file"
  fi
done

total_prs=$(wc -l < "$prs_created_file" 2>/dev/null || echo "0")
total_failures=$(wc -l < "$failures_file" 2>/dev/null || echo "0")
total_pr_failures=$(wc -l < "$pr_failures_file" 2>/dev/null || echo "0")
rm -f "$prs_created_file" "$failures_file" "$pr_failures_file"

echo ""
echo "Summary: $total_prs repo(s) with PR created or already open, $total_pr_failures repo(s) where PR could not be created, $total_failures branch setup failure(s)"

if [ "$total_pr_failures" -gt 0 ]; then
  echo "::warning::$total_pr_failures repo(s) could not have PRs auto-created. Branches were set up successfully. Ensure PAT_WORKFLOWS has pull_requests:write permission to enable automatic PR creation."
fi

if [ "$total_failures" -gt 0 ]; then
  echo "::error::$total_failures repo(s) failed to set up branches. Check the log above for details."
  exit 1
fi
