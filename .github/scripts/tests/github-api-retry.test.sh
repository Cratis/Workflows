#!/usr/bin/env bash
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

set -euo pipefail

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_directory=$(mktemp -d)
mock_bin="$test_directory/bin"
attempt_file="$test_directory/attempts"
passed=0
failed=0

cleanup() {
  rm -rf "$test_directory"
}
trap cleanup EXIT
mkdir -p "$mock_bin"

cat > "$mock_bin/gh" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
count=0
[ ! -f "$MOCK_ATTEMPT_FILE" ] || count=$(cat "$MOCK_ATTEMPT_FILE")
count=$((count + 1))
printf '%s' "$count" > "$MOCK_ATTEMPT_FILE"
if [ "$count" -lt "${MOCK_SUCCEED_ON:-2}" ]; then
  echo "HTTP 503 Service Unavailable" >&2
  exit 1
fi
echo '{"ok":true}'
MOCK
chmod +x "$mock_bin/gh"

# shellcheck disable=SC1091
source "$script_directory/github-api-retry.sh"
sleep() { :; }

assert_equal() {
  local name="$1"
  local expected="$2"
  local actual="$3"
  if [ "$actual" = "$expected" ]; then
    echo "ok - $name"
    passed=$((passed + 1))
  else
    echo "not ok - $name (expected '$expected', got '$actual')" >&2
    failed=$((failed + 1))
  fi
}

rm -f "$attempt_file"
output=$(PATH="$mock_bin:$PATH" \
  MOCK_ATTEMPT_FILE="$attempt_file" \
  MOCK_SUCCEED_ON=2 \
  GH_API_MAX_ATTEMPTS=3 \
  GH_API_BASE_DELAY_SECONDS=0 \
  GH_API_MAX_JITTER_SECONDS=0 \
  gh_api_with_retry repos/Cratis/AI)
assert_equal "transient 5xx response is retried" "2" "$(cat "$attempt_file")"
assert_equal "successful retry preserves stdout" '{"ok":true}' "$output"

rm -f "$attempt_file"
set +e
PATH="$mock_bin:$PATH" \
  MOCK_ATTEMPT_FILE="$attempt_file" \
  MOCK_SUCCEED_ON=2 \
  GH_API_MAX_ATTEMPTS=3 \
  GH_API_RETRY_MODE=never \
  gh_api_with_retry -X POST repos/Cratis/Arc/actions/workflows/202/dispatches >/dev/null 2>&1
status=$?
set -e
assert_equal "retry mode never returns the first failure" "1" "$status"
assert_equal "retry mode never prevents duplicate non-idempotent requests" "1" "$(cat "$attempt_file")"

echo
echo "$passed passed, $failed failed"
[ "$failed" -eq 0 ]
