#!/usr/bin/env bash
# Propagates common wrapper workflows to all Cratis repositories so that
# each repository has a standard set of reusable workflows.
#
# Currently bootstraps:
#   - cleanup-pr-artifacts.yml             — cleans up PR-published GitHub Packages
#   - update-packages.yml                  — weekly package updates (NuGet + NPM)
#   - auto-approve-publish-deployments.yml — auto-approves npm/nuget trusted publishing deployments
#   - verify-pull-request-policy.yml      — notes, drift, release intent and work records in one job;
#                                           replaces only known bootstrapped legacy wrappers atomically
#   - .github/codeql/codeql-config.yml     — shared CodeQL query filters
#
# Called by .github/workflows/bootstrap-common-workflows.yml after checkout.
#
# This script handles both initial bootstrap and ongoing updates:
#   - New repos:            adds missing wrapper workflows
#   - Already-bootstrapped: updates wrappers if their content has changed
#   - Up-to-date repos:     skips processing
#
# Rollout (Cratis/Workflows#163): a run does not start every push-triggered
# workflow in every repository at once.
#   - Canary: the first repository that needs a change, trying the repositories
#     in CANARY_PREFERENCE first, is pushed without [skip ci], once the token is
#     shown to read its Actions runs. The rollout then waits for the workflow
#     runs GitHub starts for that commit and stops (exit 1, no other repository
#     touched) if any of them does not succeed or they are not complete within
#     CANARY_TIMEOUT_SECONDS. They pass only once CANARY_SETTLE_SECONDS have
#     elapsed and two consecutive reads show the same completed runs, since
#     GitHub creates a commit's runs over several seconds. A canary that starts
#     no run within CANARY_SETTLE_SECONDS is logged and the rollout proceeds.
#   - Every later commit ends with " [skip ci]". After each push, the next
#     repository waits STAGGER_SECONDS before its HEAD is read.
#
# Expects:
#   GH_TOKEN      - PAT with repo + Workflows permissions; the PAT owner must
#                   be a bypass actor on target repos' branch protection rulesets
#                   so that direct pushes to the default branch are allowed.
#   REPOS_FILE    - path to a JSON file containing the repos array
#                   (written by the "Get all Cratis repositories" step)
#   REPOS_IGNORE  - JSON array of repo names to skip (e.g. '["Workflows"]')
#   The PAT also needs Actions: Read to list the canary's workflow runs.
#
# Optional (defaults in brackets):
#   CANARY_PREFERENCE      - repositories tried first as canary, space or comma separated [Fundamentals]
#   CANARY_SETTLE_SECONDS  - least time from the canary push to a verdict on its runs [300]
#   CANARY_TIMEOUT_SECONDS - how long the canary's runs may take to complete [2700]
#   CANARY_POLL_SECONDS    - interval between reads of the canary's runs [30]
#   STAGGER_SECONDS        - wait before reading the next repository after a push [20]
#   SLEEP_CMD              - command used to wait; tests replace it [sleep]

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

canary_preference="${CANARY_PREFERENCE:-Fundamentals}"
canary_settle_seconds="${CANARY_SETTLE_SECONDS:-300}"
canary_timeout_seconds="${CANARY_TIMEOUT_SECONDS:-2700}"
canary_poll_seconds="${CANARY_POLL_SECONDS:-30}"
stagger_seconds="${STAGGER_SECONDS:-20}"
sleep_cmd="${SLEEP_CMD:-sleep}"
for setting in "CANARY_SETTLE_SECONDS=$canary_settle_seconds" "CANARY_TIMEOUT_SECONDS=$canary_timeout_seconds" \
  "CANARY_POLL_SECONDS=$canary_poll_seconds" "STAGGER_SECONDS=$stagger_seconds"; do
  if ! [[ "${setting#*=}" =~ ^[0-9]+$ ]]; then
    echo "::error::${setting%%=*} must be a whole number of seconds, got '${setting#*=}'"
    exit 2
  fi
done
if [ "$canary_poll_seconds" -lt 1 ]; then
  echo "::error::CANARY_POLL_SECONDS must be at least 1"
  exit 2
fi

# Commits pushed so far in this run; the first one is the canary.
pushes_done=0
# Set by update_repo to the commit it pushed, empty when it pushed nothing.
pushed_sha=""
# Set after each push; the next repository waits STAGGER_SECONDS before reading its HEAD.
stagger_pending=false
# update_repo returns this when the token cannot read the canary repository's Actions runs.
ACTIONS_UNREADABLE=76

failures_file=$(mktemp)

# ================================================================
# Bootstrapped file definitions (base64-encoded)
# ================================================================
# To verify the encoded content, run:  echo "<value>" | base64 -d
#
# Each entry: path in target repo -> base64 content

declare -A BOOTSTRAPPED_FILES

# cleanup-pr-artifacts.yml — triggers on PR close, delegates to reusable workflow
# Decodes to:
#   name: Cleanup PR Artifacts
#   on:
#     pull_request:
#       types: [closed]
#   jobs:
#     cleanup:
#       uses: Cratis/Workflows/.github/workflows/cleanup-pr-artifacts.yml@main
#       with:
#         pull_request: ${{ github.event.pull_request.number }}
#       secrets: inherit
BOOTSTRAPPED_FILES[".github/workflows/cleanup-pr-artifacts.yml"]="bmFtZTogQ2xlYW51cCBQUiBBcnRpZmFjdHMKCm9uOgogIHB1bGxfcmVxdWVzdDoKICAgIHR5cGVzOiBbY2xvc2VkXQoKam9iczoKICBjbGVhbnVwOgogICAgdXNlczogQ3JhdGlzL1dvcmtmbG93cy8uZ2l0aHViL3dvcmtmbG93cy9jbGVhbnVwLXByLWFydGlmYWN0cy55bWxAbWFpbgogICAgd2l0aDoKICAgICAgcHVsbF9yZXF1ZXN0OiAke3sgZ2l0aHViLmV2ZW50LnB1bGxfcmVxdWVzdC5udW1iZXIgfX0KICAgIHNlY3JldHM6IGluaGVyaXQK"

# update-packages.yml — weekly scheduled + manual trigger, delegates to reusable workflow
# The cron placeholder is replaced per repository before creating its blob.
# Decodes to:
#   name: Update Packages
#   on:
#     schedule:
#       - cron: '__PACKAGE_UPDATE_CRON__'
#     workflow_dispatch:
#   jobs:
#     update:
#       uses: Cratis/Workflows/.github/workflows/update-packages.yml@main
#       secrets:
#         PAT_WORKFLOWS: ${{ secrets.PAT_WORKFLOWS }}
BOOTSTRAPPED_FILES[".github/workflows/update-packages.yml"]="bmFtZTogVXBkYXRlIFBhY2thZ2VzCgpvbjoKICBzY2hlZHVsZToKICAgIC0gY3JvbjogJ19fUEFDS0FHRV9VUERBVEVfQ1JPTl9fJwogIHdvcmtmbG93X2Rpc3BhdGNoOgoKam9iczoKICB1cGRhdGU6CiAgICB1c2VzOiBDcmF0aXMvV29ya2Zsb3dzLy5naXRodWIvd29ya2Zsb3dzL3VwZGF0ZS1wYWNrYWdlcy55bWxAbWFpbgogICAgc2VjcmV0czoKICAgICAgUEFUX1dPUktGTE9XUzogJHt7IHNlY3JldHMuUEFUX1dPUktGTE9XUyB9fQo="

# auto-approve-publish-deployments.yml — approves pending npm/nuget deployments
# for Publish workflow runs (trusted publishing environments).
# Decodes to:
#   name: Auto-Approve Publish Deployments
#   on:
#     workflow_run:
#       workflows: ["Publish"]
#       types: [requested]
#   permissions:
#     actions: read
#     deployments: write
#   jobs:
#     approve:
#       uses: Cratis/Workflows/.github/workflows/auto-approve-publish-deployments.yml@main
#       with:
#         workflow_run_id: ${{ github.event.workflow_run.id }}
#       secrets: inherit
BOOTSTRAPPED_FILES[".github/workflows/auto-approve-publish-deployments.yml"]="bmFtZTogQXV0by1BcHByb3ZlIFB1Ymxpc2ggRGVwbG95bWVudHMKIyBSZXVzYWJsZSB3b3JrZmxvdyB0aGF0IGFwcHJvdmVzIHBlbmRpbmcgZW52aXJvbm1lbnQgZGVwbG95bWVudHMgKG5wbS9udWdldCkKIyBmb3IgYSB3b3JrZmxvdyBydW4sIGludGVuZGVkIGZvciB0cnVzdGVkIHB1Ymxpc2hpbmcgZmxvd3MuCgpvbjoKICB3b3JrZmxvd19jYWxsOgogICAgaW5wdXRzOgogICAgICB3b3JrZmxvd19ydW5faWQ6CiAgICAgICAgZGVzY3JpcHRpb246IFdvcmtmbG93IHJ1biBpZCB0byBhcHByb3ZlIHBlbmRpbmcgZGVwbG95bWVudHMgZm9yCiAgICAgICAgcmVxdWlyZWQ6IHRydWUKICAgICAgICB0eXBlOiBudW1iZXIKCnBlcm1pc3Npb25zOgogIGFjdGlvbnM6IHJlYWQKICBkZXBsb3ltZW50czogd3JpdGUKCmpvYnM6CiAgYXBwcm92ZToKICAgIHJ1bnMtb246IHVidW50dS1sYXRlc3QKICAgIHN0ZXBzOgogICAgICAtIG5hbWU6IEFwcHJvdmUgcGVuZGluZyBucG0vbnVnZXQgZGVwbG95bWVudHMKICAgICAgICBlbnY6CiAgICAgICAgICBHSF9UT0tFTjogJHt7IGdpdGh1Yi50b2tlbiB9fQogICAgICAgICAgV09SS0ZMT1dfUlVOX0lEOiAke3sgaW5wdXRzLndvcmtmbG93X3J1bl9pZCB9fQogICAgICAgIHJ1bjogfAogICAgICAgICAgc2V0IC1ldW8gcGlwZWZhaWwKCiAgICAgICAgICBtYXhfYXR0ZW1wdHM9MTgwCiAgICAgICAgICBzbGVlcF9zZWNvbmRzPTEwCgogICAgICAgICAgZm9yICgoYXR0ZW1wdD0xOyBhdHRlbXB0PD1tYXhfYXR0ZW1wdHM7IGF0dGVtcHQrKykpOyBkbwogICAgICAgICAgICBtYXBmaWxlIC10IGVudl9pZHMgPCA8KAogICAgICAgICAgICAgIGdoIGFwaSAicmVwb3MvJHt7IGdpdGh1Yi5yZXBvc2l0b3J5IH19L2FjdGlvbnMvcnVucy8ke1dPUktGTE9XX1JVTl9JRH0vcGVuZGluZ19kZXBsb3ltZW50cyIgXAogICAgICAgICAgICAgICAgLS1qcSAnLltdIHwgc2VsZWN0KC5lbnZpcm9ubWVudC5uYW1lID09ICJucG0iIG9yIC5lbnZpcm9ubWVudC5uYW1lID09ICJudWdldCIpIHwgLmVudmlyb25tZW50LmlkJyB8CiAgICAgICAgICAgICAgICBzb3J0IC11CiAgICAgICAgICAgICkKCiAgICAgICAgICAgIGlmIFsgIiR7I2Vudl9pZHNbQF19IiAtZ3QgMCBdOyB0aGVuCiAgICAgICAgICAgICAgYXJncz0oKQogICAgICAgICAgICAgIGZvciBlbnZfaWQgaW4gIiR7ZW52X2lkc1tAXX0iOyBkbwogICAgICAgICAgICAgICAgYXJncys9KC1GICJlbnZpcm9ubWVudF9pZHNbXT0ke2Vudl9pZH0iKQogICAgICAgICAgICAgIGRvbmUKCiAgICAgICAgICAgICAgZ2ggYXBpIC1YIFBPU1QgInJlcG9zLyR7eyBnaXRodWIucmVwb3NpdG9yeSB9fS9hY3Rpb25zL3J1bnMvJHtXT1JLRkxPV19SVU5fSUR9L3BlbmRpbmdfZGVwbG95bWVudHMiIFwKICAgICAgICAgICAgICAgICIke2FyZ3NbQF19IiBcCiAgICAgICAgICAgICAgICAtZiBzdGF0ZT1hcHByb3ZlZCBcCiAgICAgICAgICAgICAgICAtZiBjb21tZW50PSdBdXRvLWFwcHJvdmVkIGZvciB0cnVzdGVkIHB1Ymxpc2hpbmcnCgogICAgICAgICAgICAgIGVjaG8gIkFwcHJvdmVkIHBlbmRpbmcgZGVwbG95bWVudHMgZm9yIGVudmlyb25tZW50czogJHtlbnZfaWRzWypdfSIKICAgICAgICAgICAgICBleGl0IDAKICAgICAgICAgICAgZmkKCiAgICAgICAgICAgIHJ1bl9zdGF0dXM9JChnaCBhcGkgInJlcG9zLyR7eyBnaXRodWIucmVwb3NpdG9yeSB9fS9hY3Rpb25zL3J1bnMvJHtXT1JLRkxPV19SVU5fSUR9IiAtLWpxICcuc3RhdHVzJykKICAgICAgICAgICAgaWYgWyAiJHJ1bl9zdGF0dXMiID0gImNvbXBsZXRlZCIgXTsgdGhlbgogICAgICAgICAgICAgIGVjaG8gIk5vIHBlbmRpbmcgbnBtL251Z2V0IGRlcGxveW1lbnRzIGZvdW5kIGZvciBydW4gJHtXT1JLRkxPV19SVU5fSUR9IgogICAgICAgICAgICAgIGV4aXQgMAogICAgICAgICAgICBmaQoKICAgICAgICAgICAgZWNobyAiQXR0ZW1wdCAke2F0dGVtcHR9LyR7bWF4X2F0dGVtcHRzfTogbm8gcGVuZGluZyBucG0vbnVnZXQgZGVwbG95bWVudHMgeWV0IGZvciBydW4gJHtXT1JLRkxPV19SVU5fSUR9OyBydW4gc3RhdHVzIGlzICR7cnVuX3N0YXR1c30uIFdhaXRpbmcgJHtzbGVlcF9zZWNvbmRzfXMuLi4iCiAgICAgICAgICAgIHNsZWVwICIkc2xlZXBfc2Vjb25kcyIKICAgICAgICAgIGRvbmUKCiAgICAgICAgICBlY2hvICI6OmVycm9yOjpUaW1lZCBvdXQgd2FpdGluZyBmb3IgcGVuZGluZyBucG0vbnVnZXQgZGVwbG95bWVudHMgZm9yIHJ1biAke1dPUktGTE9XX1JVTl9JRH0iCiAgICAgICAgICBleGl0IDEK"

# verify-no-work-records.yml — fails PRs that track AI session work records
# (plans, handovers, prompts); such files are local-only in .ai-work/
#
#   name: Verify No Work Records
#   on:
#     pull_request:
#       paths: ["**.md", ".ai-work/**"]
#     push:
#       branches: ["main"]
#       paths: ["**.md", ".ai-work/**"]
#   concurrency:
#     group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
#     cancel-in-progress: true
#   jobs:
#     verify:
#       uses: Cratis/Workflows/.github/workflows/verify-no-work-records.yml@main
BOOTSTRAPPED_FILES[".github/workflows/verify-no-work-records.yml"]="bmFtZTogVmVyaWZ5IE5vIFdvcmsgUmVjb3JkcwoKb246CiAgcHVsbF9yZXF1ZXN0OgogICAgcGF0aHM6IFsiKioubWQiLCAiLmFpLXdvcmsvKioiXQogIHB1c2g6CiAgICBicmFuY2hlczogWyJtYWluIl0KICAgIHBhdGhzOiBbIioqLm1kIiwgIi5haS13b3JrLyoqIl0KCmNvbmN1cnJlbmN5OgogIGdyb3VwOiAke3sgZ2l0aHViLndvcmtmbG93IH19LSR7eyBnaXRodWIuZXZlbnQucHVsbF9yZXF1ZXN0Lm51bWJlciB8fCBnaXRodWIucmVmIH19CiAgY2FuY2VsLWluLXByb2dyZXNzOiB0cnVlCgpqb2JzOgogIHZlcmlmeToKICAgIHVzZXM6IENyYXRpcy9Xb3JrZmxvd3MvLmdpdGh1Yi93b3JrZmxvd3MvdmVyaWZ5LW5vLXdvcmstcmVjb3Jkcy55bWxAbWFpbgo="

# verify-release-notes.yml — fails release-bound PRs whose description (published
# verbatim as the release notes) breaks the release-note contract
# Decodes to:
#   name: Verify Release Notes
#
#   # Thin caller of the organization-wide release-notes gate. This pull request's
#   # description is published verbatim as the release notes, so the gate checks it
#   # before the merge. The contract - which sections, issue references and content
#   # are allowed, and what the errors say - lives in
#   # Cratis/Workflows/.github/workflows/verify-release-notes.yml; do not reintroduce
#   # logic here. Installed and kept current by Cratis/Workflows'
#   # bootstrap-common-workflows.
#   #
#   # `edited` re-runs the check when the description changes and `labeled` and
#   # `unlabeled` when the release label does. There is no branch or label filter: the
#   # gate itself checks pull requests into the default branch, fails one that carries
#   # major, minor or patch, warns on one labelled no-release or not labelled yet, and
#   # passes Dependabot's with a notice. `pull-requests: read` lets it read the pull
#   # request as it is now, so a re-run sees the current labels and description.
#   #
#   # The job is named release-notes so the check reads `release-notes / verify` and
#   # does not collide with other `verify / verify` gates.
#   #
#   # RUNNER_GATE is an outage escape hatch and is normally unset: set it temporarily to reroute the
#   # job when the default runner is down. A private repository does not use this caller as is; it
#   # keeps its own copy with its own fallback, for example
#   # `runs-on: ${{ vars.RUNNER_GATE || 'cratis-arc' }}`.
#   concurrency:
#     group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
#     cancel-in-progress: true
#
#   on:
#     pull_request:
#       types: [opened, edited, reopened, synchronize, labeled, unlabeled, ready_for_review]
#
#   permissions:
#     contents: read
#     pull-requests: read
#
#   jobs:
#     release-notes:
#       uses: Cratis/Workflows/.github/workflows/verify-release-notes.yml@main
#       with:
#         runs-on: ${{ vars.RUNNER_GATE || 'ubuntu-latest' }}
BOOTSTRAPPED_FILES[".github/workflows/verify-release-notes.yml"]="bmFtZTogVmVyaWZ5IFJlbGVhc2UgTm90ZXMKCiMgVGhpbiBjYWxsZXIgb2YgdGhlIG9yZ2FuaXphdGlvbi13aWRlIHJlbGVhc2Utbm90ZXMgZ2F0ZS4gVGhpcyBwdWxsIHJlcXVlc3QncwojIGRlc2NyaXB0aW9uIGlzIHB1Ymxpc2hlZCB2ZXJiYXRpbSBhcyB0aGUgcmVsZWFzZSBub3Rlcywgc28gdGhlIGdhdGUgY2hlY2tzIGl0CiMgYmVmb3JlIHRoZSBtZXJnZS4gVGhlIGNvbnRyYWN0IC0gd2hpY2ggc2VjdGlvbnMsIGlzc3VlIHJlZmVyZW5jZXMgYW5kIGNvbnRlbnQKIyBhcmUgYWxsb3dlZCwgYW5kIHdoYXQgdGhlIGVycm9ycyBzYXkgLSBsaXZlcyBpbgojIENyYXRpcy9Xb3JrZmxvd3MvLmdpdGh1Yi93b3JrZmxvd3MvdmVyaWZ5LXJlbGVhc2Utbm90ZXMueW1sOyBkbyBub3QgcmVpbnRyb2R1Y2UKIyBsb2dpYyBoZXJlLiBJbnN0YWxsZWQgYW5kIGtlcHQgY3VycmVudCBieSBDcmF0aXMvV29ya2Zsb3dzJwojIGJvb3RzdHJhcC1jb21tb24td29ya2Zsb3dzLgojCiMgYGVkaXRlZGAgcmUtcnVucyB0aGUgY2hlY2sgd2hlbiB0aGUgZGVzY3JpcHRpb24gY2hhbmdlcyBhbmQgYGxhYmVsZWRgIGFuZAojIGB1bmxhYmVsZWRgIHdoZW4gdGhlIHJlbGVhc2UgbGFiZWwgZG9lcy4gVGhlcmUgaXMgbm8gYnJhbmNoIG9yIGxhYmVsIGZpbHRlcjogdGhlCiMgZ2F0ZSBpdHNlbGYgY2hlY2tzIHB1bGwgcmVxdWVzdHMgaW50byB0aGUgZGVmYXVsdCBicmFuY2gsIGZhaWxzIG9uZSB0aGF0IGNhcnJpZXMKIyBtYWpvciwgbWlub3Igb3IgcGF0Y2gsIHdhcm5zIG9uIG9uZSBsYWJlbGxlZCBuby1yZWxlYXNlIG9yIG5vdCBsYWJlbGxlZCB5ZXQsIGFuZAojIHBhc3NlcyBEZXBlbmRhYm90J3Mgd2l0aCBhIG5vdGljZS4gYHB1bGwtcmVxdWVzdHM6IHJlYWRgIGxldHMgaXQgcmVhZCB0aGUgcHVsbAojIHJlcXVlc3QgYXMgaXQgaXMgbm93LCBzbyBhIHJlLXJ1biBzZWVzIHRoZSBjdXJyZW50IGxhYmVscyBhbmQgZGVzY3JpcHRpb24uCiMKIyBUaGUgam9iIGlzIG5hbWVkIHJlbGVhc2Utbm90ZXMgc28gdGhlIGNoZWNrIHJlYWRzIGByZWxlYXNlLW5vdGVzIC8gdmVyaWZ5YCBhbmQKIyBkb2VzIG5vdCBjb2xsaWRlIHdpdGggb3RoZXIgYHZlcmlmeSAvIHZlcmlmeWAgZ2F0ZXMuCiMKIyBSVU5ORVJfR0FURSBpcyBhbiBvdXRhZ2UgZXNjYXBlIGhhdGNoIGFuZCBpcyBub3JtYWxseSB1bnNldDogc2V0IGl0IHRlbXBvcmFyaWx5IHRvIHJlcm91dGUgdGhlCiMgam9iIHdoZW4gdGhlIGRlZmF1bHQgcnVubmVyIGlzIGRvd24uIEEgcHJpdmF0ZSByZXBvc2l0b3J5IGRvZXMgbm90IHVzZSB0aGlzIGNhbGxlciBhcyBpczsgaXQKIyBrZWVwcyBpdHMgb3duIGNvcHkgd2l0aCBpdHMgb3duIGZhbGxiYWNrLCBmb3IgZXhhbXBsZQojIGBydW5zLW9uOiAke3sgdmFycy5SVU5ORVJfR0FURSB8fCAnY3JhdGlzLWFyYycgfX1gLgpjb25jdXJyZW5jeToKICBncm91cDogJHt7IGdpdGh1Yi53b3JrZmxvdyB9fS0ke3sgZ2l0aHViLmV2ZW50LnB1bGxfcmVxdWVzdC5udW1iZXIgfHwgZ2l0aHViLnJlZiB9fQogIGNhbmNlbC1pbi1wcm9ncmVzczogdHJ1ZQoKb246CiAgcHVsbF9yZXF1ZXN0OgogICAgdHlwZXM6IFtvcGVuZWQsIGVkaXRlZCwgcmVvcGVuZWQsIHN5bmNocm9uaXplLCBsYWJlbGVkLCB1bmxhYmVsZWQsIHJlYWR5X2Zvcl9yZXZpZXddCgpwZXJtaXNzaW9uczoKICBjb250ZW50czogcmVhZAogIHB1bGwtcmVxdWVzdHM6IHJlYWQKCmpvYnM6CiAgcmVsZWFzZS1ub3RlczoKICAgIHVzZXM6IENyYXRpcy9Xb3JrZmxvd3MvLmdpdGh1Yi93b3JrZmxvd3MvdmVyaWZ5LXJlbGVhc2Utbm90ZXMueW1sQG1haW4KICAgIHdpdGg6CiAgICAgIHJ1bnMtb246ICR7eyB2YXJzLlJVTk5FUl9HQVRFIHx8ICd1YnVudHUtbGF0ZXN0JyB9fQo="

# verify-semver-label.yml — requires exactly one of major, minor, patch or no-release,
# and only no-release on a Dependabot pull request (whose labels it corrects first).
# Installed only where the repository releases (see releases_with_release_action).
# Decodes to:
#   name: Verify Semver Label
#
#   # Thin caller of the organization-wide release-intent gate. The policy - which
#   # labels are accepted and what the errors say - lives in
#   # Cratis/Workflows/.github/workflows/verify-release-intent.yml. Thirty diverging
#   # per-repository copies of that logic are how the 2026-08-25 unintended releases
#   # happened; do not reintroduce logic here. Installed and kept current by
#   # Cratis/Workflows' bootstrap-common-workflows.
#   #
#   # A Dependabot pull request first has its labels corrected: Dependabot adds major,
#   # minor or patch on its own, and a Dependabot pull request only ever carries
#   # no-release. The gate then reads the labels as they are now, so it sees the
#   # correction although a label change made with GITHUB_TOKEN starts no new run.
#   #
#   # The job is named release-intent so the check reads `release-intent / verify` and
#   # does not collide with `verify / verify` from verify-no-work-records.
#   concurrency:
#     group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
#     cancel-in-progress: true
#
#   on:
#     pull_request:
#       types: [opened, reopened, synchronize, labeled, unlabeled]
#       # Scoped to the same branch Publish releases from. A pull request stacked onto another one's branch cannot
#       # cut a release, so demanding a version label of it would be asking which version a merge that publishes
#       # nothing should carry.
#       branches:
#         - main
#
#   permissions:
#     contents: read
#
#   jobs:
#     dependabot-labels:
#       if: github.event.pull_request.user.login == 'dependabot[bot]'
#       uses: Cratis/Workflows/.github/workflows/normalize-dependabot-labels.yml@main
#       permissions:
#         pull-requests: write
#
#     release-intent:
#       needs: dependabot-labels
#       # Also after a failed or skipped correction: the gate then reports the labels as they are.
#       if: ${{ !cancelled() }}
#       uses: Cratis/Workflows/.github/workflows/verify-release-intent.yml@main
#       permissions:
#         contents: read
#         pull-requests: read
BOOTSTRAPPED_FILES[".github/workflows/verify-semver-label.yml"]="bmFtZTogVmVyaWZ5IFNlbXZlciBMYWJlbAoKIyBUaGluIGNhbGxlciBvZiB0aGUgb3JnYW5pemF0aW9uLXdpZGUgcmVsZWFzZS1pbnRlbnQgZ2F0ZS4gVGhlIHBvbGljeSAtIHdoaWNoCiMgbGFiZWxzIGFyZSBhY2NlcHRlZCBhbmQgd2hhdCB0aGUgZXJyb3JzIHNheSAtIGxpdmVzIGluCiMgQ3JhdGlzL1dvcmtmbG93cy8uZ2l0aHViL3dvcmtmbG93cy92ZXJpZnktcmVsZWFzZS1pbnRlbnQueW1sLiBUaGlydHkgZGl2ZXJnaW5nCiMgcGVyLXJlcG9zaXRvcnkgY29waWVzIG9mIHRoYXQgbG9naWMgYXJlIGhvdyB0aGUgMjAyNi0wOC0yNSB1bmludGVuZGVkIHJlbGVhc2VzCiMgaGFwcGVuZWQ7IGRvIG5vdCByZWludHJvZHVjZSBsb2dpYyBoZXJlLiBJbnN0YWxsZWQgYW5kIGtlcHQgY3VycmVudCBieQojIENyYXRpcy9Xb3JrZmxvd3MnIGJvb3RzdHJhcC1jb21tb24td29ya2Zsb3dzLgojCiMgQSBEZXBlbmRhYm90IHB1bGwgcmVxdWVzdCBmaXJzdCBoYXMgaXRzIGxhYmVscyBjb3JyZWN0ZWQ6IERlcGVuZGFib3QgYWRkcyBtYWpvciwKIyBtaW5vciBvciBwYXRjaCBvbiBpdHMgb3duLCBhbmQgYSBEZXBlbmRhYm90IHB1bGwgcmVxdWVzdCBvbmx5IGV2ZXIgY2FycmllcwojIG5vLXJlbGVhc2UuIFRoZSBnYXRlIHRoZW4gcmVhZHMgdGhlIGxhYmVscyBhcyB0aGV5IGFyZSBub3csIHNvIGl0IHNlZXMgdGhlCiMgY29ycmVjdGlvbiBhbHRob3VnaCBhIGxhYmVsIGNoYW5nZSBtYWRlIHdpdGggR0lUSFVCX1RPS0VOIHN0YXJ0cyBubyBuZXcgcnVuLgojCiMgVGhlIGpvYiBpcyBuYW1lZCByZWxlYXNlLWludGVudCBzbyB0aGUgY2hlY2sgcmVhZHMgYHJlbGVhc2UtaW50ZW50IC8gdmVyaWZ5YCBhbmQKIyBkb2VzIG5vdCBjb2xsaWRlIHdpdGggYHZlcmlmeSAvIHZlcmlmeWAgZnJvbSB2ZXJpZnktbm8td29yay1yZWNvcmRzLgpjb25jdXJyZW5jeToKICBncm91cDogJHt7IGdpdGh1Yi53b3JrZmxvdyB9fS0ke3sgZ2l0aHViLmV2ZW50LnB1bGxfcmVxdWVzdC5udW1iZXIgfHwgZ2l0aHViLnJlZiB9fQogIGNhbmNlbC1pbi1wcm9ncmVzczogdHJ1ZQoKb246CiAgcHVsbF9yZXF1ZXN0OgogICAgdHlwZXM6IFtvcGVuZWQsIHJlb3BlbmVkLCBzeW5jaHJvbml6ZSwgbGFiZWxlZCwgdW5sYWJlbGVkXQogICAgIyBTY29wZWQgdG8gdGhlIHNhbWUgYnJhbmNoIFB1Ymxpc2ggcmVsZWFzZXMgZnJvbS4gQSBwdWxsIHJlcXVlc3Qgc3RhY2tlZCBvbnRvIGFub3RoZXIgb25lJ3MgYnJhbmNoIGNhbm5vdAogICAgIyBjdXQgYSByZWxlYXNlLCBzbyBkZW1hbmRpbmcgYSB2ZXJzaW9uIGxhYmVsIG9mIGl0IHdvdWxkIGJlIGFza2luZyB3aGljaCB2ZXJzaW9uIGEgbWVyZ2UgdGhhdCBwdWJsaXNoZXMKICAgICMgbm90aGluZyBzaG91bGQgY2FycnkuCiAgICBicmFuY2hlczoKICAgICAgLSBtYWluCgpwZXJtaXNzaW9uczoKICBjb250ZW50czogcmVhZAoKam9iczoKICBkZXBlbmRhYm90LWxhYmVsczoKICAgIGlmOiBnaXRodWIuZXZlbnQucHVsbF9yZXF1ZXN0LnVzZXIubG9naW4gPT0gJ2RlcGVuZGFib3RbYm90XScKICAgIHVzZXM6IENyYXRpcy9Xb3JrZmxvd3MvLmdpdGh1Yi93b3JrZmxvd3Mvbm9ybWFsaXplLWRlcGVuZGFib3QtbGFiZWxzLnltbEBtYWluCiAgICBwZXJtaXNzaW9uczoKICAgICAgcHVsbC1yZXF1ZXN0czogd3JpdGUKCiAgcmVsZWFzZS1pbnRlbnQ6CiAgICBuZWVkczogZGVwZW5kYWJvdC1sYWJlbHMKICAgICMgQWxzbyBhZnRlciBhIGZhaWxlZCBvciBza2lwcGVkIGNvcnJlY3Rpb246IHRoZSBnYXRlIHRoZW4gcmVwb3J0cyB0aGUgbGFiZWxzIGFzIHRoZXkgYXJlLgogICAgaWY6ICR7eyAhY2FuY2VsbGVkKCkgfX0KICAgIHVzZXM6IENyYXRpcy9Xb3JrZmxvd3MvLmdpdGh1Yi93b3JrZmxvd3MvdmVyaWZ5LXJlbGVhc2UtaW50ZW50LnltbEBtYWluCiAgICBwZXJtaXNzaW9uczoKICAgICAgY29udGVudHM6IHJlYWQKICAgICAgcHVsbC1yZXF1ZXN0czogcmVhZAo="

# Legacy payloads above are retained as migration fixtures, not installed anymore.
# One caller now supplies the policy / verify context. Tokens below are rendered
# per repository; only releasing repositories get the separate write-scoped job.
BOOTSTRAPPED_FILES[".github/workflows/verify-pull-request-policy.yml"]="bmFtZTogVmVyaWZ5IFB1bGwgUmVxdWVzdCBQb2xpY3kKCm9uOgogIHB1bGxfcmVxdWVzdDoKICAgIHR5cGVzOiBbb3BlbmVkLCBlZGl0ZWQsIHJlb3BlbmVkLCBzeW5jaHJvbml6ZSwgbGFiZWxlZCwgdW5sYWJlbGVkLCByZWFkeV9mb3JfcmV2aWV3XQogIHB1c2g6CiAgICBicmFuY2hlczogW21haW5dCgpjb25jdXJyZW5jeToKICBncm91cDogJHt7IGdpdGh1Yi53b3JrZmxvdyB9fS0ke3sgZ2l0aHViLmV2ZW50LnB1bGxfcmVxdWVzdC5udW1iZXIgfHwgZ2l0aHViLnJlZiB9fQogIGNhbmNlbC1pbi1wcm9ncmVzczogdHJ1ZQoKcGVybWlzc2lvbnM6CiAgY29udGVudHM6IHJlYWQKICBwdWxsLXJlcXVlc3RzOiByZWFkCgpqb2JzOgpfX0RFUEVOREFCT1RfSk9CX18gIHBvbGljeToKX19QT0xJQ1lfTkVFRFNfXyAgICB1c2VzOiBDcmF0aXMvV29ya2Zsb3dzLy5naXRodWIvd29ya2Zsb3dzL3ZlcmlmeS1wdWxsLXJlcXVlc3QtcG9saWN5LnltbEBtYWluCiAgICB3aXRoOgogICAgICBydW5zLW9uOiAke3sgdmFycy5SVU5ORVJfR0FURSB8fCAnX19QT0xJQ1lfUlVOTkVSX18nIH19CiAgICAgIHJlbGVhc2UtaW50ZW50OiBfX1JFTEVBU0VfSU5URU5UX18K"

# Blob ids of every historical bootstrap payload for each path (git log -p of
# this script). Never accept an arbitrary wrapper just because its name matches.
declare -A LEGACY_WRAPPER_HASHES
LEGACY_WRAPPER_HASHES[".github/workflows/verify-no-work-records.yml"]="7c8cd3960a171a8a5d230cc307a5d51bd267f9d7 fd2934b809b5a306f1fecb37cb47c37768534cdc"
LEGACY_WRAPPER_HASHES[".github/workflows/verify-release-notes.yml"]="3af453d97af21253079bc6f2ad244166584c7630 e68b9b6f9a60f2d2f2b3e8f144e3fdce4048d40f"
LEGACY_WRAPPER_HASHES[".github/workflows/verify-semver-label.yml"]="5c60360adffde449eba3dcee57e49d1aea6573c6"
for legacy_path in "${!LEGACY_WRAPPER_HASHES[@]}"; do
  unset 'BOOTSTRAPPED_FILES['"$legacy_path"']'
done

# $3 is the default runner: GitHub-hosted for public repositories, whose pull_request jobs must never
# reach self-hosted runners (any fork could run code there), and cratis-arc-small for private ones.
# RUNNER_GATE overrides it per repository or organization.
render_policy_wrapper() {
  local file_content="$1" releases="$2" runner="$3" normalizer="" needs="" normalizer_with=""
  # Bash 5.2+ expands an unquoted & in a substitution replacement to the match; the
  # fragments contain &&. Quoting the replacement instead is not portable: Bash 3.2
  # keeps the quotes literally.
  shopt -u patsub_replacement 2>/dev/null || true
  if [ "$runner" != ubuntu-latest ]; then
    normalizer_with='    with:
      runs-on: ${{ vars.RUNNER_GATE || '\''__POLICY_RUNNER__'\'' }}
'
  fi
  if [ "$releases" = true ]; then
    normalizer='  dependabot-labels:
    if: github.event.pull_request.user.login == '\''dependabot[bot]'\'' && github.event.pull_request.base.ref == '\''main'\''
    uses: Cratis/Workflows/.github/workflows/normalize-dependabot-labels.yml@main
'"$normalizer_with"'    permissions:
      pull-requests: write

'
    needs='    needs: dependabot-labels
    if: ${{ !cancelled() }}
'
  fi
  file_content="${file_content/__DEPENDABOT_JOB__/$normalizer}"
  file_content="${file_content/__POLICY_NEEDS__/$needs}"
  file_content="${file_content/__RELEASE_INTENT__/$releases}"
  printf '%s\n' "${file_content//__POLICY_RUNNER__/$runner}"
}

# .github/codeql/codeql-config.yml — shared CodeQL configuration
# Decodes to:
#   name: "Cratis CodeQL config"
#
#   query-filters:
#     # CA1031 is intentionally excluded from the shared baseline.
#     - exclude:
#         id: ca1031
BOOTSTRAPPED_FILES[".github/codeql/codeql-config.yml"]="bmFtZTogIkNyYXRpcyBDb2RlUUwgY29uZmlnIgoKcXVlcnktZmlsdGVyczoKICAjIENBMTAzMSBpcyBpbnRlbnRpb25hbGx5IGV4Y2x1ZGVkIGZyb20gdGhlIHNoYXJlZCBiYXNlbGluZS4KICAtIGV4Y2x1ZGU6CiAgICAgIGlkOiBjYTEwMzEK"

# Stable POSIX checksum of the repository name: Monday, minute 1-59, 03:00-07:59 UTC.
# Unlike an array index, this does not move existing schedules when repositories are added.
package_update_cron() {
  local checksum remainder
  read -r checksum remainder < <(printf '%s' "$1" | cksum)
  printf '%d %d * * 1' "$((checksum % 59 + 1))" "$((checksum / 59 % 5 + 3))"
}

# ================================================================
# Per-file skips
# ================================================================
# A repository that keeps its own, deliberately different copy of one bootstrapped
# file is listed here for that file only; it still receives every other file.
# Unlike REPOS_IGNORE, which skips a repository entirely.
#
# Each entry: path in target repo -> space-separated repository names
#
#   Arc.Kotlin - its verify-semver-label.yml pins verify-release-intent.yml to a full
#                SHA and uses a literal concurrency-group prefix, each explained in
#                that file; the @main caller would undo both

declare -A SKIP_FILE_REPOS
SKIP_FILE_REPOS[".github/workflows/verify-semver-label.yml"]="Arc.Kotlin"

# Whether $2 keeps its own copy of the bootstrapped file $1.
skips_file() {
  local file_path="$1" repo="$2" skipped
  for skipped in ${SKIP_FILE_REPOS[$file_path]:-}; do
    [ "$skipped" = "$repo" ] && return 0
  done
  return 1
}

# ================================================================
# Release-intent caller: only where a repository releases
# ================================================================
# verify-semver-label.yml demands one of major/minor/patch/no-release on every pull request. That is only
# meaningful where merging can cut a release, so a repository that never releases (a blog, a samples or workshop
# repository, a Homebrew tap) must not get a gate that turns every one of its pull requests red.

SEMVER_LABEL_FILE=".github/workflows/verify-semver-label.yml"

# Whether repository $1 releases with Cratis/release-action: it already has the release-intent caller, or one of
# its workflows uses Cratis/release-action (the owner is matched case-insensitively). $2 is the repository's
# recursive tree (JSON). A repository that starts releasing is picked up on the next run, without a list to
# maintain.
releases_with_release_action() {
  local repo="$1" tree="$2" blob_sha content encoded
  local policy_file=".github/workflows/verify-pull-request-policy.yml"
  if echo "$tree" | jq -e --arg path "$SEMVER_LABEL_FILE" '.tree[] | select(.path == $path)' >/dev/null 2>&1; then
    return 0
  fi
  while read -r blob_sha; do
    [ -n "$blob_sha" ] || continue
    if ! encoded=$(gh api "repos/Cratis/$repo/git/blobs/$blob_sha" --jq '.content' 2>/dev/null) \
      || ! content=$(printf '%s' "$encoded" | base64 -d 2>/dev/null); then
      echo "  Could not discover release configuration in Cratis/$repo (blob $blob_sha); skipping repo" >&2
      return 2
    fi
    if grep -qi 'Cratis/release-action' <<<"$content"; then
      return 0
    fi
    if echo "$tree" | jq -e --arg path "$policy_file" --arg sha "$blob_sha" \
      '.tree[] | select(.path == $path and .sha == $sha)' >/dev/null \
      && grep -Eq '^[[:space:]]+release-intent:[[:space:]]+true([[:space:]]*(#.*)?)?$' <<<"$content"; then
      return 0
    fi
  done < <(echo "$tree" | jq -r '.tree[] | select(.type == "blob" and (.path | test("^[.]github/workflows/[^/]+[.]ya?ml$"))) | .sha')
  return 1
}

# ================================================================
# Rollout: canary first, then staggered [skip ci] commits (Cratis/Workflows#163)
# ================================================================

# The repositories in processing order: those named in CANARY_PREFERENCE (in that order) first, then the rest in
# their listed order. The first repository that pushes a commit is the canary, so it is the first preferred
# repository that needs a change, or else the first repository that needs one.
rollout_order() {
  echo "$repos" | jq -r --arg preference "$canary_preference" '
    . as $all
    | reduce ($preference | splits("[\\s,]+") | select(length > 0)) as $name ([];
        if ($all | index($name)) != null and index($name) == null then . + [$name] else . end)
    | . as $first
    | ($first + ($all | map(select(. as $name | $first | index($name) == null))))[]'
}

# Reads a workflow-runs response on stdin and prints none, pending, success or failure. A completed run counts as
# a success only with conclusion success, skipped or neutral; anything else fails the canary.
canary_runs_state() {
  jq -r '
    (.workflow_runs | if type == "array" then . else error("no workflow_runs array") end) as $runs
    | def ok: .status == "completed" and ((.conclusion // "") as $c | ["success", "skipped", "neutral"] | index($c) != null);
    if ($runs | length) == 0 then "none"
    elif any($runs[]; .status == "completed" and (ok | not)) then "failure"
    elif all($runs[]; ok) then "success"
    else "pending" end'
}

# Reads a workflow-runs response on stdin and prints a stable fingerprint of its runs and their outcomes.
canary_runs_fingerprint() {
  jq -c '[.workflow_runs[] | [(.id // .name // ""), .status, (.conclusion // "")]] | sort'
}

# Waits for the workflow runs GitHub starts for the canary commit $2 in repository $1. Returns 0 when they all
# succeeded or none started within CANARY_SETTLE_SECONDS, 1 when one failed, they did not complete within
# CANARY_TIMEOUT_SECONDS, or the runs could not be read three times in a row. GitHub creates a commit's runs over
# several seconds, so success also needs CANARY_SETTLE_SECONDS elapsed and the same completed runs on two
# consecutive reads. Elapsed time is counted in poll intervals, so the wait is deterministic when SLEEP_CMD is
# replaced.
wait_for_canary() {
  local repo="$1" sha="$2" elapsed=0 read_errors=0 response state runs_error fingerprint previous=""
  echo "  Waiting for the workflow runs of canary Cratis/$repo at $sha..."
  while true; do
    runs_error=$(mktemp)
    state=""
    if response=$(gh api "repos/Cratis/$repo/actions/runs?head_sha=$sha&per_page=100" 2>"$runs_error") \
      && state=$(printf '%s' "$response" | canary_runs_state 2>>"$runs_error"); then
      read_errors=0
    else
      read_errors=$((read_errors + 1))
      echo "  ⚠ Could not read the workflow runs of Cratis/$repo (attempt $read_errors of 3): $(cat "$runs_error" 2>/dev/null || true)"
    fi
    rm -f "$runs_error"
    fingerprint=""
    case "$state" in
      success)
        # A run GitHub has not created yet would be missed by trusting the first all-green read.
        fingerprint=$(printf '%s' "$response" | canary_runs_fingerprint)
        if [ "$elapsed" -lt "$canary_settle_seconds" ] || [ "$fingerprint" != "$previous" ]; then
          echo "  Canary Cratis/$repo: runs completed so far succeeded; confirming after ${elapsed}s"
          previous="$fingerprint"
        else
        echo "  ✓ Canary Cratis/$repo: $(printf '%s' "$response" | jq '.workflow_runs | length') workflow run(s) succeeded"
        return 0
        fi
        ;;
      failure)
        echo "::error::Canary Cratis/$repo: a workflow run for $sha did not succeed; stopping the rollout"
        printf '%s' "$response" | jq -r '.workflow_runs[] | "    \(.name // "?"): \(.status) \(.conclusion // "") \(.html_url // "")"'
        return 1
        ;;
      none)
        if [ "$elapsed" -ge "$canary_settle_seconds" ]; then
          echo "  ℹ Canary Cratis/$repo started no workflow runs within ${canary_settle_seconds}s; proceeding"
          return 0
        fi
        ;;
    esac
    [ "$state" = success ] || previous=""
    if [ "$read_errors" -ge 3 ]; then
      echo "::error::Could not read the workflow runs of canary Cratis/$repo; stopping the rollout"
      return 1
    fi
    if [ "$elapsed" -ge "$canary_timeout_seconds" ]; then
      echo "::error::Canary Cratis/$repo: workflow runs for $sha not complete after ${canary_timeout_seconds}s; stopping the rollout"
      return 1
    fi
    "$sleep_cmd" "$canary_poll_seconds"
    elapsed=$((elapsed + canary_poll_seconds))
  done
}

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
    echo "  • Permissions → Workflows: Read and write"
    echo "Update the PAT at: https://github.com/settings/personal-access-tokens"
    exit 1
  fi
  echo "✓ PAT has write access to Cratis/$probe_repo (pre-flight check passed)"
fi

update_repo() {
  local repo="$1" attempt="$2"
  pushed_sha=""
  # Skip repos in the ignore list
  if echo "$repos_ignore" | jq -e --arg r "$repo" 'index($r) != null' >/dev/null 2>&1; then
    echo "Skipping $repo (in ignore list)"
    return 0
  fi

  echo "Processing Cratis/$repo..."

  # Space out the pushes: after each push, wait before reading the next repository's HEAD, so the wait does not
  # widen the window between reading HEAD and updating the ref. A retry of the same repository already waited.
  if [ "$stagger_pending" = true ] && [ "$stagger_seconds" -gt 0 ]; then
    echo "  Waiting ${stagger_seconds}s after the previous push (stagger)"
    "$sleep_cmd" "$stagger_seconds"
  fi
  stagger_pending=false

  # ----------------------------------------------------------------
  # 1. Get default branch and HEAD SHA
  # ----------------------------------------------------------------
  repo_info_error=$(mktemp)
  default_branch=$(gh api "repos/Cratis/$repo" \
    --jq '.default_branch' 2>"$repo_info_error" || true)
  if [ -z "$default_branch" ]; then
    repo_info_api_error=$(cat "$repo_info_error" 2>/dev/null || true)
    echo "  ⚠ Could not get default branch for $repo, skipping"
    [ -n "$repo_info_api_error" ] && echo "    API error: $repo_info_api_error"
    rm -f "$repo_info_error"
    return 1
  fi
  rm -f "$repo_info_error"
  # Unknown visibility must never fall back to a self-hosted runner for a public repository.
  repo_private=$(gh api "repos/Cratis/$repo" --jq '.private' 2>/dev/null || true)
  case "$repo_private" in
    true) repo_runner=cratis-arc-small ;;
    false) repo_runner=ubuntu-latest ;;
    *)
      echo "  ⚠ Could not determine the visibility of $repo, skipping"
      return 1
      ;;
  esac

  head_sha_error=$(mktemp)
  _head_sha_resp=$(gh api "repos/Cratis/$repo/git/ref/heads/$default_branch" \
    2>"$head_sha_error" || true)
  head_sha=$(extract_sha "$_head_sha_resp" '.object.sha')
  if [ -z "$head_sha" ]; then
    head_sha_api_error=$(cat "$head_sha_error" 2>/dev/null || true)
    echo "  ⚠ Could not get HEAD SHA for $repo ($default_branch branch not found), skipping"
    [ -n "$head_sha_api_error" ] && echo "    API error: $head_sha_api_error"
    rm -f "$head_sha_error"
    return 1
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
    return 1
  fi
  rm -f "$tree_sha_error"

  # ----------------------------------------------------------------
  # 3. Get the full recursive tree to check existing files
  # ----------------------------------------------------------------
  subtree_error=$(mktemp)
  subtree=$(gh api "repos/Cratis/$repo/git/trees/$tree_sha?recursive=1" \
    2>"$subtree_error" || true)
  if ! echo "$subtree" | jq -e '.truncated != true and (.tree | type == "array")' >/dev/null 2>&1; then
    subtree_api_error=$(cat "$subtree_error" 2>/dev/null || true)
    echo "  ⚠ Could not get tree for $repo, skipping"
    [ -n "$subtree_api_error" ] && echo "    API error: $subtree_api_error"
    rm -f "$subtree_error"
    return 1
  fi
  rm -f "$subtree_error"

  # ----------------------------------------------------------------
  # 4. Create blobs and build tree entries for all wrapper workflows
  # ----------------------------------------------------------------
  tree_entries="[]"
  has_changes=false
  commit_parts=()

  repo_releases=false
  discovery=0
  releases_with_release_action "$repo" "$subtree" || discovery=$?
  if [ "$discovery" -eq 0 ]; then repo_releases=true
  elif [ "$discovery" -ne 1 ]; then return 1
  fi
  # Arc.Kotlin retains its deliberately pinned semver wrapper, not a second gate.
  if skips_file "$SEMVER_LABEL_FILE" "$repo"; then repo_releases=false; fi
  for file_path in "${!BOOTSTRAPPED_FILES[@]}"; do
    if skips_file "$file_path" "$repo"; then
      echo "  ℹ Skipping $file_path (Cratis/$repo keeps its own copy)"
      continue
    fi
    file_b64="${BOOTSTRAPPED_FILES[$file_path]}"
    if [ "$file_path" = ".github/workflows/verify-pull-request-policy.yml" ]; then
      file_content=$(printf '%s' "$file_b64" | base64 -d)
      file_b64=$(render_policy_wrapper "$file_content" "$repo_releases" "$repo_runner" | base64 | tr -d '\n')
    fi
    if [ "$file_path" = ".github/workflows/update-packages.yml" ]; then
      file_content=$(printf '%s' "$file_b64" | base64 -d)
      file_content="${file_content/__PACKAGE_UPDATE_CRON__/$(package_update_cron "$repo")}"
      file_b64=$(printf '%s\n' "$file_content" | base64 | tr -d '\n')
    fi
    case "$file_path" in
      .github/codeql/codeql-config.yml)
        file_label="codeql-config"
        ;;
      *.yml)
        file_label=$(basename "$file_path" .yml)
        ;;
      *)
        file_label=$(basename "$file_path")
        ;;
    esac

    # Create blob
    blob_error=$(mktemp)
    _blob_resp=$(gh api -X POST "repos/Cratis/$repo/git/blobs" \
      -f content="$file_b64" -f encoding=base64 \
      2>"$blob_error" || true)
    blob_sha=$(extract_sha "$_blob_resp")

    if [ -z "$blob_sha" ]; then
      blob_err=$(cat "$blob_error" 2>/dev/null || true)
      echo "  ⚠ Could not create blob for $file_label in $repo"
      [ -n "$blob_err" ] && echo "    blob error: $blob_err"
      rm -f "$blob_error"
      return 1  # No tree/ref update, so no legacy wrapper can be deleted.
    fi
    rm -f "$blob_error"

    # Check if file already matches
    existing_sha=$(echo "$subtree" | jq -r \
      --arg path "$file_path" \
      '.tree[] | select(.path == $path) | .sha' \
      2>/dev/null || true)

    if [ "$existing_sha" = "$blob_sha" ]; then
      echo "  ℹ $file_label already up-to-date"
      continue
    fi

    has_changes=true
    if [ -n "$existing_sha" ]; then
      commit_parts+=("update $file_label")
    else
      commit_parts+=("add $file_label")
    fi

    # Add tree entry
    tree_entries=$(echo "$tree_entries" | jq \
      --arg path "$file_path" \
      --arg sha  "$blob_sha" \
      '. + [{path: $path, mode: "100644", type: "blob", sha: $sha}]')
  done

  # Add the replacement and deletions to ONE tree. This is reached only after
  # every replacement blob was created successfully, including the policy blob.
  for file_path in "${!LEGACY_WRAPPER_HASHES[@]}"; do
    if skips_file "$file_path" "$repo"; then continue; fi
    existing_sha=$(echo "$subtree" | jq -r --arg path "$file_path" '.tree[] | select(.path == $path) | .sha')
    [ -n "$existing_sha" ] || continue
    if [[ " ${LEGACY_WRAPPER_HASHES[$file_path]} " != *" $existing_sha "* ]]; then
      echo "  Keeping customized $file_path in Cratis/$repo"
      continue
    fi
    tree_entries=$(echo "$tree_entries" | jq --arg path "$file_path" \
      '. + [{path: $path, mode: "100644", type: "blob", sha: null}]')
    has_changes=true
    commit_parts+=("remove $(basename "$file_path" .yml)")
  done

  if [ "$has_changes" != "true" ]; then
    echo "  ℹ No changes needed for $repo"
    return 0
  fi

  # The canary is useless if its runs cannot be followed: prove the token can list this repository's Actions
  # runs before writing anything to it.
  if [ "$pushes_done" -eq 0 ]; then
    actions_error=$(mktemp)
    if ! gh api "repos/Cratis/$repo/actions/runs?per_page=1" >/dev/null 2>"$actions_error"; then
      echo "::error::PAT_WORKFLOWS cannot read Actions runs in canary Cratis/$repo; it needs the Actions: Read permission. Nothing was pushed."
      echo "    API error: $(cat "$actions_error" 2>/dev/null || true)"
      rm -f "$actions_error"
      return "$ACTIONS_UNREADABLE"
    fi
    rm -f "$actions_error"
  fi

  # ----------------------------------------------------------------
  # 5. Create the new tree object
  # ----------------------------------------------------------------
  new_tree_json=$(jq -n \
    --arg base_tree "$tree_sha" \
    --argjson tree "$tree_entries" \
    '{base_tree: $base_tree, tree: $tree}')

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
    return 1
  fi
  rm -f "$tree_error"

  # ----------------------------------------------------------------
  # 6. Create the commit
  # ----------------------------------------------------------------
  # Join commit parts: "Add/Update cleanup-pr-artifacts, add update-packages"
  commit_message=$(IFS=', '; echo "Bootstrap common workflows: ${commit_parts[*]}")
  commit_message="${commit_message^}"  # Capitalize first letter
  # Exception to .cratis/ai/rules/github-actions.md ("Do not add [skip ci] to bot commits as a load
  # optimization"), approved for Cratis/Workflows#163: these commits touch only bootstrapped wrapper files, and
  # the canary - the first commit of the run - keeps its CI run, which must succeed before any other repository
  # is changed. Without it one run starts every push-triggered workflow in every repository at once.
  if [ "$pushes_done" -gt 0 ]; then
    commit_message="$commit_message [skip ci]"
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
    return 1
  fi
  rm -f "$commit_error"

  # ----------------------------------------------------------------
  # 7. Push commit directly to the default branch
  # ----------------------------------------------------------------
  push_error=$(mktemp)
  push_result=$(gh api -X PATCH "repos/Cratis/$repo/git/refs/heads/$default_branch" \
    -f sha="$new_commit_sha" \
    -F force=false \
    2>"$push_error" || true)
  updated_sha=$(extract_sha "$push_result" '.object.sha')

  if [ -z "$updated_sha" ]; then
    push_api_error=$(cat "$push_error" 2>/dev/null || true)
    push_msg=$(echo "$push_result" | jq -r '.message // empty' 2>/dev/null || true)
    echo "  ⚠ Could not push commit to $default_branch in $repo"
    [ -n "$push_api_error" ] && echo "    API error: $push_api_error"
    [ -n "$push_msg" ]       && echo "    GitHub message: $push_msg"
    rm -f "$push_error"
    if [ "$attempt" -eq 0 ] && grep -Eqi 'not a fast forward|not fast forward|non-fast-forward' <<<"$push_api_error $push_msg"; then
      echo "  Branch moved; retrying once from fresh HEAD"
      return 75
    fi
    return 1
  fi
  rm -f "$push_error"
  pushed_sha="$updated_sha"

  echo "  ✓ Pushed to $default_branch in $repo: $commit_message"
}

# The loop runs in this shell (not a pipeline subshell) so the push count and the canary outcome survive it.
rollout=$(rollout_order)
mapfile -t ordered_repos <<<"$rollout"
repos_processed=0
canary_failed=""
actions_unreadable=""
for repo in "${ordered_repos[@]}"; do
  [ -n "$repo" ] || continue
  repos_processed=$((repos_processed + 1))
  result=0
  update_repo "$repo" 0 || result=$?
  if [ "$result" -eq 75 ]; then
    result=0
    update_repo "$repo" 1 || result=$?
  fi
  if [ "$result" -eq "$ACTIONS_UNREADABLE" ]; then
    echo "$repo" >> "$failures_file"
    actions_unreadable="$repo"
    break
  fi
  if [ "$result" -ne 0 ]; then echo "$repo" >> "$failures_file"; continue; fi
  [ -n "$pushed_sha" ] || continue
  pushes_done=$((pushes_done + 1))
  stagger_pending=true
  if [ "$pushes_done" -eq 1 ] && ! wait_for_canary "$repo" "$pushed_sha"; then
    echo "$repo" >> "$failures_file"
    canary_failed="$repo"
    break
  fi
done

total_failures=$(wc -l < "$failures_file" 2>/dev/null || echo "0")
rm -f "$failures_file"

echo ""
echo "Processed $repos_processed repo(s), pushed $pushes_done commit(s)"
echo "Summary: $total_failures failure(s)"

if [ -n "$actions_unreadable" ]; then
  echo "::error::Stopped before the canary Cratis/$actions_unreadable: grant PAT_WORKFLOWS Actions: Read. No repository was changed."
  exit 1
fi
if [ -n "$canary_failed" ]; then
  echo "::error::Canary Cratis/$canary_failed did not pass; no other repository was changed after it."
  exit 1
fi

if [ "$total_failures" -gt 0 ]; then
  echo "::error::$total_failures repo(s) failed. Check the log above for details."
  exit 1
fi
