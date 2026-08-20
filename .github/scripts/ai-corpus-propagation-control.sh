#!/usr/bin/env bash
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
#
# Freezes, verifies, and deliberately restores the Cratis AI corpus propagation workflows.
# Workflow state is changed through the GitHub Actions API; repository files and refs are untouched.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$script_dir/github-api-retry.sh"

organization="${CRATIS_ORGANIZATION:-Cratis}"
source_repository="${AI_CORPUS_SOURCE_REPOSITORY:-AI}"
workflows_repository="${AI_CORPUS_WORKFLOWS_REPOSITORY:-Workflows}"
operation="${1:-status}"
apply=false
canary_repository=""
snapshot_file=""
confirmation=""
confirm_legacy=false
failures=0
changed=0
skipped=0
cancelled=0
inventory_file=""
temporary_directory=""

propagate_path=".github/workflows/propagate-copilot-instructions.yml"
sync_path=".github/workflows/sync-copilot-instructions.yml"
bootstrap_path=".github/workflows/bootstrap-copilot-sync.yml"

usage() {
  cat <<'EOF'
Usage:
  ai-corpus-propagation-control.sh status [options]
  ai-corpus-propagation-control.sh verify-frozen [options]
  ai-corpus-propagation-control.sh freeze --snapshot <path> [--apply] [options]
  ai-corpus-propagation-control.sh restore --snapshot <path> [--apply] [options]
  ai-corpus-propagation-control.sh canary --repo <name> [--apply] [options]
  ai-corpus-propagation-control.sh enable-single-source [--apply] [options]
  ai-corpus-propagation-control.sh enable-legacy-all-to-all \
      --confirm-legacy-all-to-all [--apply] [options]

Operations:
  status
      Reports controlled workflow states and queued or running runs.

  verify-frozen
      Fails unless every controlled workflow is inactive and no controlled run is
      queued or running.

  freeze
      Takes a complete, versioned state snapshot, disables only workflows that are
      currently active, cancels queued or running controlled runs, and waits for
      quiescence. Applying a freeze requires --snapshot.

  restore
      Validates a freeze snapshot against the complete current topology and enables
      only workflows that the snapshot recorded as active. Workflows that were
      already inactive before the freeze are never enabled.

  canary
      Quiesces the complete propagation system, enables only the central reusable
      sync workflow and one target repository's manual sync wrapper, then dispatches
      a PR-based sync from the corpus source.

  enable-single-source
      Quiesces the complete system, enables the central reusable workflows and
      target sync wrappers, and enables automatic propagation only in the
      authoritative source repository.

  enable-legacy-all-to-all
      Quiesces the complete system, then enables every propagation and sync wrapper
      plus bootstrap. This dangerous legacy topology requires explicit confirmation.

Options:
  --apply                         Perform GitHub mutations. Otherwise, dry-run.
  --confirm-organization <name>   Required with --apply; must exactly match the org.
  --snapshot <path>               Snapshot output (freeze) or input (restore).
  --repo <name>                   Canary target repository name.
  --organization <name>          GitHub organization (default: Cratis).
  --source-repository <name>      Authoritative corpus repository (default: AI).
  --workflows-repository <name>   Reusable workflows repository (default: Workflows).
  --confirm-legacy-all-to-all     Required for the legacy topology.
  -h, --help                      Show this help.

Prerequisites: Bash, GitHub CLI (gh), jq, and a token with Actions read access.
Mutations additionally require Actions write access to every controlled repository.
EOF
}

shift || true
while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply)
      apply=true
      ;;
    --confirm-organization)
      [ "$#" -ge 2 ] || { echo "--confirm-organization requires a value" >&2; exit 2; }
      confirmation="$2"
      shift
      ;;
    --snapshot)
      [ "$#" -ge 2 ] || { echo "--snapshot requires a value" >&2; exit 2; }
      snapshot_file="$2"
      shift
      ;;
    --repo)
      [ "$#" -ge 2 ] || { echo "--repo requires a value" >&2; exit 2; }
      canary_repository="$2"
      shift
      ;;
    --organization)
      [ "$#" -ge 2 ] || { echo "--organization requires a value" >&2; exit 2; }
      organization="$2"
      shift
      ;;
    --source-repository)
      [ "$#" -ge 2 ] || { echo "--source-repository requires a value" >&2; exit 2; }
      source_repository="$2"
      shift
      ;;
    --workflows-repository)
      [ "$#" -ge 2 ] || { echo "--workflows-repository requires a value" >&2; exit 2; }
      workflows_repository="$2"
      shift
      ;;
    --confirm-legacy-all-to-all)
      confirm_legacy=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Required command not found: $1" >&2
    exit 2
  }
}

validate_repository_name() {
  local value="$1"
  local label="$2"
  if [[ ! "$value" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo "$label must be a repository name, not owner/name: $value" >&2
    exit 2
  fi
}

cleanup() {
  [ -z "$temporary_directory" ] || rm -rf "$temporary_directory"
}
trap cleanup EXIT

require_command gh
require_command jq
validate_repository_name "$organization" "--organization"
validate_repository_name "$source_repository" "--source-repository"
validate_repository_name "$workflows_repository" "--workflows-repository"
[ -z "$canary_repository" ] || validate_repository_name "$canary_repository" "--repo"

case "$operation" in
  status|verify-frozen)
    if [ "$apply" = true ]; then
      echo "$operation is read-only and does not accept --apply" >&2
      exit 2
    fi
    ;;
  freeze)
    if [ "$apply" = true ] && [ -z "$snapshot_file" ]; then
      echo "freeze --apply requires --snapshot <path>" >&2
      exit 2
    fi
    ;;
  restore)
    [ -n "$snapshot_file" ] || { echo "restore requires --snapshot <path>" >&2; exit 2; }
    ;;
  canary)
    [ -n "$canary_repository" ] || { echo "canary requires --repo <name>" >&2; exit 2; }
    ;;
  enable-single-source)
    ;;
  enable-legacy-all-to-all)
    [ "$confirm_legacy" = true ] || {
      echo "enable-legacy-all-to-all requires --confirm-legacy-all-to-all" >&2
      exit 2
    }
    ;;
  help|-h|--help)
    usage
    exit 0
    ;;
  *)
    echo "Unknown operation: $operation" >&2
    usage >&2
    exit 2
    ;;
esac

if [ "$apply" = true ] && [ "$confirmation" != "$organization" ]; then
  echo "--apply requires --confirm-organization $organization" >&2
  exit 2
fi

if [ "$apply" = true ]; then
  echo "Mode: APPLY (organization confirmation matched)"
else
  echo "Mode: DRY RUN (pass --apply --confirm-organization $organization to mutate GitHub state)"
fi

echo "Organization: $organization"
echo "Corpus source: $organization/$source_repository"
echo "Reusable workflows: $organization/$workflows_repository"

temporary_directory=$(mktemp -d)
inventory_file="$temporary_directory/inventory.json"

list_repository_pages() {
  gh_api_with_retry --paginate --slurp "orgs/$organization/repos?per_page=100&type=all"
}

list_workflow_pages() {
  local repository="$1"
  gh_api_with_retry --paginate --slurp \
    "repos/$organization/$repository/actions/workflows?per_page=100"
}

list_run_pages() {
  local repository="$1"
  local workflow_id="$2"
  gh_api_with_retry --paginate --slurp \
    "repos/$organization/$repository/actions/workflows/$workflow_id/runs?per_page=100"
}

build_inventory() {
  local repository_pages
  local repositories_json
  local workflows_ndjson="$temporary_directory/workflows.ndjson"
  local repository
  local workflow_pages
  local matching_records
  local record
  local workflow_id
  local runs_pages
  local active_runs
  local duplicate_paths

  : > "$workflows_ndjson"
  echo
  echo "Preflight: reading the complete controlled-workflow topology..."

  if ! repository_pages=$(list_repository_pages); then
    echo "Failed to list repositories in $organization" >&2
    return 1
  fi
  if ! repositories_json=$(jq -ce \
    '[.[][] | select(.archived == false) | .name] | unique | sort' \
    <<< "$repository_pages"); then
    echo "GitHub returned malformed repository data for $organization" >&2
    return 1
  fi
  if [ "$(jq 'length' <<< "$repositories_json")" -eq 0 ]; then
    echo "GitHub returned no active repositories for $organization; refusing to continue" >&2
    return 1
  fi

  while IFS= read -r repository; do
    if ! workflow_pages=$(list_workflow_pages "$repository"); then
      echo "  ✗ $organization/$repository :: failed to list workflows" >&2
      return 1
    fi
    if ! matching_records=$(jq -ce \
      --arg repository "$repository" \
      --arg workflows_repository "$workflows_repository" \
      --arg propagate "$propagate_path" \
      --arg sync "$sync_path" \
      --arg bootstrap "$bootstrap_path" '
        [ .[]?.workflows[]?
          | select(
              .path == $propagate or
              .path == $sync or
              ($repository == $workflows_repository and .path == $bootstrap))
          | {
              repository: $repository,
              id: .id,
              name: .name,
              path: .path,
              state: .state
            }
        ]' <<< "$workflow_pages"); then
      echo "  ✗ $organization/$repository :: malformed workflow response" >&2
      return 1
    fi

    duplicate_paths=$(jq -r 'group_by(.path)[] | select(length > 1) | .[0].path' <<< "$matching_records")
    if [ -n "$duplicate_paths" ]; then
      echo "  ✗ $organization/$repository :: duplicate controlled workflow path(s):" >&2
      printf '%s\n' "$duplicate_paths" | sed 's/^/      /' >&2
      return 1
    fi

    while IFS= read -r record; do
      [ -n "$record" ] || continue
      workflow_id=$(jq -r '.id' <<< "$record")
      if ! runs_pages=$(list_run_pages "$repository" "$workflow_id"); then
        echo "  ✗ $organization/$repository :: failed to list all runs for workflow $workflow_id" >&2
        return 1
      fi
      if ! active_runs=$(jq -ce '
          [ .[]?.workflow_runs[]?
            | select(.status != "completed")
            | {id: .id, status: .status}
          ]
          | unique_by(.id)
          | sort_by(.id)' <<< "$runs_pages"); then
        echo "  ✗ $organization/$repository :: malformed run response for workflow $workflow_id" >&2
        return 1
      fi
      jq -c --argjson active_runs "$active_runs" '. + {active_runs: $active_runs}' \
        <<< "$record" >> "$workflows_ndjson"
    done < <(jq -c '.[]' <<< "$matching_records")
  done < <(jq -r '.[]' <<< "$repositories_json")

  jq -n \
    --arg organization "$organization" \
    --arg source_repository "$source_repository" \
    --arg workflows_repository "$workflows_repository" \
    --argjson repositories "$repositories_json" \
    --slurpfile workflows "$workflows_ndjson" '
      {
        organization: $organization,
        source_repository: $source_repository,
        workflows_repository: $workflows_repository,
        repositories: $repositories,
        workflows: $workflows
      }' > "$inventory_file"

  echo "  ✓ inspected $(jq '.repositories | length' "$inventory_file") repositories and $(jq '.workflows | length' "$inventory_file") controlled workflows"
}

workflow_record() {
  local repository="$1"
  local workflow_path="$2"
  jq -c --arg repository "$repository" --arg path "$workflow_path" '
    .workflows[] | select(.repository == $repository and .path == $path)' "$inventory_file"
}

require_workflow() {
  local repository="$1"
  local workflow_path="$2"
  local count

  count=$(jq --arg repository "$repository" --arg path "$workflow_path" '
    [.workflows[] | select(.repository == $repository and .path == $path)] | length' "$inventory_file")
  if [ "$count" -ne 1 ]; then
    echo "Required workflow is missing: $organization/$repository/$workflow_path" >&2
    return 1
  fi
}

require_repository() {
  local repository="$1"
  if ! jq -e --arg repository "$repository" '.repositories | index($repository) != null' \
    "$inventory_file" >/dev/null; then
    echo "Required repository was not found: $organization/$repository" >&2
    return 1
  fi
}

require_central_workflows() {
  require_repository "$workflows_repository" &&
    require_workflow "$workflows_repository" "$bootstrap_path" &&
    require_workflow "$workflows_repository" "$propagate_path" &&
    require_workflow "$workflows_repository" "$sync_path"
}

set_record_state() {
  local record="$1"
  local desired_state="$2"
  local current_state_override="${3:-}"
  local repository
  local workflow_id
  local workflow_name
  local current_state
  local action

  repository=$(jq -r '.repository' <<< "$record")
  workflow_id=$(jq -r '.id' <<< "$record")
  workflow_name=$(jq -r '.name' <<< "$record")
  current_state=$(jq -r '.state' <<< "$record")
  [ -z "$current_state_override" ] || current_state="$current_state_override"

  if [ "$desired_state" = "disabled_manually" ] && [ "$current_state" != "active" ]; then
    echo "  = $organization/$repository :: $workflow_name remains $current_state"
    skipped=$((skipped + 1))
    return 0
  fi
  if [ "$current_state" = "$desired_state" ]; then
    echo "  = $organization/$repository :: $workflow_name ($current_state)"
    skipped=$((skipped + 1))
    return 0
  fi

  if [ "$desired_state" = "active" ]; then
    action="enable"
  else
    action="disable"
  fi

  if [ "$apply" = false ]; then
    echo "  ~ $organization/$repository :: $workflow_name ($current_state -> $desired_state)"
    changed=$((changed + 1))
    return 0
  fi

  if gh_api_with_retry -X PUT \
    "repos/$organization/$repository/actions/workflows/$workflow_id/$action" >/dev/null; then
    echo "  ✓ $organization/$repository :: $workflow_name -> $desired_state"
    changed=$((changed + 1))
  else
    echo "  ✗ $organization/$repository :: failed to set $workflow_name to $desired_state" >&2
    failures=$((failures + 1))
  fi
}

cancel_run() {
  local repository="$1"
  local workflow_name="$2"
  local run_id="$3"

  if [ "$apply" = false ]; then
    echo "  ~ $organization/$repository :: cancel run $run_id ($workflow_name)"
    cancelled=$((cancelled + 1))
  elif gh_api_with_retry -X POST \
    "repos/$organization/$repository/actions/runs/$run_id/cancel" >/dev/null; then
    echo "  ✓ $organization/$repository :: canceled run $run_id ($workflow_name)"
    cancelled=$((cancelled + 1))
  else
    echo "  ✗ $organization/$repository :: failed to cancel run $run_id ($workflow_name)" >&2
    failures=$((failures + 1))
  fi
}

cancel_controlled_runs() {
  local record
  local repository
  local workflow_id
  local workflow_name
  local runs_pages
  local run_id

  while IFS= read -r record; do
    repository=$(jq -r '.repository' <<< "$record")
    workflow_id=$(jq -r '.id' <<< "$record")
    workflow_name=$(jq -r '.name' <<< "$record")

    if [ "$apply" = false ]; then
      while IFS= read -r run_id; do
        [ -n "$run_id" ] || continue
        cancel_run "$repository" "$workflow_name" "$run_id"
      done < <(jq -r '.active_runs[].id' <<< "$record")
      continue
    fi

    # Refresh after all workflow disables. This catches runs that started between
    # the preflight inventory and the mutation phase.
    if ! runs_pages=$(list_run_pages "$repository" "$workflow_id"); then
      echo "  ✗ $organization/$repository :: failed to refresh runs for $workflow_name" >&2
      failures=$((failures + 1))
      continue
    fi
    while IFS= read -r run_id; do
      [ -n "$run_id" ] || continue
      cancel_run "$repository" "$workflow_name" "$run_id"
    done < <(jq -r '.[]?.workflow_runs[]? | select(.status != "completed") | .id' <<< "$runs_pages" | sort -u)
  done < <(jq -c '.workflows[]' "$inventory_file")
}

wait_for_quiescence() {
  local timeout_seconds="${AI_CORPUS_QUIESCENCE_TIMEOUT_SECONDS:-300}"
  local interval_seconds="${AI_CORPUS_QUIESCENCE_POLL_SECONDS:-5}"
  local deadline
  local record
  local repository
  local workflow_id
  local workflow_name
  local runs_pages
  local active_count
  local total_active

  [[ "$timeout_seconds" =~ ^[0-9]+$ ]] || timeout_seconds=300
  [[ "$interval_seconds" =~ ^[0-9]+$ ]] || interval_seconds=5
  [ "$interval_seconds" -gt 0 ] || interval_seconds=1
  deadline=$((SECONDS + timeout_seconds))

  echo "Verifying quiescence (timeout: ${timeout_seconds}s)..."
  while true; do
    total_active=0
    while IFS= read -r record; do
      repository=$(jq -r '.repository' <<< "$record")
      workflow_id=$(jq -r '.id' <<< "$record")
      workflow_name=$(jq -r '.name' <<< "$record")
      if ! runs_pages=$(list_run_pages "$repository" "$workflow_id"); then
        echo "  ✗ failed to verify runs for $organization/$repository :: $workflow_name" >&2
        failures=$((failures + 1))
        return 1
      fi
      if ! active_count=$(jq -e '[.[]?.workflow_runs[]? | select(.status != "completed")] | length' \
        <<< "$runs_pages"); then
        echo "  ✗ malformed run response while verifying $organization/$repository :: $workflow_name" >&2
        failures=$((failures + 1))
        return 1
      fi
      total_active=$((total_active + active_count))
    done < <(jq -c '.workflows[]' "$inventory_file")

    if [ "$total_active" -eq 0 ]; then
      echo "  ✓ no queued or running controlled workflows remain"
      return 0
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "  ✗ $total_active controlled run(s) remain after ${timeout_seconds}s" >&2
      failures=$((failures + 1))
      return 1
    fi
    echo "  … waiting for $total_active controlled run(s) to finish cancellation"
    sleep "$interval_seconds"
  done
}

write_snapshot() {
  local parent_directory
  local temporary_snapshot

  [ -n "$snapshot_file" ] || return 0
  if [ -e "$snapshot_file" ]; then
    echo "Snapshot path already exists; refusing to overwrite it: $snapshot_file" >&2
    return 1
  fi
  parent_directory=$(dirname "$snapshot_file")
  if [ ! -d "$parent_directory" ]; then
    echo "Snapshot parent directory does not exist: $parent_directory" >&2
    return 1
  fi

  temporary_snapshot=$(mktemp "$parent_directory/.ai-corpus-snapshot.XXXXXX")
  umask 077
  jq --arg created_at "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" '
    {
      schema_version: 1,
      created_at: $created_at,
      organization,
      source_repository,
      workflows_repository,
      repositories,
      workflows: [
        .workflows[]
        | {
            repository,
            id,
            name,
            path,
            original_state: .state
          }
      ]
    }' "$inventory_file" > "$temporary_snapshot"
  mv "$temporary_snapshot" "$snapshot_file"
  echo "  ✓ wrote pre-mutation snapshot to $snapshot_file"
}

validate_snapshot() {
  local current_keys="$temporary_directory/current-keys.json"
  local snapshot_keys="$temporary_directory/snapshot-keys.json"
  local drift

  [ -f "$snapshot_file" ] || { echo "Snapshot file not found: $snapshot_file" >&2; return 1; }
  if ! jq -e '
      .schema_version == 1 and
      (.created_at | type == "string" and length > 0) and
      (.organization | type == "string") and
      (.source_repository | type == "string") and
      (.workflows_repository | type == "string") and
      (.repositories | type == "array") and
      (.workflows | type == "array") and
      all(.workflows[];
        (.repository | type == "string") and
        (.id | type == "number") and
        (.path | type == "string") and
        (.original_state | type == "string"))' "$snapshot_file" >/dev/null; then
    echo "Snapshot is malformed or uses an unsupported schema: $snapshot_file" >&2
    return 1
  fi

  if ! jq -e \
    --arg organization "$organization" \
    --arg source "$source_repository" \
    --arg workflows "$workflows_repository" '
      .organization == $organization and
      .source_repository == $source and
      .workflows_repository == $workflows' "$snapshot_file" >/dev/null; then
    echo "Snapshot metadata does not match the requested organization/repositories" >&2
    return 1
  fi

  if ! diff -u \
    <(jq -S '.repositories | unique | sort' "$snapshot_file") \
    <(jq -S '.repositories | unique | sort' "$inventory_file") >/dev/null; then
    echo "Snapshot repository coverage is stale or incomplete; refusing restore" >&2
    return 1
  fi

  jq -S '[.workflows[] | [.repository, .path, .id]] | sort' "$inventory_file" > "$current_keys"
  jq -S '[.workflows[] | [.repository, .path, .id]] | sort' "$snapshot_file" > "$snapshot_keys"
  if ! cmp -s "$current_keys" "$snapshot_keys"; then
    echo "Snapshot workflow identities do not exactly match the current topology; refusing restore" >&2
    return 1
  fi

  if [ "$(jq '[.workflows[] | [.repository, .path]] | length' "$snapshot_file")" -ne \
       "$(jq '[.workflows[] | [.repository, .path]] | unique | length' "$snapshot_file")" ]; then
    echo "Snapshot contains duplicate workflow paths; refusing restore" >&2
    return 1
  fi

  drift=$(jq -n -r --slurpfile snapshot "$snapshot_file" --slurpfile current "$inventory_file" '
    [ $snapshot[0].workflows[] as $saved
      | $current[0].workflows[]
      | select(.repository == $saved.repository and .path == $saved.path and .id == $saved.id)
      | select(
          if $saved.original_state == "active"
          then (.state != "active" and .state != "disabled_manually")
          else .state != $saved.original_state
          end)
      | "\(.repository) :: \(.path) (saved=\($saved.original_state), current=\(.state))"
    ] | .[]')
  if [ -n "$drift" ]; then
    echo "Workflow state drift is incompatible with this snapshot; refusing restore:" >&2
    printf '%s\n' "$drift" | sed 's/^/  /' >&2
    return 1
  fi
  if [ "$(jq '[.workflows[].active_runs[]] | length' "$inventory_file")" -ne 0 ]; then
    echo "Controlled runs are still queued or running; refusing restore until the organization is quiescent" >&2
    return 1
  fi

  echo "  ✓ snapshot schema, metadata, coverage, identities, state drift, and quiescence are valid"
}

status() {
  local repository
  local count
  local record
  local active
  local disabled
  local other
  local active_runs

  printf '\n%-36s %-22s %s\n' "REPOSITORY" "STATE" "WORKFLOW"
  while IFS= read -r repository; do
    count=$(jq --arg repository "$repository" \
      '[.workflows[] | select(.repository == $repository)] | length' "$inventory_file")
    if [ "$count" -eq 0 ]; then
      printf '%-36s %-22s %s\n' "$organization/$repository" "-" "no controlled workflows installed"
      continue
    fi
    while IFS= read -r record; do
      printf '%-36s %-22s %s (%s)\n' \
        "$organization/$repository" \
        "$(jq -r '.state' <<< "$record")" \
        "$(jq -r '.name' <<< "$record")" \
        "$(jq -r '.path' <<< "$record")"
    done < <(jq -c --arg repository "$repository" \
      '.workflows[] | select(.repository == $repository)' "$inventory_file")
  done < <(jq -r '.repositories[]' "$inventory_file")

  active=$(jq '[.workflows[] | select(.state == "active")] | length' "$inventory_file")
  disabled=$(jq '[.workflows[] | select(.state == "disabled_manually")] | length' "$inventory_file")
  other=$(jq '[.workflows[] | select(.state != "active" and .state != "disabled_manually")] | length' "$inventory_file")
  active_runs=$(jq '[.workflows[].active_runs[]] | length' "$inventory_file")
  echo
  echo "Controlled workflows: active=$active disabled_manually=$disabled other=$other"
  echo "Queued/running controlled runs: $active_runs"
}

verify_frozen() {
  local active_records
  local active_runs

  require_central_workflows || { failures=$((failures + 1)); return 0; }
  active_records=$(jq '[.workflows[] | select(.state == "active")] | length' "$inventory_file")
  active_runs=$(jq '[.workflows[].active_runs[]] | length' "$inventory_file")
  if [ "$active_records" -ne 0 ]; then
    echo "  ✗ freeze verification failed: $active_records controlled workflow(s) are active" >&2
    jq -r '.workflows[] | select(.state == "active") | "    \(.repository) :: \(.path)"' \
      "$inventory_file" >&2
    failures=$((failures + 1))
  fi
  if [ "$active_runs" -ne 0 ]; then
    echo "  ✗ freeze verification failed: $active_runs controlled run(s) are queued or running" >&2
    jq -r '.workflows[] as $workflow | .active_runs[] | "    \($workflow.repository) :: run \(.id) (\(.status))"' \
      "$inventory_file" >&2
    failures=$((failures + 1))
  fi
  if [ "$active_records" -eq 0 ] && [ "$active_runs" -eq 0 ]; then
    echo "  ✓ organization is frozen: all controlled workflows are inactive and no controlled runs are active"
  fi
}

disable_all_and_cancel() {
  local bootstrap_record
  local record

  bootstrap_record=$(workflow_record "$workflows_repository" "$bootstrap_path")
  set_record_state "$bootstrap_record" "disabled_manually"

  while IFS= read -r record; do
    if [ "$(jq -r '.path' <<< "$record")" = "$bootstrap_path" ]; then
      continue
    fi
    set_record_state "$record" "disabled_manually"
  done < <(jq -c '.workflows[]' "$inventory_file")

  cancel_controlled_runs
  if [ "$apply" = true ]; then
    wait_for_quiescence
  fi
}

freeze() {
  echo
  echo "Freezing AI corpus propagation and synchronization..."
  require_central_workflows || { failures=$((failures + 1)); return 0; }
  require_repository "$source_repository" || { failures=$((failures + 1)); return 0; }

  if [ "$apply" = true ]; then
    write_snapshot || { failures=$((failures + 1)); return 0; }
  elif [ -n "$snapshot_file" ]; then
    echo "  ~ would write the complete pre-mutation snapshot to $snapshot_file"
  else
    echo "  - no snapshot path supplied; --snapshot is mandatory when applying"
  fi

  disable_all_and_cancel
}

restore() {
  local saved
  local repository
  local workflow_path
  local record

  echo
  echo "Restoring workflow activation state from $snapshot_file..."
  validate_snapshot || { failures=$((failures + 1)); return 0; }

  while IFS= read -r saved; do
    repository=$(jq -r '.repository' <<< "$saved")
    workflow_path=$(jq -r '.path' <<< "$saved")
    record=$(workflow_record "$repository" "$workflow_path")
    set_record_state "$record" "active"
  done < <(jq -c '.workflows[] | select(.original_state == "active")' "$snapshot_file")

  echo "  Workflows recorded as inactive were left unchanged. Canceled runs and corpus content are not restored."
}

quiesce_for_topology_change() {
  echo "Quiescing all controlled workflows before enabling the requested topology..."
  disable_all_and_cancel
  [ "$failures" -eq 0 ]
}

enable_after_quiescence() {
  local repository="$1"
  local workflow_path="$2"
  local record

  record=$(workflow_record "$repository" "$workflow_path")
  set_record_state "$record" "active" "disabled_manually"
}

enable_canary() {
  local default_branch
  local sync_record
  local sync_id

  [ "$canary_repository" != "$workflows_repository" ] || {
    echo "The reusable-workflows repository cannot be the canary" >&2
    exit 2
  }

  echo
  echo "Preparing isolated PR-based synchronization canary for $organization/$canary_repository..."
  require_central_workflows || { failures=$((failures + 1)); return 0; }
  require_repository "$source_repository" || { failures=$((failures + 1)); return 0; }
  require_repository "$canary_repository" || { failures=$((failures + 1)); return 0; }
  require_workflow "$canary_repository" "$sync_path" || { failures=$((failures + 1)); return 0; }

  if ! default_branch=$(gh_api_with_retry "repos/$organization/$canary_repository" --jq '.default_branch'); then
    echo "Failed to resolve the canary repository's default branch" >&2
    failures=$((failures + 1))
    return 0
  fi
  if [ -z "$default_branch" ] || [ "$default_branch" = "null" ]; then
    echo "The canary repository has no default branch" >&2
    failures=$((failures + 1))
    return 0
  fi

  quiesce_for_topology_change || return 0
  enable_after_quiescence "$workflows_repository" "$sync_path"
  enable_after_quiescence "$canary_repository" "$sync_path"
  [ "$failures" -eq 0 ] || return 0

  sync_record=$(workflow_record "$canary_repository" "$sync_path")
  sync_id=$(jq -r '.id' <<< "$sync_record")
  if [ "$apply" = false ]; then
    echo "  ~ dispatch Sync Copilot Instructions in $organization/$canary_repository from $organization/$source_repository"
  elif GH_API_RETRY_MODE=never gh_api_with_retry -X POST \
    "repos/$organization/$canary_repository/actions/workflows/$sync_id/dispatches" \
    -f "ref=$default_branch" \
    -f "inputs[source_repository]=$organization/$source_repository" >/dev/null; then
    echo "  ✓ dispatched canary sync; inspect its run and pull request before enabling propagation"
  else
    echo "  ✗ failed to dispatch canary sync" >&2
    failures=$((failures + 1))
  fi
}

enable_single_source() {
  local record
  local repository
  local workflow_path

  echo
  echo "Converging on a single authoritative propagation source..."
  require_central_workflows || { failures=$((failures + 1)); return 0; }
  require_repository "$source_repository" || { failures=$((failures + 1)); return 0; }
  require_workflow "$source_repository" "$propagate_path" || { failures=$((failures + 1)); return 0; }

  quiesce_for_topology_change || return 0
  while IFS= read -r record; do
    repository=$(jq -r '.repository' <<< "$record")
    workflow_path=$(jq -r '.path' <<< "$record")
    if [ "$workflow_path" = "$sync_path" ] ||
       { [ "$workflow_path" = "$propagate_path" ] &&
         { [ "$repository" = "$source_repository" ] || [ "$repository" = "$workflows_repository" ]; }; }; then
      set_record_state "$record" "active" "disabled_manually"
    fi
  done < <(jq -c '.workflows[]' "$inventory_file")

  echo
  echo "No fan-out was dispatched automatically. After verification, run:"
  echo "  gh workflow run propagate-copilot-instructions.yml --repo $organization/$source_repository"
}

enable_legacy_all_to_all() {
  local record

  echo
  echo "WARNING: restoring the legacy multi-source all-to-all topology."
  require_central_workflows || { failures=$((failures + 1)); return 0; }
  quiesce_for_topology_change || return 0
  while IFS= read -r record; do
    set_record_state "$record" "active" "disabled_manually"
  done < <(jq -c '.workflows[]' "$inventory_file")
}

if ! build_inventory; then
  failures=$((failures + 1))
else
  case "$operation" in
    status)
      status
      ;;
    verify-frozen)
      verify_frozen
      ;;
    freeze)
      freeze
      ;;
    restore)
      restore
      ;;
    canary)
      enable_canary
      ;;
    enable-single-source)
      enable_single_source
      ;;
    enable-legacy-all-to-all)
      enable_legacy_all_to_all
      ;;
  esac
fi

echo
echo "Summary: changed=$changed skipped=$skipped cancelled=$cancelled failures=$failures"
if [ "$apply" = false ] && [ "$operation" != "status" ] && [ "$operation" != "verify-frozen" ]; then
  echo "Dry run only. Re-run with --apply --confirm-organization $organization to perform GitHub changes."
fi

[ "$failures" -eq 0 ]
