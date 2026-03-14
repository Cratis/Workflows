#!/usr/bin/env bash
# Propagates Copilot instruction files from the source repository to a single
# target repository in the Cratis organization, opening a PR with the changes.
# Called by .github/workflows/propagate-copilot-instructions.yml for each
# matrix job (one per target repository).
#
# Expects:
#   GH_TOKEN      - PAT with Contents (r/w) + Pull requests (r/w) + Workflows (r/w)
#   SOURCE_REPO   - source repository in owner/repo format (e.g. Cratis/AI)
#   TARGET_REPO   - target repository name (e.g. Chronicle)

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

source_repo="${SOURCE_REPO:?SOURCE_REPO must be set}"
repo="${TARGET_REPO:?TARGET_REPO must be set}"

branch="copilot-sync/update-instructions"

# ----------------------------------------------------------------
# Fetch Copilot files from the source repository
# ----------------------------------------------------------------
echo "Fetching Copilot instruction files from ${source_repo}..."
source_tree_raw=$(gh api "repos/${source_repo}/git/trees/HEAD?recursive=1" 2>/dev/null || true)

if [ -z "$source_tree_raw" ]; then
  echo "::error::Could not fetch tree from ${source_repo}"
  exit 1
fi

copilot_files=$(echo "$source_tree_raw" | jq -c \
  '[.tree[] | select(.type == "blob") |
   select(.path | test("^\\.github/(copilot-instructions\\.md$|instructions/|agents/|skills/|prompts/)")) |
   {path: .path, sha: .sha}]' 2>/dev/null || true)

if [ -z "$copilot_files" ] || [ "$copilot_files" = "[]" ]; then
  echo "No Copilot instruction files found in ${source_repo} — nothing to propagate."
  exit 0
fi
echo "✓ Found $(echo "$copilot_files" | jq 'length') Copilot file(s) in ${source_repo}"

# ----------------------------------------------------------------
# Filter out files matching .copilot-sync-ignore patterns
# ----------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=copilot-sync-ignore-filter.sh
source "${SCRIPT_DIR}/copilot-sync-ignore-filter.sh"

if ! _apply_copilot_sync_ignore; then
  echo "All Copilot files excluded by .copilot-sync-ignore — nothing to propagate."
  exit 0
fi

echo "Processing Cratis/${repo}..."

# ----------------------------------------------------------------
# 1. Get default branch, HEAD SHA, and repository node ID
# ----------------------------------------------------------------
repo_info_error=$(mktemp)
repo_info_json=$(gh api "repos/Cratis/${repo}" \
  --jq '{default_branch: .default_branch, node_id: .node_id}' \
  2>"$repo_info_error" || true)
default_branch=$(echo "$repo_info_json" | jq -r '.default_branch // empty' 2>/dev/null || true)
repo_node_id=$(echo "$repo_info_json" | jq -r '.node_id // empty' 2>/dev/null || true)
if [ -z "$default_branch" ]; then
  repo_info_api_error=$(cat "$repo_info_error" 2>/dev/null || true)
  echo "::error::Could not get default branch for ${repo}"
  [ -n "$repo_info_api_error" ] && echo "  API error: $repo_info_api_error"
  rm -f "$repo_info_error"
  exit 1
fi
rm -f "$repo_info_error"

head_sha_error=$(mktemp)
_head_sha_resp=$(gh api "repos/Cratis/${repo}/git/ref/heads/${default_branch}" \
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
_tree_sha_resp=$(gh api "repos/Cratis/${repo}/git/commits/${head_sha}" \
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
subtree=$(gh api "repos/Cratis/${repo}/git/trees/${tree_sha}?recursive=1" \
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
# 3. Check for an existing sync branch and open PR early.
#    This determines the correct comparison baseline for the
#    "up to date" check below: when a PR is already open we compare
#    the source against the sync branch tree so that new source
#    changes are always committed to the existing PR rather than
#    being silently skipped because the default branch looks current.
# ----------------------------------------------------------------
existing_ref_result=$(gh api graphql \
  -f query='query($owner:String!,$name:String!,$ref:String!){repository(owner:$owner,name:$name){ref(qualifiedName:$ref){id target{oid}}}}' \
  -f owner="Cratis" \
  -f name="$repo" \
  -f ref="refs/heads/${branch}" \
  2>/dev/null || true)
existing_ref_id=$(echo "$existing_ref_result" | jq -r '.data.repository.ref.id // empty' 2>/dev/null || true)
existing_branch_sha=$(echo "$existing_ref_result" | jq -r '.data.repository.ref.target.oid // empty' 2>/dev/null || true)

existing_pr=""
list_pr_error=$(mktemp)
if api_result=$(gh api "repos/Cratis/${repo}/pulls?state=open&head=Cratis:${branch}" 2>"$list_pr_error"); then
  existing_pr=$(echo "$api_result" | jq -r '.[0].number // empty' 2>/dev/null || true)
else
  list_pr_api_error=$(cat "$list_pr_error" 2>/dev/null || true)
  echo "⚠ Could not list PRs for ${repo}"
  [ -n "$list_pr_api_error" ] && echo "  API error: $list_pr_api_error"
fi
rm -f "$list_pr_error"

# When a PR is open, compare the source against the sync branch tree
# so we only skip if the PR branch already carries the latest changes.
# When there is no PR, fall back to comparing against the default branch.
comparison_subtree="$subtree"
if [ -n "$existing_pr" ] && [ "$existing_pr" != "null" ] && \
   [ -n "$existing_branch_sha" ] && [ "$existing_branch_sha" != "null" ]; then
  echo "ℹ Open PR #${existing_pr} found for ${repo} — comparing source against sync branch"
  sync_tree_sha_error=$(mktemp)
  _sync_tree_sha_resp=$(gh api "repos/Cratis/${repo}/git/commits/${existing_branch_sha}" \
    2>"$sync_tree_sha_error" || true)
  sync_tree_sha=$(extract_sha "$_sync_tree_sha_resp" '.tree.sha')
  rm -f "$sync_tree_sha_error"
  if [ -n "$sync_tree_sha" ]; then
    sync_subtree_error=$(mktemp)
    sync_subtree=$(gh api "repos/Cratis/${repo}/git/trees/${sync_tree_sha}?recursive=1" \
      2>"$sync_subtree_error" || true)
    rm -f "$sync_subtree_error"
    [ -n "$sync_subtree" ] && comparison_subtree="$sync_subtree"
  fi
fi

# ----------------------------------------------------------------
# 4. Check whether all copilot files are already up to date
#    (git blob SHAs are content-addressed across repositories)
# ----------------------------------------------------------------
files_up_to_date=true
while IFS=' ' read -r chk_path chk_sha; do
  [ -z "$chk_path" ] && continue
  existing_sha=$(echo "$comparison_subtree" | jq -r \
    --arg p "$chk_path" \
    '.tree[] | select(.path == $p) | .sha // empty' 2>/dev/null || true)
  if [ "$existing_sha" != "$chk_sha" ]; then
    files_up_to_date=false
    break
  fi
done <<< "$(echo "$copilot_files" | jq -r '.[] | .path + " " + .sha' 2>/dev/null || true)"

if [ "$files_up_to_date" = "true" ]; then
  if [ -n "$existing_pr" ] && [ "$existing_pr" != "null" ]; then
    echo "ℹ PR #${existing_pr} for ${repo} is already up to date with the source"
  else
    echo "ℹ No changes needed for ${repo} (files already up to date)"
  fi
  exit 0
fi

# ----------------------------------------------------------------
# 5. Create blobs in the target repository for each source file
# ----------------------------------------------------------------
new_tree_json=$(jq -n --arg base_tree "$tree_sha" \
  '{"base_tree": $base_tree, "tree": []}')

while IFS=' ' read -r src_path src_sha; do
  [ -z "$src_path" ] && continue

  # Fetch blob content from source repo (returned as base64 by API).
  # NOTE: zero-byte files return {"content":"","encoding":"base64"} — the
  # content field is legitimately empty.  We must check whether the API call
  # itself succeeded (non-empty JSON response), not whether content is empty.
  blob_error=$(mktemp)
  blob_resp=$(gh api "repos/${source_repo}/git/blobs/${src_sha}" \
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

  target_blob_error=$(mktemp)
  _target_blob_resp=$(gh api -X POST "repos/Cratis/${repo}/git/blobs" \
    -f "content=${clean_b64}" \
    -f encoding=base64 \
    2>"$target_blob_error" || true)
  target_blob_sha=$(extract_sha "$_target_blob_resp")
  rm -f "$target_blob_error"

  if [ -z "$target_blob_sha" ]; then
    echo "::error::Could not create blob for ${src_path} in ${repo}"
    exit 1
  fi

  new_tree_json=$(echo "$new_tree_json" | jq \
    --arg p "$src_path" \
    --arg s "$target_blob_sha" \
    '.tree += [{path: $p, mode: "100644", type: "blob", sha: $s}]')
done <<< "$(echo "$copilot_files" | jq -r '.[] | .path + " " + .sha' 2>/dev/null || true)"

# ----------------------------------------------------------------
# 6. Create new tree and commit
# ----------------------------------------------------------------
new_tree_error=$(mktemp)
_new_tree_resp=$(echo "$new_tree_json" | \
  gh api -X POST "repos/Cratis/${repo}/git/trees" \
  --input - 2>"$new_tree_error" || true)
new_tree_sha=$(extract_sha "$_new_tree_resp")

if [ -z "$new_tree_sha" ]; then
  new_tree_api_error=$(cat "$new_tree_error" 2>/dev/null || true)
  echo "::error::Could not create tree for ${repo}"
  [ -n "$new_tree_api_error" ] && echo "  API error: $new_tree_api_error"
  rm -f "$new_tree_error"
  exit 1
fi
rm -f "$new_tree_error"

commit_error=$(mktemp)
_commit_resp=$(jq -n \
  --arg msg  "Sync Copilot instructions from ${source_repo}" \
  --arg tree "$new_tree_sha" \
  --arg parent "$head_sha" \
  '{"message": $msg, "tree": $tree, "parents": [$parent]}' | \
  gh api -X POST "repos/Cratis/${repo}/git/commits" \
  --input - 2>"$commit_error" || true)
new_commit_sha=$(extract_sha "$_commit_resp")

if [ -z "$new_commit_sha" ]; then
  commit_api_error=$(cat "$commit_error" 2>/dev/null || true)
  echo "::error::Could not create commit for ${repo}"
  [ -n "$commit_api_error" ] && echo "  API error: $commit_api_error"
  rm -f "$commit_error"
  exit 1
fi
rm -f "$commit_error"

# ----------------------------------------------------------------
# 7. Create or force-update the feature branch via GraphQL
#
# GraphQL createRef/updateRef register the branch in GitHub's branch
# index, which is required for the Pulls API and createPullRequest
# mutation.  REST low-level ref writes do NOT register in the index.
#
# existing_ref_id was resolved earlier (before the up-to-date check)
# so we reuse it here instead of issuing a second query.
# ----------------------------------------------------------------
branch_error=$(mktemp)
branch_ok=""

if [ -n "$existing_ref_id" ] && [ "$existing_ref_id" != "null" ]; then
  branch_result=$(gh api graphql \
    -f query='mutation($refId:ID!,$oid:GitObjectID!){updateRef(input:{refId:$refId,oid:$oid,force:true}){ref{name target{oid}}}}' \
    -f refId="$existing_ref_id" \
    -f oid="$new_commit_sha" \
    2>"$branch_error" || true)
  branch_ok=$(echo "$branch_result" | jq -r '.data.updateRef.ref.name // empty' 2>/dev/null || true)
else
  if [ -z "$repo_node_id" ]; then
    echo "::error::No repository node ID for ${repo}; cannot create branch via GraphQL"
    rm -f "$branch_error"
    exit 1
  fi
  branch_result=$(gh api graphql \
    -f query='mutation($repoId:ID!,$name:String!,$oid:GitObjectID!){createRef(input:{repositoryId:$repoId,name:$name,oid:$oid}){ref{name target{oid}}}}' \
    -f repoId="$repo_node_id" \
    -f name="refs/heads/${branch}" \
    -f oid="$new_commit_sha" \
    2>"$branch_error" || true)
  branch_ok=$(echo "$branch_result" | jq -r '.data.createRef.ref.name // empty' 2>/dev/null || true)
fi

if [ -z "$branch_ok" ] || [ "$branch_ok" = "null" ]; then
  branch_api_error=$(cat "$branch_error" 2>/dev/null || true)
  branch_gql_errors=$(echo "$branch_result" | jq -r '(.errors // []) | map(.message) | join("; ")' 2>/dev/null || true)
  echo "::error::Could not create/update branch for ${repo} (GraphQL)"
  [ -n "$branch_api_error" ] && echo "  stderr: $branch_api_error"
  [ -n "$branch_gql_errors" ] && echo "  GraphQL errors: $branch_gql_errors"
  echo "  Full response: $(echo "$branch_result" | head -c 500)"
  rm -f "$branch_error"
  exit 1
fi
rm -f "$branch_error"
echo "✓ Branch ${branch} ready for ${repo} (GraphQL)"

# ----------------------------------------------------------------
# 8. Create PR, or confirm the existing PR was updated
# ----------------------------------------------------------------

# If a PR was already open we have already force-pushed the sync
# branch above, so the PR now reflects the latest changes.
if [ -n "$existing_pr" ] && [ "$existing_pr" != "null" ]; then
  echo "✓ Updated existing PR #${existing_pr} for ${repo} with latest Copilot changes"
  exit 0
fi

pr_body="Propagates Copilot instruction files from [${source_repo}](https://github.com/${source_repo}).

### Changes include:
- Updated \`.github/copilot-instructions.md\` (if present in source)
- Updated \`.github/instructions/\` folder (if present in source)
- Updated \`.github/agents/\` folder (if present in source)
- Updated \`.github/skills/\` folder (if present in source)
- Updated \`.github/prompts/\` folder (if present in source)

**Source repository:** ${source_repo}"

pr_created=false

# ------------------------------------------------------------------
# Strategy 1: GraphQL createPullRequest mutation
# ------------------------------------------------------------------
if [ -n "$repo_node_id" ]; then
  pr_error=$(mktemp)
  gql_input_file=$(mktemp)
  jq -n \
    --arg query 'mutation($repoId:ID!,$base:String!,$head:String!,$title:String!,$body:String!){createPullRequest(input:{repositoryId:$repoId,baseRefName:$base,headRefName:$head,title:$title,body:$body}){pullRequest{url}}}' \
    --arg repoId "$repo_node_id" \
    --arg base "$default_branch" \
    --arg head "$branch" \
    --arg title "Sync Copilot Instructions from ${source_repo}" \
    --arg body "$pr_body" \
    '{query:$query,variables:{repoId:$repoId,base:$base,head:$head,title:$title,body:$body}}' \
    > "$gql_input_file"

  pr_response=$(gh api graphql --input "$gql_input_file" 2>"$pr_error" || true)
  rm -f "$gql_input_file"

  pr_url=$(echo "$pr_response" | jq -r '.data.createPullRequest.pullRequest.url // empty' 2>/dev/null || true)
  if [ -n "$pr_url" ] && [ "$pr_url" != "null" ]; then
    echo "✓ Created PR for ${repo} (GraphQL): ${pr_url}"
    pr_created=true
  else
    gql_err=$(cat "$pr_error" 2>/dev/null || true)
    gql_errors=$(echo "$pr_response" | jq -r '(.errors // []) | map(.message) | join("; ")' 2>/dev/null || true)
    gql_data_errors=$(echo "$pr_response" | jq -r '(.data.createPullRequest.errors // []) | map(.message // .code // "unknown") | join("; ")' 2>/dev/null || true)
    echo "ℹ GraphQL PR creation failed for ${repo}"
    [ -n "$gql_err" ] && echo "  stderr: $gql_err"
    [ -n "$gql_errors" ] && echo "  GraphQL errors: $gql_errors"
    [ -n "$gql_data_errors" ] && echo "  Mutation errors: $gql_data_errors"
    echo "  Full response: $(echo "$pr_response" | head -c 800)"

    if echo "$gql_errors$gql_data_errors" | grep -qi "already exists"; then
      echo "ℹ PR already exists for ${repo} (detected via GraphQL error)"
      pr_created=true
    fi
  fi
  rm -f "$pr_error"
fi

# ------------------------------------------------------------------
# Strategy 2: REST API fallback
# ------------------------------------------------------------------
if [ "$pr_created" = "false" ]; then
  echo "ℹ Trying REST API fallback for ${repo}..."
  pr_error=$(mktemp)
  rest_input_file=$(mktemp)

  jq -n \
    --arg title "Sync Copilot Instructions from ${source_repo}" \
    --arg body "$pr_body" \
    --arg head "$branch" \
    --arg base "$default_branch" \
    '{title:$title, body:$body, head:$head, base:$base}' \
    > "$rest_input_file"

  pr_response=$(gh api -X POST "repos/Cratis/${repo}/pulls" \
    --input "$rest_input_file" \
    2>"$pr_error" || true)
  rm -f "$rest_input_file"

  pr_url=$(echo "$pr_response" | jq -r '.html_url // empty' 2>/dev/null || true)

  if [ -n "$pr_url" ] && [ "$pr_url" != "null" ]; then
    echo "✓ Created PR for ${repo} (REST): ${pr_url}"
    pr_created=true
  else
    rest_err=$(cat "$pr_error" 2>/dev/null || true)
    rest_msg=$(echo "$pr_response" | jq -r '.message // empty' 2>/dev/null || true)
    rest_errors=$(echo "$pr_response" | jq -r '(.errors // []) | map(.message // .code // "unknown") | join("; ")' 2>/dev/null || true)
    echo "⚠ REST PR creation also failed for ${repo}"
    [ -n "$rest_err" ] && echo "  stderr: $rest_err"
    [ -n "$rest_msg" ] && echo "  GitHub message: $rest_msg"
    [ -n "$rest_errors" ] && echo "  Validation errors: $rest_errors"
    echo "  Full response: $(echo "$pr_response" | head -c 800)"

    if echo "$rest_errors$rest_msg" | grep -qi "already exists"; then
      echo "ℹ PR already exists for ${repo} (detected via REST 422)"
      pr_created=true
    elif echo "$rest_errors" | grep -qi "no commits between"; then
      echo "ℹ No diff between ${branch} and ${default_branch} for ${repo} — skipping"
      pr_created=true
    fi
  fi
  rm -f "$pr_error"
fi

if [ "$pr_created" = "false" ]; then
  echo "::error::Could not create PR for ${repo}"
  exit 1
fi
