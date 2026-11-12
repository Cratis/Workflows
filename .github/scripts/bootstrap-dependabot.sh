#!/usr/bin/env bash
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
# Dispatch-only rollout of .github/dependabot.yml. Existing actions entries are untouched;
# other ecosystems/comments are preserved by the pure-append helper. No wrappers or labels.
# GH_TOKEN: PAT_WORKFLOWS with Contents: Read/write and branch-ruleset bypass for writes.
# REPOS_FILE: JSON array of non-archived repository names, supplied by the workflow.
# DRY_RUN defaults to true: GETs only, DRYRUN lines and a summary, no waits or git-data writes.
# SLEEP_CMD replaces the small inter-push delay in offline tests.
set -euo pipefail

# Duplicated from bootstrap-common-workflows.yml's REPOS_TO_IGNORE so this independent,
# manually dispatched rollout does not change or execute the org-wide wrapper bootstrap.
repos_ignore='["Workflows","cratis.github.io","StudioIssues","Documentation","cratis.studio","Automation",".github","Dockerfiles","AI","Templates","Dockerfiles","release-action","Chronicle.Dapr","Chronicle.Wolverine","Stage","Studio","Direct","Ensemble","Infrastructure","Experiments","Strategy","Identity","KeldaKraft","studio-cluster-verification","Ante","Chronicle.Elixir","Components","Orleans","AI.Distribution","Arc.TypeScript","Chronicle.Python","Eventmodelers-Build-Kit-Java","Eventmodelers-Build-Kit-Kotlin","Chronicle.Go","Arc.Go","Fundamentals.Go"]'
# Wrapper-ignored repositories known to carry stale action pins: Dependabot only, not wrappers.
DEPENDABOT_INCLUDE='["Identity","Studio","Stage","Arc.TypeScript","Dockerfiles","AI","Infrastructure","Ensemble"]'
# Respect deliberate deletions, private Actions budgets and separately managed Go repositories.
DEPENDABOT_IGNORE='["Arc.Kotlin","Fundamentals.Kotlin","Arc.Elixir","acme-commerce-demo","SEO","Chronicle-ghsa-8c5g-wcfc-wgfh","Chronicle.Go","Arc.Go","Fundamentals.Go"]'
dry_run="${DRY_RUN:-true}"
case "$dry_run" in true|false) ;; *) echo 'DRY_RUN must be true or false' >&2; exit 2 ;; esac
repos_file="${REPOS_FILE:-${GITHUB_WORKSPACE:-.}/repos.json}"
helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ensure-dependabot-config.py"
jq -e 'type == "array" and all(.[]; type == "string")' "$repos_file" >/dev/null
repos=$(jq -r --argjson ignore "$repos_ignore" --argjson include "$DEPENDABOT_INCLUDE" \
  --argjson exclude "$DEPENDABOT_IGNORE" '
  .[] | select(. as $name | (($ignore | index($name)) == null or ($include | index($name)) != null)
    and ($exclude | index($name)) == null)' "$repos_file")
input_file=$(mktemp)
config_file=$(mktemp)
trap 'rm -f "$input_file" "$config_file"' EXIT
records=()
skipped=()
pushes_done=0
failures=0

# Minimal git-data helpers copied from the common bootstrap; never source its executable body.
extract_sha() {
  local value
  value=$(printf '%s' "$1" | jq -r "${2:-.sha} // empty" 2>/dev/null || true)
  if [[ "$value" =~ ^[0-9a-f]{40,64}$ ]]; then printf '%s\n' "$value"; fi
}

report() {
  local repo="$1" action="$2" path="$3" line
  [ "$dry_run" = true ] || return 0
  line="DRYRUN $repo $action $path"
  echo "$line"
  records+=("$line")
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then echo "$line" >> "$GITHUB_STEP_SUMMARY"; fi
}

read_snapshot() {
  local repo="$1" response
  default_branch=$(gh api "repos/Cratis/$repo" --jq '.default_branch') || return 1
  [ -n "$default_branch" ] || return 1
  response=$(gh api "repos/Cratis/$repo/git/ref/heads/$default_branch") || return 1
  head_sha=$(extract_sha "$response" '.object.sha')
  [ -n "$head_sha" ] || return 1
  response=$(gh api "repos/Cratis/$repo/git/commits/$head_sha") || return 1
  tree_sha=$(extract_sha "$response" '.tree.sha')
  [ -n "$tree_sha" ] || return 1
  subtree=$(gh api "repos/Cratis/$repo/git/trees/$tree_sha?recursive=1") || return 1
  printf '%s' "$subtree" | jq -e '.truncated != true and (.tree | type == "array")' >/dev/null
}

prepare_config() {
  local repo="$1" existing_sha variant_sha content status=0
  variant_sha=$(printf '%s' "$subtree" | jq -r '.tree[] | select(.path == ".github/dependabot.yaml") | .sha')
  if [ -n "$variant_sha" ]; then
    echo "  ⚠ $repo has dependabot.yaml; skipping dependabot.yml"
    skipped+=("$repo .github/dependabot.yaml")
    report "$repo" skip-exists .github/dependabot.yaml
    return 10
  fi
  existing_sha=$(printf '%s' "$subtree" | jq -r '.tree[] | select(.path == ".github/dependabot.yml") | .sha')
  action=add
  : > "$input_file"
  if [ -n "$existing_sha" ]; then
    action=update
    content=$(gh api "repos/Cratis/$repo/git/blobs/$existing_sha" --jq .content) || return 1
    printf '%s' "$content" | base64 -d > "$input_file" || return 1
  fi
  python3 "$helper" < "$input_file" > "$config_file" || status=$?
  case "$status" in
    0) return 0 ;;
    10)
      echo "  ℹ $repo dependabot.yml already has github-actions"
      report "$repo" skip-exists .github/dependabot.yml
      return 10
      ;;
    11)
      echo "  ⚠ Cannot merge dependabot.yml in $repo safely, skipping"
      skipped+=("$repo .github/dependabot.yml")
      report "$repo" skip-unmergeable .github/dependabot.yml
      return 10
      ;;
    *) return 1 ;;
  esac
}

push_config() {
  local repo="$1" response blob_sha new_tree_sha commit_sha updated_sha push_error error
  response=$(gh api -X POST "repos/Cratis/$repo/git/blobs" \
    -f content="$(base64 < "$config_file" | tr -d '\n')" -f encoding=base64) || return 1
  blob_sha=$(extract_sha "$response")
  [ -n "$blob_sha" ] || return 1
  response=$(jq -n --arg base "$tree_sha" --arg sha "$blob_sha" \
    '{base_tree: $base, tree: [{path: ".github/dependabot.yml", mode: "100644", type: "blob", sha: $sha}]}' \
    | gh api -X POST "repos/Cratis/$repo/git/trees" --input -) || return 1
  new_tree_sha=$(extract_sha "$response")
  [ -n "$new_tree_sha" ] || return 1
  response=$(jq -n --arg tree "$new_tree_sha" --arg parent "$head_sha" \
    --arg message "Bootstrap Dependabot: $action dependabot.yml" \
    '{message: $message, tree: $tree, parents: [$parent]}' \
    | gh api -X POST "repos/Cratis/$repo/git/commits" --input -) || return 1
  commit_sha=$(extract_sha "$response")
  [ -n "$commit_sha" ] || return 1
  push_error=$(mktemp)
  if response=$(gh api -X PATCH "repos/Cratis/$repo/git/refs/heads/$default_branch" \
    -f sha="$commit_sha" -F force=false 2>"$push_error"); then
    rm -f "$push_error"
    updated_sha=$(extract_sha "$response" '.object.sha')
    [ -n "$updated_sha" ] || return 1
  else
    error=$(cat "$push_error")
    rm -f "$push_error"
    echo "$error" >&2
    if grep -Eqi 'not a fast forward|not fast forward|non-fast-forward' <<< "$error $response"; then return 75; fi
    return 1
  fi
  echo "  ✓ Pushed to $default_branch in $repo: Bootstrap Dependabot: $action dependabot.yml"
  pushed=true
}

update_repo() {
  local repo="$1" default_branch head_sha tree_sha subtree action status=0 permission
  pushed=false
  read_snapshot "$repo" || return 1
  prepare_config "$repo" || status=$?
  case "$status" in 10) return 0 ;; 0) ;; *) return 1 ;; esac
  if [ "$dry_run" = true ]; then
    report "$repo" "$action" .github/dependabot.yml
    return 0
  fi
  permission=$(gh api "repos/Cratis/$repo" --jq '.permissions.push // false') || return 1
  if [ "$permission" != true ]; then
    echo "  ⚠ PAT_WORKFLOWS does not have write (push) access to Cratis/$repo" >&2
    return 1
  fi
  push_config "$repo"
}

summary() {
  local record repo action path
  if [ "$dry_run" = true ]; then
    echo 'DRYRUN-SUMMARY'
    printf '| Repository | Action | File |\n| --- | --- | --- |\n'
    for record in "${records[@]}"; do
      read -r _ repo action path <<< "$record"
      printf '| %s | %s | %s |\n' "$repo" "$action" "$path"
    done
  fi
  if [ "${#skipped[@]}" -gt 0 ]; then printf 'DEPENDABOT-SKIPPED %s\n' "${skipped[@]}"; fi
  echo "Summary: $pushes_done commit(s), $failures failure(s)"
}

while IFS= read -r repo; do
  [ -n "$repo" ] || continue
  echo "Processing Cratis/$repo..."
  result=0
  update_repo "$repo" || result=$?
  if [ "$result" -eq 75 ]; then
    echo '  Branch moved; retrying once from fresh HEAD'
    result=0
    update_repo "$repo" || result=$?
  fi
  if [ "$result" -ne 0 ]; then
    echo "  ⚠ Could not update Dependabot in $repo" >&2
    failures=$((failures + 1))
    continue
  fi
  if [ "$pushed" = true ]; then
    pushes_done=$((pushes_done + 1))
    "${SLEEP_CMD:-sleep}" 1
  fi
done <<< "$repos"
summary
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then summary >> "$GITHUB_STEP_SUMMARY"; fi
[ "$failures" -eq 0 ]
