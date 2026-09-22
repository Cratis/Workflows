# Workflows

> [!IMPORTANT]
> This repository is for use by the **[Cratis](https://github.com/Cratis) organization only**. The reusable workflows here are designed specifically for the Cratis GitHub organization and include runtime validation that rejects calls from outside it.

Common reusable GitHub Actions workflows for Cratis repositories.

## Reviewed package updates

`.github/workflows/update-packages.yml` updates detected NuGet, NPM, Gradle and
Mix dependencies in the **calling repository**. Existing callers retain their
runner, token fallback, ecosystem commands, and legacy direct-push/rebase behavior.
Nothing opts a repository into PRs or a release policy automatically.

| Optional input | Default | Contract |
|---|---|---|
| `post-update-command` | Empty (skipped) | Trusted Bash command in the caller root, after **all** ecosystem updates and before **any** build. Runs with `-euo pipefail`; a nonzero exit blocks both publication modes. |
| `publication-mode` | `direct` | `direct` retains legacy publication; `pull-request` uses the validated-candidate rules below. Other values fail. |
| `pull-request-label` | Empty (no label) | One existing, caller-chosen label, only in `pull-request` mode. Must match `[a-zA-Z0-9][a-zA-Z0-9_.:/-]*`. No comma lists, whitespace, or implicit release intent. |
| `runs-on` | `ubuntu-latest` | Existing runner-label input; private callers should retain their approved self-hosted label. |
| `npm-package-exclusions` | `typescript` | Existing comma-delimited exclusions, supplemented by the caller's `.github/update-packages-npm-exclusions`. |

An individually approved caller can use this wrapper (replace
`REVIEWED_COMMIT_SHA` with the reviewed Workflows commit). The script path is an
example: the caller must commit its own implementation before enabling the hook.

```yaml
name: Update Packages
on:
  workflow_dispatch:

# Needed if the secret falls back to GITHUB_TOKEN; subject to caller/org policy.
permissions:
  contents: write
  pull-requests: write

jobs:
  update:
    uses: Cratis/Workflows/.github/workflows/update-packages.yml@REVIEWED_COMMIT_SHA
    with:
      runs-on: cratis-arc
      publication-mode: pull-request
      post-update-command: bash .github/scripts/sync-package-image-versions.sh
      # Optional: set this variable only to the caller's intended existing label.
      pull-request-label: ${{ vars.PACKAGE_UPDATE_PR_LABEL }}
    secrets:
      PAT_WORKFLOWS: ${{ secrets.PAT_WORKFLOWS || github.token }}
```

The checkout is **not Workflows**: helper scripts in this repository are unavailable
at runtime. The publication implementation is inline. Hook commands must be trusted,
static caller configuration, never interpolated event text; they run with the same
checkout access as dependency tools. Child scripts must propagate errors themselves
(the outer strict Bash cannot detect an error a child deliberately suppresses).
Hooks should only synchronize intended dependency-related tracked files, such as
Stage's Dockerfile image version, and must not commit, push, change Git configuration,
or start background writers.

### Validated-candidate rules (`pull-request` only)

- The checkout must equal the current remote default-branch tip, with a clean tracked
  worktree/index. A dispatch from a different/stale branch fails before updates.
- After updates and the hook, tracked changes are committed **before** builds. Thus
  package and Dockerfile changes share the exact commit whose tracked tree is built.
  Only `git add --update` is used. Untracked/ignored files, caches, and `.ai-work/`
  are never swept into the candidate; even staged new paths are rejected.
- **New hook-owned files (including renames to new paths) require a separate reviewed
  commit first.** Pre-track them before enabling the updater. Unstaged new files
  are not published and must not be required source inputs for validating the candidate.
  This mode deliberately does not offer an unrestricted `git add` hook escape hatch.
- All detected ecosystem builds must succeed. Tracked worktree/index changes made by
  builds invalidate the candidate instead of being committed or discarded. Ordinary
  untracked/ignored build outputs are permitted but never published. If nothing tracked
  changed before builds, no branch or PR is created, even if builds generate output.
- Immediately before/after pushing and after PR creation, the workflow checks the
  original base and candidate identities. Any observed base/default-branch change,
  candidate commit/tree/index drift, API failure, or readback mismatch fails the job.
  There is no rebase, reset, amend, force push, retry, or automatic merge in this mode.
- The exact commit is pushed only to
  `automation/package-update-<GITHUB_RUN_ID>-<GITHUB_RUN_ATTEMPT>`. After base checks,
  `POST /repos/{repository}/git/refs` atomically reserves this branch at the already
  remote validated base, requiring **absence** (an existing branch returns 422).
  A prior existence lookup is advisory, not the ownership boundary. Successful
  exclusive creation and matching API response/API and Git readbacks are required
  before a normal, nonforce push of the exact candidate. No existing ref is patched
  or reused, even if it points at the same base; API errors never fall through to push.
- `gh pr create` uses explicit `--repo`, `--head`, `--base`, title and body, so gh
  does not implicitly push/fork or prompt. It creates an ordinary PR against the
  default branch, with only the optional caller-selected label. The body is a
  user-facing `## Changed` release note, not an internal validation narrative.

These stronger staging and identity guarantees do **not** retrofit legacy `direct`
mode's inherited `git add -A` and rebase loop. Use `pull-request` for reviewed delivery.

### Tokens, permissions and operational limits

The secret remains optional: checkout and gh use `PAT_WORKFLOWS || github.token`.
No permissions block or repository/organization setting is changed by the reusable
workflow. Provision Bash, Git and `gh` on the chosen runner in addition to the existing
ecosystem prerequisites.

- A classic PAT needs `repo`; a fine-grained PAT needs repository **Contents: write**,
  **Pull requests: write**, and **Metadata: read**, including access to the selected
  existing PR label. Repository branch/ruleset policies must allow the new automation
  branch. PR mode does not require default-branch bypass.
- `GITHUB_TOKEN` needs caller-granted `contents: write` and `pull-requests: write`.
  The caller cannot elevate beyond organization/repository policy, and the existing
  **Allow GitHub Actions to create and approve pull requests** policy must permit PR
  creation. A disabled policy or missing permission is a blocking error, not a reason
  to fall back to direct pushes or change settings.
- [Events created using `GITHUB_TOKEN`](https://docs.github.com/en/actions/how-tos/writing-workflows/choosing-when-your-workflow-runs/triggering-a-workflow#triggering-a-workflow-from-a-workflow)
  generally do not trigger downstream workflows (except explicit dispatch events).
  A PR created successfully with that token therefore does **not** prove PR checks
  will start. Use an approved PAT when ordinary push/PR/label events must trigger
  caller CI/release-intent gates; this workflow dispatches no compensating workflows.
  PAT permissions are independent of the workflow's `GITHUB_TOKEN` permission block.

Ref reservation, Git push and GitHub PR APIs are not one transaction. A base/branch
can move between checks or after the final check; branch protection and fresh PR
checks remain the merge authority. Reservation establishes creation ownership, not
an exclusive lock against later writers. A failed API response can be ambiguous;
reservation/readback/push failure can leave a **base-only branch** without a PR.
Later races, failed PR/label operations, or inconsistent readback can leave a
candidate branch or ordinary PR while the job fails. The job reports these possible
leftovers loudly. Inspect ownership and state before any manual recovery; no
automatic cleanup, branch reuse, deduplication, or retry occurs.
A rerun starts from a fresh checkout and validates anew on a new attempt branch; it
can open another PR. The workflow never makes an old PR eligible for automatic merge.

**Deployment boundary:** package-update implementation changes are excluded from
`Bootstrap Common Workflows` push paths: the canonical wrapper template is unchanged,
existing callers already reference the reused implementation, and an implementation
update must not rewrite unrelated repositories. The bootstrap workflow itself is not
in those paths either, so this narrowly scoped rollout triggers no organization-wide
write workflow. All other watched workflow/template, CodeQL and bootstrap-script
triggers remain intact; changing those still requires separate deployment approval.
Stage is explicitly excluded from bootstrap because it owns its reviewed
package-update/kernel guard workflow and pin, which bootstrap must not overwrite.
No organization-wide wrapper migration or bootstrap execution is part of this change.
Existing `@main` callers still adopt implementation changes immediately; a targeted
caller such as Stage should retain its reviewed immutable pin.

### Offline regression checks

```bash
python3 .github/scripts/tests/update-packages.test.py
python3 .github/scripts/tests/bootstrap-package-update-safety.test.py
actionlint .github/workflows/update-packages.yml .github/workflows/verify-package-updates.yml \
  .github/workflows/bootstrap-common-workflows.yml
```

The tests extract and execute the actual inline Bash using temporary local bare Git
repositories and stub gh/toolchains. They cover ordering, failure gates, exact-tree
publication, atomic expected-absent reservation (including a same-base competing
branch race), API failure gates, default compatibility, stale bases, candidate drift,
labels, and unrelated output exclusion without credentials, network, or running the
legacy rebase loop. Structural tests verify Stage ownership, retained bootstrap and
PR-template triggers, and that the effective changed-file set avoids organization-wide
write triggers. CI supplies the PR base/pre-push SHA; local structural checks include
staged, unstaged and untracked nonignored files against HEAD. They do not test live
token policy, actual package availability, or downstream CI.

## Reviewed Cratis AI profile updates

`Update Cratis AI Profile Subscription` prepares normal pull requests for
repositories that explicitly subscribe through `.cratis/ai.json`. It replaces
fleet corpus copying with exact package-version updates; it never auto-merges or
changes project context.

The controller:

- accepts only an immutable `Cratis/AI.Distribution` release manifest URL and
  matching SHA-256;
- verifies profile, public/engineering channel, package name, and exact SemVer;
- performs a dry run unless `apply` and an exact repository confirmation are
  both supplied;
- changes only `.cratis/ai.json` and the matching exact Pi package source in
  `.pi/settings.json`;
- preserves Pi skill filters and unrelated settings;
- rejects partial APM-managed updates until their lockfile can be refreshed;
- supports explicit rollback to a lower exact release;
- scopes the GitHub App token to one authorized repository; and
- opens a normal PR whose repository checks must pass before a human merges it.

Run the controller locally without GitHub writes:

```bash
node .github/scripts/update-ai-profile-subscription.mjs \
  --repository /path/to/subscriber \
  --release-manifest /path/to/release-manifest.json
```

Add `--apply` only in a disposable/local checkout. The hosted workflow owns
branch and PR creation. Its one-time GitHub App installation scope and first
canary repositories remain tracked in Cratis/Workflows#72 and #73.

## AI setup in Cratis repositories

The legacy Copilot synchronization system — all-to-all propagation, the
per-repository sync wrappers, and the corpus bootstrap — is **retired**.
Cratis repositories no longer carry a synchronized `.ai/` corpus or generated
tool adapters.

Each repository instead commits the small, reviewed AI contract:

| File | Role |
|---|---|
| `.cratis/PROJECT.md` | the repository's own project context (facts, commands, conventions) |
| `.cratis/ai.json` | the profile subscription: which `cratis/*` profiles the repository expects |
| `AGENTS.md` + `CLAUDE.md`/`GEMINI.md` | minimal bootstraps pointing at `.cratis/PROJECT.md` |

Shared behavior arrives through the Cratis AI marketplace plugins (see the
[harness guide](https://www.cratis.io/ai/harnesses/)); profile updates land as
reviewed pull requests through `update-ai-profile-subscription.yml` above.
General improvements are proposed in `Cratis/AI`, never synchronized
back from a consuming repository.

---

## Cleaning up PR artifacts

When a repository publishes Docker images or NuGet packages during pull requests (for example, pre-release builds tagged with the PR number), those packages should be removed once the pull request is closed to avoid accumulating stale artifacts.

### Deployment boundary

> [!WARNING]
> Merging a change to `.github/workflows/cleanup-pr-artifacts.yml` into `main`
> triggers **Bootstrap Common Workflows**, which can write directly to default
> branches across the organization. Review and explicitly authorize that separate
> deployment effect before merging. Do not dispatch bootstrap merely to test cleanup.
> Existing callers using `@main` also adopt the changed implementation immediately.

The wrapper below can be added to an individually approved repository. Pin its
reusable workflow reference to a reviewed commit when an immutable rollout is needed.

### Manual setup

If you prefer to add the wrapper manually, create the following file in your repository:

**`.github/workflows/cleanup-pr-artifacts.yml`**

```yaml
name: Cleanup PR Artifacts

on:
  pull_request:
    types: [closed]

jobs:
  cleanup:
    uses: Cratis/Workflows/.github/workflows/cleanup-pr-artifacts.yml@main
    with:
      pull_request: ${{ github.event.pull_request.number }}
    secrets: inherit
```

The workflow assumes packages are tagged or versioned using the PR number:

| Package type | Expected pattern | Example |
|---|---|---|
| Container image (Docker) | Every tag matches `^pr{number}($\|-)` | `pr42`, `pr42-linux` |
| NuGet package | Version matches `(^\|[.-])pr{number}([.-]\|$)` | `1.0.0-pr42.1` |

The boundary patterns preserve existing matching: `pr42` never matches `pr420`.
A container version with both a matching tag and any other tag (including `latest`,
a stable tag, or another PR) **fails preflight** instead of deleting the shared digest.
Untagged/nonmatching versions and packages explicitly linked elsewhere are ignored.
A null repository association is unlinked and ignored; missing or malformed
association data fails closed rather than guessing ownership.

Only packages whose `repository.full_name` equals the **calling repository** are
eligible in reusable calls. Both container and NuGet inventories are completely
paginated and validated before any DELETE. Listing errors, invalid JSON, malformed
pagination, and identity drift fail the job; they are never treated as an empty
inventory. A genuinely empty inventory succeeds with a zero count.

Every planned package association and version identity is read again during full
preflight and immediately before deletion. Each DELETE must return 204 or 404,
then a fresh package read must still confirm ownership, the version GET must return
404, and the active-version inventory must exclude the ID. Authentication errors
are not waived. Other DELETE failures stop immediately, without blind retries.
Readback mismatch makes the run red even if earlier deletes succeeded.

The program is inline in the reusable workflow: it neither checks out caller code
with the package token nor fetches a helper from a moving `main` reference.

**Secret required:** `PAT_WORKFLOWS`, with package read/delete access and the package
administration rights required by GitHub. Classic PAT scopes are `read:packages`
and `delete:packages`; private repository visibility may also require `repo`.
Manual apply additionally needs read access to the target pull request. Use a
GitHub-supported token type for the organization Packages endpoints; do not assume
a fine-grained token supports them. A permission failure is a blocking error.

### Retry an already-closed PR safely

Dispatch **Cleanup PR Artifacts in Cratis/Workflows**, not the caller repository.
No reopen/close event is needed. One run addresses exactly one repository and PR;
there is no fleet mode. Manual runs are restricted to `refs/heads/main` and default
to `dry_run: true`. Before enabling manual use, configure the Workflows environment
`pr-package-cleanup` with required reviewers and a `main`-only deployment rule.
This workflow does not create or change environments or secrets.

| Manual input | Requirement |
|---|---|
| `target_repository` | Explicit `Cratis/repository` |
| `pull_request` | Positive integer |
| `dry_run` | Defaults to `true`; `false` requests apply |
| `confirmation` | Apply requires exactly `DELETE Cratis/repository PR number` |
| `expected_targets` | Apply requires the **entire approved** `cleanup-plan.json`, not just a count |

Example commands for a separately authorized operator (not run by tests):

```bash
# Read-only dry run, after the workflow has been deployed through review.
gh workflow run cleanup-pr-artifacts.yml --repo Cratis/Workflows --ref main \
  -f target_repository=Cratis/Chronicle -f pull_request=42 -f dry_run=true

# Download the artifact from that exact dry-run ID, not the latest matching run.
gh run download DRY_RUN_ID --repo Cratis/Workflows \
  -n cleanup-plan-DRY_RUN_ID-1 -D .ai-work/cleanup-pr42

# Only after reviewing/approving every target and retaining verified backups:
gh workflow run cleanup-pr-artifacts.yml --repo Cratis/Workflows --ref main \
  -f target_repository=Cratis/Chronicle -f pull_request=42 -f dry_run=false \
  -f 'confirmation=DELETE Cratis/Chronicle PR 42' \
  -F expected_targets=@.ai-work/cleanup-pr42/cleanup-plan.json
```

Use the actual run attempt in the artifact name. The machine-readable plan records
repository, PR, package types/names, numeric version IDs, version names/tags, and
DELETE/restore endpoints; it is printed as JSON and retained as a 30-day run
artifact, referenced in the run summary. It contains no credentials. Keep local
plans and backups in ignored `.ai-work/` or approved secure storage, never Git.

Apply requires the PR to be closed, verifies its number and base repository, and
compares a freshly completed inventory against the entire approved manifest.
Any added, removed, renamed, or retagged target rejects apply before deleting.
An empty approved manifest cannot authorize apply. There is no bypass input.
The closed state is checked again before each deletion.

**Recovery and limitations:** Packages APIs are not transactional. Publication,
retagging, repository transfers, PR reopening, and permissions can race the last
check; coordinate/quiesce publishers before applying. A 404 alone does not prove
deletion (GitHub may hide resources), hence the additional authenticated package
and active-list readbacks. Inventories cover only what the token can see; a
permission-filtered HTTP 200 cannot prove organization-wide visibility. Provision
visibility for all packages linked to the target repository. Manual manifest
comparison also rejects a visibility change that hides an approved target.
API consistency delays conservatively fail the run.
A partial failure does not roll back already-deleted versions. Inspect the run,
obtain a new dry run and explicit approval for the remaining targets before retrying;
do not reuse or silently trim an old approval. Restore endpoints are recovery
metadata, not backups or a restoration guarantee: GitHub's restoration window,
permissions, and name availability still apply. Retain verified package bytes
before destructive recovery. No restore calls are made by this workflow.

The environment protects the manual job as configured, but repository/organization
secret availability and trusted workflow writers remain security boundaries.
Reusable automatic callers retain their existing `pull_request` + `PAT_WORKFLOWS`
contract and apply behavior, without requiring manual manifests or an environment.
They must invoke cleanup only for the intended closed PR.

### Offline verification

```bash
python3 .github/scripts/tests/cleanup-pr-artifacts.test.py
actionlint .github/workflows/cleanup-pr-artifacts.yml \
  .github/workflows/verify-pr-artifact-cleanup.yml
```

The regression suite extracts the actual inline production program and substitutes
an in-memory HTTP API. It checks Bash syntax and positive/negative inventory,
scoping, pagination, approval, deletion/readback, and redaction behavior without
GitHub credentials or network requests. `Verify PR Artifact Cleanup` runs it on
relevant pull requests and main pushes; it never invokes live cleanup.

---

## Auto-approving publish deployments

For repositories using trusted publishing with npm and NuGet environments, the `auto-approve-publish-deployments` workflow automatically approves pending `npm` and `nuget` environment deployments when a publish workflow completes.

This workflow is distributed to all Cratis repositories via the common bootstrap process. It runs automatically whenever the `Publish` workflow finishes — no additional configuration is needed.

### How it works

1. When a `Publish` workflow completes (whether successful or not)
2. The `Auto-Approve Publish Deployments` workflow is triggered
3. It waits up to 30 minutes for pending npm/nuget deployments to appear
4. Any pending deployments to `npm` or `nuget` environments are automatically approved
5. Publishing proceeds without manual intervention

### Distributed via bootstrap

This workflow is included in the common workflow bootstrap process and is automatically propagated to all Cratis repositories alongside other default workflows.

> [!NOTE]
> See [publish.template.yml](/.github/workflows/publish.template.yml) for an example of a publish workflow that this auto-approve workflow will watch.

---

## Shared CodeQL configuration

The common workflow bootstrap also propagates **`.github/codeql/codeql-config.yml`** to repositories. The shared config currently excludes rule `ca1031`.

---

## Workflows in this repository

### `cleanup-pr-artifacts.yml`

**Triggers:** `workflow_call` for closed-PR callers; `workflow_dispatch` in Workflows
for an explicitly targeted manual dry run or approved retry.

Deletes only exactly matching, repository-associated container/NuGet versions after
complete preflight, and verifies their absence. The reusable contract remains the
required numeric `pull_request` input and `PAT_WORKFLOWS` secret. See
[Cleaning up PR artifacts](#cleaning-up-pr-artifacts) for matching rules, permissions,
manual approval inputs, recovery limitations, and offline verification.

---

### `bootstrap-common-workflows.yml`

**Triggers:** `push` to `main` when a watched reusable workflow (including
`cleanup-pr-artifacts.yml`), CodeQL config, or bootstrap script changes; also
`workflow_dispatch`.

Maintains common wrappers across non-archived Cratis repositories, subject to its
`REPOS_TO_IGNORE` list. This is an **organization-wide write workflow**, including
direct default-branch updates, not an offline check or an automatically authorized
cleanup rollout. Its PAT requires the repository/workflow write permissions and
branch-rule bypass described in the workflow. Exact deployment effects need
separate approval before merging a watched file.

---

### `propagate-pr-templates.yml`

**Trigger:** `push` to `main` (when template files change) or `workflow_dispatch`

Propagates the Pull Request and Issue templates from this repository (`Cratis/Workflows`) directly to the default branch of every other non-archived Cratis repository. Silently skips repositories where files are already up to date.

**Excluded repositories:** `Workflows`, `cratis.github.io`, `StudioIssues`.

**Secrets required:** `PAT_WORKFLOWS` — classic PAT with `repo` scope, or fine-grained PAT with **Contents** read/write + **Metadata** read. The PAT owner must be a bypass actor on each target repository's branch protection ruleset.

---

## Part of the Cratis ecosystem

These workflows power CI/CD across the [Cratis](https://github.com/Cratis) open-source ecosystem — [Chronicle](https://github.com/Cratis/Chronicle) (event sourcing database and runtime), [Arc](https://github.com/Cratis/Arc) (CQRS for ASP.NET Core), [Components](https://github.com/Cratis/Components) (React), the [CLI](https://github.com/Cratis/cli), and more. Documentation lives at [cratis.io](https://www.cratis.io).
