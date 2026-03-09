#!/usr/bin/env bash
# Main logic for the Update Synced Workflows workflow.
# Called by .github/workflows/update-synced-workflows.yml after checkout.
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
skipped_file=$(mktemp)
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
#       secrets: inherit
propagate_b64="bmFtZTogUHJvcGFnYXRlIENvcGlsb3QgSW5zdHJ1Y3Rpb25zCgpvbjoKICBwdXNoOgogICAgYnJhbmNoZXM6IFsibWFpbiJdCiAgICBwYXRoczoKICAgICAgLSAiLmdpdGh1Yi9jb3BpbG90LWluc3RydWN0aW9ucy5tZCIKICAgICAgLSAiLmdpdGh1Yi9pbnN0cnVjdGlvbnMvKioiCiAgICAgIC0gIi5naXRodWIvYWdlbnRzLyoqIgogICAgICAtICIuZ2l0aHViL3NraWxscy8qKiIKICAgICAgLSAiLmdpdGh1Yi9wcm9tcHRzLyoqIgogIHdvcmtmbG93X2Rpc3BhdGNoOgoKam9iczoKICBwcm9wYWdhdGU6CiAgICB1c2VzOiBDcmF0aXMvV29ya2Zsb3dzLy5naXRodWIvd29ya2Zsb3dzL3Byb3BhZ2F0ZS1jb3BpbG90LWluc3RydWN0aW9ucy55bWxAbWFpbgogICAgc2VjcmV0czogaW5oZXJpdAo="

pr_body=$'Updates the centralized Copilot sync wrapper workflows in this repository to the latest version from [Cratis/Workflows](https://github.com/Cratis/Workflows).\n\n### Changes\n\n**Updated**:\n- `.github/workflows/sync-copilot-instructions.yml`\n- `.github/workflows/propagate-copilot-instructions.yml`\n\nThese files are thin wrappers that delegate to the reusable workflows in [Cratis/Workflows](https://github.com/Cratis/Workflows). Merging this PR ensures the repository uses the latest wrapper definitions.'

branch="update-synced-workflows"

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
  # 3. Get the full recursive tree to check existing workflow SHAs
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
  # 5. Check if workflow files already match (skip if up-to-date)
  # ----------------------------------------------------------------
  existing_sync=$(echo "$subtree" | jq -r \
    '.tree[] | select(.path == ".github/workflows/sync-copilot-instructions.yml") | .sha' \
    2>/dev/null || true)
  existing_propagate=$(echo "$subtree" | jq -r \
    '.tree[] | select(.path == ".github/workflows/propagate-copilot-instructions.yml") | .sha' \
    2>/dev/null || true)

  if [ "$existing_sync" = "$sync_blob_sha" ] && \
     [ "$existing_propagate" = "$propagate_blob_sha" ]; then
    echo "  ℹ Workflow files already up-to-date in $repo, skipping"
    echo "$repo" >> "$skipped_file"
    continue
  fi

  # ----------------------------------------------------------------
  # 6. Check if either workflow file is missing (repo not bootstrapped)
  # ----------------------------------------------------------------
  if [ -z "$existing_sync" ] && [ -z "$existing_propagate" ]; then
    echo "  ℹ Workflow files not found in $repo — repository may not be bootstrapped yet, skipping"
    echo "$repo" >> "$skipped_file"
    continue
  fi

  # ----------------------------------------------------------------
  # 7. Build the new tree JSON (only the two workflow files)
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

  # ----------------------------------------------------------------
  # 8. Create the new tree object
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
    else
      [ -n "$tree_api_error" ] && echo "    API error: $tree_api_error"
    fi
    rm -f "$tree_error"
    echo "$repo" >> "$failures_file"
    continue
  fi
  rm -f "$tree_error"

  # ----------------------------------------------------------------
  # 9. Create the commit
  # ----------------------------------------------------------------
  commit_error=$(mktemp)
  _commit_resp=$(jq -n \
    --arg msg  "Update Copilot sync wrapper workflows" \
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
  # 10. Create or force-update the feature branch (via GraphQL)
  # ----------------------------------------------------------------
  branch_error=$(mktemp)
  branch_ok=""

  existing_ref_result=$(gh api graphql \
    -f query='query($owner:String!,$name:String!,$ref:String!){repository(owner:$owner,name:$name){ref(qualifiedName:$ref){id target{oid}}}}' \
    -f owner="Cratis" \
    -f name="$repo" \
    -f ref="refs/heads/$branch" \
    2>/dev/null || true)
  existing_ref_id=$(echo "$existing_ref_result" | jq -r '.data.repository.ref.id // empty' 2>/dev/null || true)

  if [ -n "$existing_ref_id" ] && [ "$existing_ref_id" != "null" ]; then
    branch_result=$(gh api graphql \
      -f query='mutation($refId:ID!,$oid:GitObjectID!){updateRef(input:{refId:$refId,oid:$oid,force:true}){ref{name target{oid}}}}' \
      -f refId="$existing_ref_id" \
      -f oid="$new_commit_sha" \
      2>"$branch_error" || true)
    branch_ok=$(echo "$branch_result" | jq -r '.data.updateRef.ref.name // empty' 2>/dev/null || true)
  else
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

  if [ -n "$existing_pr" ] && [ "$existing_pr" != "null" ]; then
    echo "  ℹ PR #$existing_pr already exists for $repo (branch force-updated)"
    echo "$repo" >> "$prs_created_file"
    continue
  fi

  pr_created=false

  # Strategy 1: GraphQL createPullRequest mutation
  if [ -n "$repo_node_id" ]; then
    pr_error=$(mktemp)
    gql_input_file=$(mktemp)
    jq -n \
      --arg query 'mutation($repoId:ID!,$base:String!,$head:String!,$title:String!,$body:String!){createPullRequest(input:{repositoryId:$repoId,baseRefName:$base,headRefName:$head,title:$title,body:$body}){pullRequest{url}}}' \
      --arg repoId "$repo_node_id" \
      --arg base "$default_branch" \
      --arg head "$branch" \
      --arg title "Update Copilot sync wrapper workflows" \
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

      if echo "$gql_errors$gql_data_errors" | grep -qi "already exists"; then
        echo "  ℹ PR already exists for $repo (detected via GraphQL error)"
        echo "$repo" >> "$prs_created_file"
        pr_created=true
      fi
    fi
    rm -f "$pr_error"
  fi

  # Strategy 2: REST API fallback
  if [ "$pr_created" = "false" ]; then
    echo "  ℹ Trying REST API fallback for $repo..."
    pr_error=$(mktemp)
    rest_input_file=$(mktemp)

    jq -n \
      --arg title "Update Copilot sync wrapper workflows" \
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

      if echo "$rest_errors$rest_msg" | grep -qi "already exists"; then
        echo "  ℹ PR already exists for $repo (detected via REST 422)"
        echo "$repo" >> "$prs_created_file"
        pr_created=true
      else
        echo "$repo" >> "$pr_failures_file"
      fi
    fi
    rm -f "$pr_error"
  fi
done

total_prs=$(wc -l < "$prs_created_file" 2>/dev/null || echo "0")
total_skipped=$(wc -l < "$skipped_file" 2>/dev/null || echo "0")
total_failures=$(wc -l < "$failures_file" 2>/dev/null || echo "0")
total_pr_failures=$(wc -l < "$pr_failures_file" 2>/dev/null || echo "0")
rm -f "$prs_created_file" "$skipped_file" "$failures_file" "$pr_failures_file"

echo ""
echo "Summary: $total_prs PR(s) created/updated, $total_skipped skipped (up-to-date or not bootstrapped), $total_failures failed"

if [ "$total_failures" -gt 0 ] || [ "$total_pr_failures" -gt 0 ]; then
  echo "::error::Failed to process $((total_failures + total_pr_failures)) repo(s). Check the log above."
  exit 1
fi
