#!/usr/bin/env bash
# Shared helper: filters copilot_files JSON array using ignore-pattern files.
#
# Provides two public functions:
#
# _apply_copilot_sync_ignore()
#   Filters using .github/.copilot-sync-ignore from the SOURCE repository.
#   Expects:  source_tree_raw, source_repo, copilot_files
#
# _apply_copilot_sync_receive_ignore()
#   Filters using .github/.copilot-sync-receive-ignore from the TARGET repository.
#   Expects:  target_tree_raw, target_repo_name, copilot_files
#
# Both functions update copilot_files in place (filtered).
# Returns 1 if all files are excluded (caller should handle the exit).

# _glob_patterns_to_regex <ignore_content>
# Converts newline-separated glob patterns into a combined ERE regex.
# Skips blank lines and lines starting with #.
# Auto-prepends .github/ to patterns that lack it.
# Outputs the combined regex via stdout. Empty string if no valid patterns.
#
# Glob-to-regex conversions:
#   **  → .*          (match across directories)
#   *   → [^/]*       (match within a single directory)
#   ?   → [^/]        (match a single character)
#   .   → \.          (literal dot)
_glob_patterns_to_regex() {
  local ignore_content="$1"
  local combined_regex=""
  local pattern regex

  while IFS= read -r pattern || [ -n "$pattern" ]; do
    pattern=$(echo "$pattern" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    [ -z "$pattern" ] && continue
    [[ "$pattern" == \#* ]] && continue

    # Normalize: ensure .github/ prefix
    [[ "$pattern" != .github/* ]] && pattern=".github/${pattern}"

    # Convert glob → regex (order matters: ** before *)
    regex=$(printf '%s' "$pattern" \
      | sed -e 's/\*\*/__GLOBSTAR__/g' \
            -e 's/\*/__STAR__/g' \
            -e 's/\./\\./g' \
            -e 's|?|[^/]|g' \
            -e 's/__GLOBSTAR__/.*/g' \
            -e 's/__STAR__/[^\/]*/g')

    if [ -n "$combined_regex" ]; then
      combined_regex="${combined_regex}|^${regex}$"
    else
      combined_regex="^${regex}$"
    fi
  done <<< "$ignore_content"

  echo "$combined_regex"
}

_apply_copilot_sync_ignore() {
  local ignore_sha
  ignore_sha=$(echo "$source_tree_raw" | jq -r \
    '.tree[] | select(.path == ".github/.copilot-sync-ignore") | .sha // empty' \
    2>/dev/null || true)

  [ -z "$ignore_sha" ] && return 0

  echo "ℹ Found .copilot-sync-ignore in ${source_repo}"
  local ignore_blob
  ignore_blob=$(gh api "repos/${source_repo}/git/blobs/${ignore_sha}" \
    --jq '.content' 2>/dev/null || true)
  local ignore_content
  ignore_content=$(echo "$ignore_blob" | base64 -d 2>/dev/null || true)

  [ -z "$ignore_content" ] && return 0

  local combined_regex
  combined_regex=$(_glob_patterns_to_regex "$ignore_content")

  [ -z "$combined_regex" ] && return 0

  local before_count after_count excluded
  before_count=$(echo "$copilot_files" | jq 'length')
  copilot_files=$(echo "$copilot_files" | jq -c \
    --arg regex "$combined_regex" \
    '[.[] | select(.path | test($regex) | not)]')
  after_count=$(echo "$copilot_files" | jq 'length')
  excluded=$((before_count - after_count))

  if [ "$excluded" -gt 0 ]; then
    echo "  Excluded ${excluded} file(s) matching .copilot-sync-ignore patterns"
  fi

  if [ "$copilot_files" = "[]" ]; then
    return 1
  fi

  echo "✓ After filtering: ${after_count} file(s) remaining"
}

_apply_copilot_sync_receive_ignore() {
  local ignore_sha
  ignore_sha=$(echo "$target_tree_raw" | jq -r \
    '.tree[] | select(.path == ".github/.copilot-sync-receive-ignore") | .sha // empty' \
    2>/dev/null || true)

  [ -z "$ignore_sha" ] && return 0

  echo "ℹ Found .copilot-sync-receive-ignore in ${target_repo_name}"
  local ignore_blob
  ignore_blob=$(gh api "repos/${target_repo_name}/git/blobs/${ignore_sha}" \
    --jq '.content' 2>/dev/null || true)
  local ignore_content
  ignore_content=$(echo "$ignore_blob" | base64 -d 2>/dev/null || true)

  [ -z "$ignore_content" ] && return 0

  local combined_regex
  combined_regex=$(_glob_patterns_to_regex "$ignore_content")

  [ -z "$combined_regex" ] && return 0

  local before_count after_count excluded
  before_count=$(echo "$copilot_files" | jq 'length')
  copilot_files=$(echo "$copilot_files" | jq -c \
    --arg regex "$combined_regex" \
    '[.[] | select(.path | test($regex) | not)]')
  after_count=$(echo "$copilot_files" | jq 'length')
  excluded=$((before_count - after_count))

  if [ "$excluded" -gt 0 ]; then
    echo "  Excluded ${excluded} file(s) matching .copilot-sync-receive-ignore patterns"
  fi

  if [ "$copilot_files" = "[]" ]; then
    return 1
  fi

  echo "✓ After receive-ignore filtering: ${after_count} file(s) remaining"
}
