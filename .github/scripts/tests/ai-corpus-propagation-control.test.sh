#!/usr/bin/env bash
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
control_script="$repository_root/.github/scripts/ai-corpus-propagation-control.sh"
test_directory=$(mktemp -d)
mock_bin="$test_directory/bin"
mutation_log="$test_directory/mutations.log"
api_log="$test_directory/api.log"
passed=0
failed=0
last_output=""
last_status=0

cleanup() {
  rm -rf "$test_directory"
}
trap cleanup EXIT

mkdir -p "$mock_bin"
: > "$mutation_log"
: > "$api_log"

cat > "$mock_bin/gh" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail

[ "${1:-}" = "api" ] || { echo "unexpected gh command: $*" >&2; exit 1; }
shift
printf '%q ' "$@" >> "$MOCK_API_LOG"
printf '\n' >> "$MOCK_API_LOG"

method="GET"
endpoint=""
previous=""
for argument in "$@"; do
  if [ "$previous" = "-X" ]; then
    method="$argument"
  fi
  case "$argument" in
    orgs/*|repos/*) endpoint="$argument" ;;
  esac
  previous="$argument"
done

if [ "${MOCK_FAIL_ENDPOINT:-}" = "$endpoint" ]; then
  echo "mock API failure for $endpoint" >&2
  exit 1
fi

if [ "$method" != "GET" ]; then
  printf '%s %s\n' "$method" "$endpoint" >> "$MOCK_MUTATION_LOG"
  if [[ "$endpoint" == */cancel ]]; then
    touch "$MOCK_STATE_DIRECTORY/cancelled"
  fi
  printf '{}\n'
  exit 0
fi

case "$endpoint" in
  "orgs/Cratis/repos?per_page=100&type=all")
    cat <<'JSON'
[[{"name":"AI","archived":false},{"name":"Arc","archived":false}],[{"name":"Archived","archived":true},{"name":"Workflows","archived":false}]]
JSON
    ;;
  "repos/Cratis/AI/actions/workflows?per_page=100")
    state="active"
    sync_state="active"
    if [[ "${MOCK_SCENARIO:-active}" == frozen* ]]; then state="disabled_manually"; sync_state="disabled_manually"; fi
    if [[ "${MOCK_SCENARIO:-active}" == *mixed ]]; then sync_state="disabled_inactivity"; fi
    printf '[{"workflows":[{"id":101,"name":"Propagate Copilot Instructions","path":".github/workflows/propagate-copilot-instructions.yml","state":"%s"}]},{"workflows":[{"id":102,"name":"Sync Copilot Instructions","path":".github/workflows/sync-copilot-instructions.yml","state":"%s"}]}]\n' "$state" "$sync_state"
    ;;
  "repos/Cratis/Arc/actions/workflows?per_page=100")
    state="active"
    if [[ "${MOCK_SCENARIO:-active}" == frozen* ]]; then state="disabled_manually"; fi
    printf '[{"workflows":[{"id":201,"name":"Propagate Copilot Instructions","path":".github/workflows/propagate-copilot-instructions.yml","state":"%s"},{"id":202,"name":"Sync Copilot Instructions","path":".github/workflows/sync-copilot-instructions.yml","state":"%s"}]}]\n' "$state" "$state"
    ;;
  "repos/Cratis/Workflows/actions/workflows?per_page=100")
    state="active"
    if [[ "${MOCK_SCENARIO:-active}" == frozen* ]]; then state="disabled_manually"; fi
    if [ "${MOCK_SCENARIO:-active}" = "missing-central" ]; then
      printf '[{"workflows":[{"id":301,"name":"Propagate Copilot Instructions","path":".github/workflows/propagate-copilot-instructions.yml","state":"%s"},{"id":302,"name":"Sync Copilot Instructions","path":".github/workflows/sync-copilot-instructions.yml","state":"%s"}]}]\n' "$state" "$state"
    else
      printf '[{"workflows":[{"id":301,"name":"Propagate Copilot Instructions","path":".github/workflows/propagate-copilot-instructions.yml","state":"%s"},{"id":302,"name":"Sync Copilot Instructions","path":".github/workflows/sync-copilot-instructions.yml","state":"%s"},{"id":303,"name":"Bootstrap Copilot Sync","path":".github/workflows/bootstrap-copilot-sync.yml","state":"%s"}]}]\n' "$state" "$state" "$state"
    fi
    ;;
  repos/Cratis/*/actions/workflows/*/runs\?per_page=100)
    workflow_id=$(printf '%s' "$endpoint" | awk -F/ '{print $(NF-1)}')
    if [ "${MOCK_SCENARIO:-active}" = "active" ] && [ "$workflow_id" = "101" ] && [ ! -f "$MOCK_STATE_DIRECTORY/cancelled" ]; then
      printf '[{"workflow_runs":[{"id":9001,"status":"in_progress"}]}]\n'
    else
      printf '[{"workflow_runs":[]}]\n'
    fi
    ;;
  "repos/Cratis/Arc")
    printf 'main\n'
    ;;
  *)
    echo "unhandled mock endpoint: $endpoint" >&2
    exit 1
    ;;
esac
MOCK
chmod +x "$mock_bin/gh"

run_control() {
  local scenario="$1"
  shift
  local output_file="$test_directory/output"

  : > "$mutation_log"
  : > "$api_log"
  rm -f "$test_directory/cancelled"
  set +e
  PATH="$mock_bin:$PATH" \
    MOCK_SCENARIO="$scenario" \
    MOCK_MUTATION_LOG="$mutation_log" \
    MOCK_API_LOG="$api_log" \
    MOCK_STATE_DIRECTORY="$test_directory" \
    GH_API_MAX_ATTEMPTS=1 \
    AI_CORPUS_QUIESCENCE_TIMEOUT_SECONDS=2 \
    AI_CORPUS_QUIESCENCE_POLL_SECONDS=1 \
    "$control_script" "$@" > "$output_file" 2>&1
  last_status=$?
  set -e
  last_output=$(cat "$output_file")
}

pass() {
  echo "ok - $1"
  passed=$((passed + 1))
}

fail() {
  echo "not ok - $1" >&2
  printf '%s\n' "$last_output" | sed 's/^/  /' >&2
  failed=$((failed + 1))
}

assert_success() {
  local name="$1"
  if [ "$last_status" -eq 0 ]; then pass "$name"; else fail "$name"; fi
}

assert_failure() {
  local name="$1"
  if [ "$last_status" -ne 0 ]; then pass "$name"; else fail "$name"; fi
}

assert_contains() {
  local name="$1"
  local expected="$2"
  if grep -Fq "$expected" <<< "$last_output"; then pass "$name"; else fail "$name"; fi
}

assert_mutation_count() {
  local name="$1"
  local expected="$2"
  local actual
  actual=$(wc -l < "$mutation_log" | tr -d ' ')
  if [ "$actual" -eq "$expected" ]; then
    pass "$name"
  else
    echo "expected $expected mutations, got $actual" >> "$test_directory/output"
    last_output=$(cat "$test_directory/output")
    fail "$name"
  fi
}

assert_first_mutation_contains() {
  local name="$1"
  local expected="$2"
  local first_mutation
  first_mutation=$(head -n 1 "$mutation_log")
  if grep -Fq "$expected" <<< "$first_mutation"; then pass "$name"; else fail "$name"; fi
}

snapshot="$test_directory/freeze-snapshot.json"

run_control active freeze --snapshot "$snapshot"
assert_success "freeze dry-run succeeds"
assert_contains "freeze dry-run plans snapshot" "would write the complete pre-mutation snapshot"
assert_mutation_count "freeze dry-run performs no mutations" 0
if [ ! -e "$snapshot" ]; then pass "freeze dry-run does not write snapshot"; else fail "freeze dry-run does not write snapshot"; fi

run_control active freeze --apply --snapshot "$snapshot"
assert_failure "applied freeze requires typed organization confirmation"
assert_contains "confirmation failure is explicit" "requires --confirm-organization Cratis"
assert_mutation_count "unconfirmed freeze performs no mutations" 0

run_control active freeze --apply --confirm-organization Cratis --snapshot "$snapshot"
assert_success "applied freeze succeeds"
assert_contains "applied freeze verifies quiescence" "no queued or running controlled workflows remain"
assert_mutation_count "applied freeze disables seven workflows and cancels one run" 8
assert_first_mutation_contains "freeze disables central bootstrap first" "workflows/303/disable"
if jq -e '.schema_version == 1 and (.workflows | length == 7) and all(.workflows[]; .original_state == "active")' "$snapshot" >/dev/null; then
  pass "freeze snapshot is complete and versioned"
else
  fail "freeze snapshot is complete and versioned"
fi

run_control active restore --snapshot "$snapshot" --apply --confirm-organization Cratis
assert_failure "restore refuses while a controlled run is active"
assert_contains "restore quiescence refusal is explicit" "refusing restore until the organization is quiescent"
assert_mutation_count "non-quiescent restore performs no mutations" 0

run_control frozen restore --snapshot "$snapshot" --apply --confirm-organization Cratis
assert_success "snapshot restore succeeds"
assert_mutation_count "restore enables only seven originally active workflows" 7

mixed_snapshot="$test_directory/mixed-snapshot.json"
run_control active-mixed freeze --snapshot "$mixed_snapshot" --apply --confirm-organization Cratis
assert_success "freeze preserves a pre-existing inactive workflow state"
assert_mutation_count "freeze disables only workflows that were active" 6
if jq -e '[.workflows[] | select(.id == 102 and .original_state == "disabled_inactivity")] | length == 1' "$mixed_snapshot" >/dev/null; then
  pass "snapshot records pre-existing inactive state"
else
  fail "snapshot records pre-existing inactive state"
fi
run_control frozen-mixed restore --snapshot "$mixed_snapshot" --apply --confirm-organization Cratis
assert_success "restore accepts unchanged pre-existing inactive state"
assert_mutation_count "restore leaves pre-existing inactive workflow untouched" 6

run_control frozen verify-frozen
assert_success "verify-frozen accepts a quiescent organization"
assert_contains "verify-frozen reports success" "organization is frozen"

run_control active verify-frozen
assert_failure "verify-frozen rejects active workflow state"
assert_contains "verify-frozen reports active workflows" "controlled workflow(s) are active"

run_control active canary --repo Arc
assert_success "canary dry-run preflights complete topology"
assert_contains "canary dry-run plans isolated dispatch" "dispatch Sync Copilot Instructions"
assert_mutation_count "canary dry-run performs no mutations" 0

run_control active canary --repo Arc --apply --confirm-organization Cratis
assert_success "applied canary quiesces before dispatch"
assert_mutation_count "canary disables seven, cancels one, enables two, and dispatches once" 11
assert_first_mutation_contains "canary disables central bootstrap first" "workflows/303/disable"

run_control missing-central freeze --snapshot "$test_directory/missing.json"
assert_failure "freeze refuses an incomplete central topology"
assert_contains "missing central workflow is reported" "Required workflow is missing"
assert_mutation_count "failed preflight performs no mutations" 0

run_control frozen status
assert_success "status succeeds"
assert_contains "status counts all controlled workflows" "Controlled workflows: active=0 disabled_manually=7 other=0"
assert_contains "status reports repositories without omissions" "Queued/running controlled runs: 0"

stale_snapshot="$test_directory/stale.json"
jq '.workflows |= map(select(.repository != "Arc"))' "$snapshot" > "$stale_snapshot"
run_control frozen restore --snapshot "$stale_snapshot" --apply --confirm-organization Cratis
assert_failure "restore rejects incomplete snapshot coverage"
assert_contains "stale snapshot refusal is explicit" "do not exactly match the current topology"
assert_mutation_count "invalid restore performs no mutations" 0

echo
echo "$passed passed, $failed failed"
[ "$failed" -eq 0 ]
