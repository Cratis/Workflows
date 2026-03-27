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
#   - Up-to-date repos:     skips processing; closes any stale open PRs
#
# Expects:
#   GH_TOKEN      - PAT with repo + pull_requests write + Workflows permissions
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

prs_created_file=$(mktemp)
failures_file=$(mktemp)
pr_failures_file=$(mktemp)

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

pr_title="Add cleanup-pr-artifacts workflow"
pr_body=$'Adds the reusable cleanup-pr-artifacts wrapper workflow.\n\nWhen a pull request is closed, this workflow automatically deletes GitHub Packages\n(container images and NuGet packages) published during that PR.\n\n**Added**:\n- `.github/workflows/cleanup-pr-artifacts.yml` — triggered on `pull_request` closed events;\n  delegates to `Cratis/Workflows/.github/workflows/cleanup-pr-artifacts.yml@main`.\n\nThe actual cleanup logic lives in [Cratis/Workflows](https://github.com/Cratis/Workflows)\nso it can be maintained in one place and updated across all repositories at once.'

branch="add-cleanup-pr-artifacts-workflow"

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
    echo "  • Permissions → Pull requests: Read and write"
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
    # Close any stale open PR for this branch — the repo is already up-to-date.
    _stale_pr_resp=$(gh api "repos/Cratis/$repo/pulls?state=open&head=Cratis:$branch" 2>/dev/null || true)
    _stale_pr_num=$(echo "$_stale_pr_resp" | jq -r '.[0].number // empty' 2>/dev/null || true)
    if [ -n "$_stale_pr_num" ] && [ "$_stale_pr_num" != "null" ]; then
      _close_pr_error=$(mktemp)
      if gh api -X PATCH "repos/Cratis/$repo/pulls/$_stale_pr_num" \
          -f state=closed 2>"$_close_pr_error"; then
        echo "  ✓ Closed stale PR #$_stale_pr_num for $repo (no changes needed)"
      else
        _close_pr_api_error=$(cat "$_close_pr_error" 2>/dev/null || true)
        echo "  ⚠ Could not close stale PR #$_stale_pr_num for $repo"
        [ -n "$_close_pr_api_error" ] && echo "    API error: $_close_pr_api_error"
      fi
      rm -f "$_close_pr_error"
    fi
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
  # 9. Create or force-update the branch (GraphQL — synchronous)
  # ----------------------------------------------------------------
  branch_error=$(mktemp)

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
    rm -f "$branch_error"
    echo "$repo" >> "$failures_file"
    continue
  fi
  rm -f "$branch_error"

  echo "  ✓ Branch $branch ready for $repo"

  # ----------------------------------------------------------------
  # 10. Create PR (skip if one already exists for this branch)
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

  if [ -z "$existing_pr" ] || [ "$existing_pr" = "null" ]; then
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
        --arg title "$pr_title" \
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
        --arg title "$pr_title" \
        --arg body  "$pr_body" \
        --arg head  "$branch" \
        --arg base  "$default_branch" \
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
  echo "::warning::$total_pr_failures repo(s) could not have PRs auto-created. Branches were set up successfully. Ensure PAT_WORKFLOWS has pull_requests:write permission."
fi

if [ "$total_failures" -gt 0 ]; then
  echo "::error::$total_failures repo(s) failed to set up branches. Check the log above for details."
  exit 1
fi
