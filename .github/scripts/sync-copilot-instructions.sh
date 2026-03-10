#!/usr/bin/env bash
# Synchronizes Copilot instruction files from a source repository to a single
# target repository, opening a PR with the changes.
# Called by .github/workflows/sync-copilot-instructions.yml
#
# Expects:
#   GH_TOKEN      - PAT with Contents (r/w) + Pull requests (r/w) + Workflows (r/w)
#   SOURCE_REPO   - source repository in owner/repo format (e.g. Cratis/AI)
#   TARGET_REPO   - target repository in owner/repo format (e.g. Cratis/SomeRepo)

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
target_repo="${TARGET_REPO:?TARGET_REPO must be set}"
target_name="${target_repo##*/}"

branch="copilot-sync/update-instructions"

pr_body="Synchronizes Copilot instruction files from [${source_repo}](https://github.com/${source_repo}).

### Changes include:
- Updated \`.github/copilot-instructions.md\` (if present in source)
- Updated \`.github/instructions/\` folder (if present in source)
- Updated \`.github/agents/\` folder (if present in source)
- Updated \`.github/skills/\` folder (if present in source)
- Updated \`.github/prompts/\` folder (if present in source)

**Source repository:** ${source_repo}"

echo "Syncing Copilot instructions from ${source_repo} to ${target_repo}..."

# ----------------------------------------------------------------
# 1. Fetch Copilot files from the source repository
# ----------------------------------------------------------------
source_tree_error=$(mktemp)
source_tree_raw=$(gh api "repos/${source_repo}/git/trees/HEAD?recursive=1" \
  2>"$source_tree_error" || true)
rm -f "$source_tree_error"

if [ -z "$source_tree_raw" ]; then
  echo "::error::Could not fetch tree from ${source_repo}"
  exit 1
fi

copilot_files=$(echo "$source_tree_raw" | jq -c \
  '[.tree[] | select(.type == "blob") |
   select(.path | test("^\\.github/(copilot-instructions\\.md$|instructions/|agents/|skills/|prompts/)")) |
   {path: .path, sha: .sha}]' 2>/dev/null || true)

if [ -z "$copilot_files" ] || [ "$copilot_files" = "[]" ]; then
  echo "No Copilot instruction files found in ${source_repo} — nothing to sync."
  exit 0
fi

echo "✓ Found $(echo "$copilot_files" | jq 'length') Copilot file(s) in ${source_repo}"

# ----------------------------------------------------------------
# 1b. Filter out files matching .copilot-sync-ignore patterns
# ----------------------------------------------------------------
ignore_sha=$(echo "$source_tree_raw" | jq -r \
  '.tree[] | select(.path == ".github/.copilot-sync-ignore") | .sha // empty' \
  2>/dev/null || true)

if [ -n "$ignore_sha" ]; then
  echo "ℹ Found .copilot-sync-ignore in ${source_repo}"
  ignore_blob=$(gh api "repos/${source_repo}/git/blobs/${ignore_sha}" \
    --jq '.content' 2>/dev/null || true)
  ignore_content=$(echo "$ignore_blob" | base64 -d 2>/dev/null || true)

  if [ -n "$ignore_content" ]; then
    # Build a combined regex from all non-comment, non-empty lines.
    # Each glob pattern is converted to a regex:
    #   **  → .*          (match across directories)
    #   *   → [^/]*       (match within a single directory)
    #   ?   → [^/]        (match a single character)
    #   .   → \.          (literal dot)
    # Patterns without a .github/ prefix get one prepended automatically.
    combined_regex=""
    while IFS= read -r pattern || [ -n "$pattern" ]; do
      pattern=$(echo "$pattern" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
      [ -z "$pattern" ] && continue
      [[ "$pattern" == \#* ]] && continue

      # Normalise: ensure .github/ prefix
      [[ "$pattern" != .github/* ]] && pattern=".github/${pattern}"

      # Convert glob → regex (order matters: ** before *)
      regex=$(printf '%s' "$pattern" \
        | sed -e 's/\*\*/__GLOBSTAR__/g' \
              -e 's/\*/__STAR__/g' \
              -e 's/\./\\./g' \
              -e 's/?/[^\/]/g' \
              -e 's/__GLOBSTAR__/.*/g' \
              -e 's/__STAR__/[^\/]*/g')

      if [ -n "$combined_regex" ]; then
        combined_regex="${combined_regex}|^${regex}$"
      else
        combined_regex="^${regex}$"
      fi
    done <<< "$ignore_content"

    if [ -n "$combined_regex" ]; then
      before_count=$(echo "$copilot_files" | jq 'length')
      copilot_files=$(echo "$copilot_files" | jq -c \
        --arg regex "$combined_regex" \
        '[.[] | select(.path | test($regex) | not)]')
      after_count=$(echo "$copilot_files" | jq 'length')
      excluded=$((before_count - after_count))

      if [ "$excluded" -gt 0 ]; then
        echo "  Excluded ${excluded} file(s) matching .copilot-sync-ignore patterns"
      fi

      if [ "$copilot_files" = "[]" ] || [ -z "$copilot_files" ]; then
        echo "All Copilot files excluded by .copilot-sync-ignore — nothing to sync."
        exit 0
      fi

      echo "✓ After filtering: ${after_count} file(s) to sync"
    fi
  fi
fi

# ----------------------------------------------------------------
# 2. Get target repository info (default branch, node ID, HEAD SHA)
# ----------------------------------------------------------------
repo_info_error=$(mktemp)
repo_info_json=$(gh api "repos/${target_repo}" \
  --jq '{default_branch: .default_branch, node_id: .node_id}' \
  2>"$repo_info_error" || true)
default_branch=$(echo "$repo_info_json" | jq -r '.default_branch // empty' 2>/dev/null || true)
repo_node_id=$(echo "$repo_info_json" | jq -r '.node_id // empty' 2>/dev/null || true)
rm -f "$repo_info_error"

if [ -z "$default_branch" ]; then
  echo "::error::Could not get default branch for ${target_repo}"
  exit 1
fi

head_sha_error=$(mktemp)
_head_sha_resp=$(gh api "repos/${target_repo}/git/ref/heads/${default_branch}" \
  2>"$head_sha_error" || true)
head_sha=$(extract_sha "$_head_sha_resp" '.object.sha')
rm -f "$head_sha_error"

if [ -z "$head_sha" ]; then
  echo "::error::Could not get HEAD SHA for ${target_repo} (${default_branch} branch not found)"
  exit 1
fi

# ----------------------------------------------------------------
# 3. Get the HEAD commit's tree SHA and current full tree
# ----------------------------------------------------------------
tree_sha_error=$(mktemp)
_tree_sha_resp=$(gh api "repos/${target_repo}/git/commits/${head_sha}" \
  2>"$tree_sha_error" || true)
tree_sha=$(extract_sha "$_tree_sha_resp" '.tree.sha')
rm -f "$tree_sha_error"

if [ -z "$tree_sha" ]; then
  echo "::error::Could not get tree SHA for ${target_repo}"
  exit 1
fi

subtree_error=$(mktemp)
subtree=$(gh api "repos/${target_repo}/git/trees/${tree_sha}?recursive=1" \
  2>"$subtree_error" || true)
rm -f "$subtree_error"

if [ -z "$subtree" ]; then
  echo "::error::Could not fetch repository tree for ${target_repo}"
  exit 1
fi

# ----------------------------------------------------------------
# 4. Check whether all copilot files are already up to date
#    (git blob SHAs are content-addressed across repositories)
# ----------------------------------------------------------------
files_up_to_date=true
while IFS=' ' read -r chk_path chk_sha; do
  [ -z "$chk_path" ] && continue
  existing_sha=$(echo "$subtree" | jq -r \
    --arg p "$chk_path" \
    '.tree[] | select(.path == $p) | .sha // empty' 2>/dev/null || true)
  if [ "$existing_sha" != "$chk_sha" ]; then
    files_up_to_date=false
    break
  fi
done <<< "$(echo "$copilot_files" | jq -r '.[] | .path + " " + .sha' 2>/dev/null || true)"

if [ "$files_up_to_date" = "true" ]; then
  echo "ℹ All Copilot files in ${target_repo} are already up to date — skipping."
  exit 0
fi

# ----------------------------------------------------------------
# 5. Create blobs in the target repository for each source file
# ----------------------------------------------------------------
new_tree_json=$(jq -n --arg base_tree "$tree_sha" \
  '{"base_tree": $base_tree, "tree": []}')

copy_failed=false
while IFS=' ' read -r src_path src_sha; do
  [ -z "$src_path" ] && continue

  # Fetch blob content from source repo (returned as base64 by API)
  blob_error=$(mktemp)
  blob_content=$(gh api "repos/${source_repo}/git/blobs/${src_sha}" \
    --jq '.content' 2>"$blob_error" || true)
  rm -f "$blob_error"

  if [ -z "$blob_content" ]; then
    echo "  ⚠ Could not fetch blob for ${src_path} from ${source_repo}"
    copy_failed=true
    break
  fi

  # Strip embedded newlines that the API inserts into base64 output
  clean_b64=$(echo "$blob_content" | tr -d '\n')

  target_blob_error=$(mktemp)
  _target_blob_resp=$(gh api -X POST "repos/${target_repo}/git/blobs" \
    -f "content=${clean_b64}" \
    -f encoding=base64 \
    2>"$target_blob_error" || true)
  target_blob_sha=$(extract_sha "$_target_blob_resp")
  rm -f "$target_blob_error"

  if [ -z "$target_blob_sha" ]; then
    echo "  ⚠ Could not create blob for ${src_path} in ${target_repo}"
    copy_failed=true
    break
  fi

  new_tree_json=$(echo "$new_tree_json" | jq \
    --arg p "$src_path" \
    --arg s "$target_blob_sha" \
    '.tree += [{path: $p, mode: "100644", type: "blob", sha: $s}]')
done <<< "$(echo "$copilot_files" | jq -r '.[] | .path + " " + .sha' 2>/dev/null || true)"

if [ "$copy_failed" = "true" ]; then
  echo "::error::Failed to prepare file blobs for ${target_repo}"
  exit 1
fi

# ----------------------------------------------------------------
# 6. Create new tree, commit
# ----------------------------------------------------------------
new_tree_error=$(mktemp)
_new_tree_resp=$(echo "$new_tree_json" | \
  gh api -X POST "repos/${target_repo}/git/trees" \
  --input - 2>"$new_tree_error" || true)
new_tree_sha=$(extract_sha "$_new_tree_resp")
rm -f "$new_tree_error"

if [ -z "$new_tree_sha" ]; then
  echo "::error::Could not create tree in ${target_repo}"
  exit 1
fi

commit_error=$(mktemp)
_commit_resp=$(jq -n \
  --arg msg  "Sync Copilot instructions from ${source_repo}" \
  --arg tree "$new_tree_sha" \
  --arg parent "$head_sha" \
  '{"message": $msg, "tree": $tree, "parents": [$parent]}' | \
  gh api -X POST "repos/${target_repo}/git/commits" \
  --input - 2>"$commit_error" || true)
new_commit_sha=$(extract_sha "$_commit_resp")
rm -f "$commit_error"

if [ -z "$new_commit_sha" ]; then
  echo "::error::Could not create commit in ${target_repo}"
  exit 1
fi

echo "  ✓ Created commit ${new_commit_sha} in ${target_repo}"

# ----------------------------------------------------------------
# 7. Create or force-update the feature branch via GraphQL
#
# GraphQL createRef/updateRef register the branch in GitHub's branch
# index, which is required for the Pulls API and createPullRequest
# mutation.  REST low-level ref writes do NOT register in the index.
# ----------------------------------------------------------------
branch_error=$(mktemp)
branch_ok=""

existing_ref_result=$(gh api graphql \
  -f query='query($owner:String!,$name:String!,$ref:String!){repository(owner:$owner,name:$name){ref(qualifiedName:$ref){id target{oid}}}}' \
  -f owner="Cratis" \
  -f name="$target_name" \
  -f ref="refs/heads/${branch}" \
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
    echo "::error::No repository node ID for ${target_repo}; cannot create branch via GraphQL"
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
  echo "::error::Could not create/update branch ${branch} in ${target_repo} (GraphQL)"
  [ -n "$branch_api_error" ] && echo "  stderr: $branch_api_error"
  [ -n "$branch_gql_errors" ] && echo "  GraphQL errors: $branch_gql_errors"
  rm -f "$branch_error"
  exit 1
fi
rm -f "$branch_error"
echo "  ✓ Branch ${branch} ready in ${target_repo} (GraphQL)"

# ----------------------------------------------------------------
# 8. Create PR (skip if one already exists for this branch)
# ----------------------------------------------------------------
existing_pr=""
list_pr_error=$(mktemp)
if api_result=$(gh api "repos/${target_repo}/pulls?state=open&head=Cratis:${branch}" 2>"$list_pr_error"); then
  existing_pr=$(echo "$api_result" | jq -r '.[0].number // empty' 2>/dev/null || true)
fi
rm -f "$list_pr_error"

if [ -n "$existing_pr" ] && [ "$existing_pr" != "null" ]; then
  echo "  ℹ PR already exists for ${target_repo} (#${existing_pr})"
  exit 0
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
    --arg title "Sync Copilot Instructions from ${source_repo}" \
    --arg body "$pr_body" \
    '{query:$query,variables:{repoId:$repoId,base:$base,head:$head,title:$title,body:$body}}' \
    > "$gql_input_file"

  pr_response=$(gh api graphql --input "$gql_input_file" 2>"$pr_error" || true)
  rm -f "$gql_input_file"

  pr_url=$(echo "$pr_response" | jq -r '.data.createPullRequest.pullRequest.url // empty' 2>/dev/null || true)
  if [ -n "$pr_url" ] && [ "$pr_url" != "null" ]; then
    echo "  ✓ Created PR for ${target_repo} (GraphQL): ${pr_url}"
    pr_created=true
  else
    gql_err=$(cat "$pr_error" 2>/dev/null || true)
    gql_errors=$(echo "$pr_response" | jq -r '(.errors // []) | map(.message) | join("; ")' 2>/dev/null || true)
    gql_data_errors=$(echo "$pr_response" | jq -r '(.data.createPullRequest.errors // []) | map(.message // .code // "unknown") | join("; ")' 2>/dev/null || true)
    echo "  ℹ GraphQL PR creation failed for ${target_repo}"
    [ -n "$gql_err" ] && echo "    stderr: $gql_err"
    [ -n "$gql_errors" ] && echo "    GraphQL errors: $gql_errors"
    [ -n "$gql_data_errors" ] && echo "    Mutation errors: $gql_data_errors"

    if echo "$gql_errors$gql_data_errors" | grep -qi "already exists"; then
      echo "  ℹ PR already exists for ${target_repo} (detected via GraphQL error)"
      pr_created=true
    fi
  fi
  rm -f "$pr_error"
fi

# Strategy 2: REST API fallback
if [ "$pr_created" = "false" ]; then
  echo "  ℹ Trying REST API fallback for ${target_repo}..."
  pr_error=$(mktemp)
  rest_input_file=$(mktemp)

  jq -n \
    --arg title "Sync Copilot Instructions from ${source_repo}" \
    --arg body "$pr_body" \
    --arg head "$branch" \
    --arg base "$default_branch" \
    '{title:$title, body:$body, head:$head, base:$base}' \
    > "$rest_input_file"

  pr_response=$(gh api -X POST "repos/${target_repo}/pulls" \
    --input "$rest_input_file" \
    2>"$pr_error" || true)
  rm -f "$rest_input_file"

  pr_url=$(echo "$pr_response" | jq -r '.html_url // empty' 2>/dev/null || true)

  if [ -n "$pr_url" ] && [ "$pr_url" != "null" ]; then
    echo "  ✓ Created PR for ${target_repo} (REST): ${pr_url}"
    pr_created=true
  else
    rest_err=$(cat "$pr_error" 2>/dev/null || true)
    rest_msg=$(echo "$pr_response" | jq -r '.message // empty' 2>/dev/null || true)
    rest_errors=$(echo "$pr_response" | jq -r '(.errors // []) | map(.message // .code // "unknown") | join("; ")' 2>/dev/null || true)
    echo "  ⚠ REST PR creation also failed for ${target_repo}"
    [ -n "$rest_err" ] && echo "    stderr: $rest_err"
    [ -n "$rest_msg" ] && echo "    GitHub message: $rest_msg"
    [ -n "$rest_errors" ] && echo "    Validation errors: $rest_errors"

    if echo "$rest_errors$rest_msg" | grep -qi "already exists"; then
      echo "  ℹ PR already exists for ${target_repo} (detected via REST 422)"
      pr_created=true
    elif echo "$rest_errors" | grep -qi "no commits between"; then
      echo "  ℹ No diff between ${branch} and ${default_branch} for ${target_repo} — skipping"
      pr_created=true
    fi
  fi
  rm -f "$pr_error"
fi

if [ "$pr_created" = "false" ]; then
  echo "::error::Could not create PR for ${target_repo}"
  exit 1
fi
