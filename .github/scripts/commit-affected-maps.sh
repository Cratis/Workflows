#!/usr/bin/env bash
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
#
# Commits the coverage maps a merge-time run wrote, and pushes them to the default branch.
#
# - Nothing changed: nothing is committed.
# - The branch has moved past the commit that was tested: nothing is committed; the newer run updates the maps.
# - The push is rejected because the branch moved meanwhile: fetch, merge, push again. Never rebase, amend or force-push.
# - No `[skip ci]`: the downstream run is the only check that sees the merged result.
# - A map that could not be committed is never fatal. A missing or stale map makes the selector run more tests, not fewer.
#
# Environment:
#   BRANCH        the branch to push to (required)
#   TESTED_SHA    the commit that was tested (required)
#   MAPS_PATH     the folder holding the maps (default .cratis/affected/maps)
#   REMOTE        the remote name (default origin)
#   MAX_ATTEMPTS  how many times to try the push (default 3)
set -euo pipefail

: "${BRANCH:?BRANCH is required}"
: "${TESTED_SHA:?TESTED_SHA is required}"
maps_path="${MAPS_PATH:-.cratis/affected/maps}"
remote="${REMOTE:-origin}"
max_attempts="${MAX_ATTEMPTS:-3}"

# With no maps folder, and none tracked, there is nothing to add; `git add` would fail on the missing pathspec.
if [ -e "$maps_path" ] || [ -n "$(git ls-files -- "$maps_path")" ]; then
  git add --all -- "$maps_path"
fi
if git diff --cached --quiet -- "$maps_path"; then
  echo "Coverage maps are unchanged; nothing to commit."
  exit 0
fi

git fetch --quiet --no-tags "$remote" "$BRANCH"
tip="$(git rev-parse FETCH_HEAD)"
if [ "$tip" != "$TESTED_SHA" ]; then
  echo "::notice title=affected-maps::$BRANCH moved past the tested commit ($tip is not $TESTED_SHA); the newer run will update the maps."
  git reset --quiet -- "$maps_path"
  exit 0
fi

git -c user.name="github-actions[bot]" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
  commit --quiet -m "Update affected-tests coverage maps" -m "Built from ${TESTED_SHA}."

for attempt in $(seq 1 "$max_attempts"); do
  if git push --quiet "$remote" "HEAD:refs/heads/$BRANCH"; then
    echo "Pushed the coverage maps to $BRANCH (attempt $attempt)."
    exit 0
  fi
  echo "Push rejected on attempt $attempt; merging $remote/$BRANCH and trying again."
  git fetch --quiet --no-tags "$remote" "$BRANCH"
  git -c user.name="github-actions[bot]" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
    merge --quiet --no-edit FETCH_HEAD
done

echo "::warning title=affected-maps::Could not push the coverage maps after $max_attempts attempts; the next merge run will update them."
exit 0
