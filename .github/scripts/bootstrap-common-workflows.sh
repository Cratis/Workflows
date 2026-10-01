#!/usr/bin/env bash
# Propagates common wrapper workflows to all Cratis repositories so that
# each repository has a standard set of reusable workflows.
#
# Currently bootstraps:
#   - cleanup-pr-artifacts.yml             — cleans up PR-published GitHub Packages
#   - update-packages.yml                  — weekly package updates (NuGet + NPM)
#   - auto-approve-publish-deployments.yml — auto-approves npm/nuget trusted publishing deployments
#   - verify-no-work-records.yml           — fails PRs that track AI session work records
#   - verify-release-notes.yml             — fails release-bound PRs whose description breaks the release-note contract
#   - verify-semver-label.yml              — requires exactly one release-intent label; no-release only on Dependabot PRs;
#                                            only in repositories that release with Cratis/release-action
#   - .github/codeql/codeql-config.yml     — shared CodeQL query filters
#
# Called by .github/workflows/bootstrap-common-workflows.yml after checkout.
#
# This script handles both initial bootstrap and ongoing updates:
#   - New repos:            adds missing wrapper workflows
#   - Already-bootstrapped: updates wrappers if their content has changed
#   - Up-to-date repos:     skips processing
#
# Expects:
#   GH_TOKEN      - PAT with repo + Workflows permissions; the PAT owner must
#                   be a bypass actor on target repos' branch protection rulesets
#                   so that direct pushes to the default branch are allowed.
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
#     push:
#       branches: ["main"]
#   jobs:
#     verify:
#       uses: Cratis/Workflows/.github/workflows/verify-no-work-records.yml@main
BOOTSTRAPPED_FILES[".github/workflows/verify-no-work-records.yml"]="bmFtZTogVmVyaWZ5IE5vIFdvcmsgUmVjb3JkcwoKb246CiAgcHVsbF9yZXF1ZXN0OgogIHB1c2g6CiAgICBicmFuY2hlczogWyJtYWluIl0KCmpvYnM6CiAgdmVyaWZ5OgogICAgdXNlczogQ3JhdGlzL1dvcmtmbG93cy8uZ2l0aHViL3dvcmtmbG93cy92ZXJpZnktbm8td29yay1yZWNvcmRzLnltbEBtYWluCg=="

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
  local repo="$1" tree="$2" blob_sha content
  if echo "$tree" | jq -e --arg path "$SEMVER_LABEL_FILE" '.tree[] | select(.path == $path)' >/dev/null 2>&1; then
    return 0
  fi
  while read -r blob_sha; do
    [ -n "$blob_sha" ] || continue
    content=$(gh api "repos/Cratis/$repo/git/blobs/$blob_sha" --jq '.content' 2>/dev/null | base64 -d 2>/dev/null || true)
    if grep -qi 'Cratis/release-action' <<<"$content"; then
      return 0
    fi
  done < <(echo "$tree" | jq -r '.tree[] | select(.type == "blob" and (.path | test("^[.]github/workflows/[^/]+[.]ya?ml$"))) | .sha')
  return 1
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

echo "$repos" | jq -r '.[]' | while read -r repo; do
  # Skip repos in the ignore list
  if echo "$repos_ignore" | jq -e --arg r "$repo" 'index($r) != null' >/dev/null 2>&1; then
    echo "Skipping $repo (in ignore list)"
    continue
  fi

  echo "Processing Cratis/$repo..."

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
  # 3. Get the full recursive tree to check existing files
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
  # 4. Create blobs and build tree entries for all wrapper workflows
  # ----------------------------------------------------------------
  tree_entries="[]"
  has_changes=false
  commit_parts=()

  repo_releases=""
  for file_path in "${!BOOTSTRAPPED_FILES[@]}"; do
    if skips_file "$file_path" "$repo"; then
      echo "  ℹ Skipping $file_path (Cratis/$repo keeps its own copy)"
      continue
    fi
    if [ "$file_path" = "$SEMVER_LABEL_FILE" ]; then
      if [ -z "$repo_releases" ]; then
        if releases_with_release_action "$repo" "$subtree"; then repo_releases=yes; else repo_releases=no; fi
      fi
      if [ "$repo_releases" = no ]; then
        echo "  ℹ Skipping $file_path (Cratis/$repo does not release with Cratis/release-action)"
        continue
      fi
    fi
    file_b64="${BOOTSTRAPPED_FILES[$file_path]}"
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
      echo "$repo" >> "$failures_file"
      continue 2  # skip entire repo on blob failure
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

  if [ "$has_changes" != "true" ]; then
    echo "  ℹ No changes needed for $repo"
    continue
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
    echo "$repo" >> "$failures_file"
    continue
  fi
  rm -f "$tree_error"

  # ----------------------------------------------------------------
  # 6. Create the commit
  # ----------------------------------------------------------------
  # Join commit parts: "Add/Update cleanup-pr-artifacts, add update-packages"
  commit_message=$(IFS=', '; echo "Bootstrap common workflows: ${commit_parts[*]}")
  commit_message="${commit_message^}"  # Capitalize first letter

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
    echo "$repo" >> "$failures_file"
    continue
  fi
  rm -f "$push_error"

  echo "  ✓ Pushed to $default_branch in $repo: $commit_message"
done

total_failures=$(wc -l < "$failures_file" 2>/dev/null || echo "0")
rm -f "$failures_file"

echo ""
echo "Summary: $total_failures failure(s)"

if [ "$total_failures" -gt 0 ]; then
  echo "::error::$total_failures repo(s) failed. Check the log above for details."
  exit 1
fi
