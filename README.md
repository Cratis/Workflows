# Workflows

> [!IMPORTANT]
> This repository is for use by the **[Cratis](https://github.com/Cratis) organization only**. The reusable workflows here are designed specifically for the Cratis GitHub organization and include runtime validation that rejects calls from outside it.

Common reusable GitHub Actions workflows for Cratis repositories.

## Superseded bump pull requests

`.github/workflows/close-superseded-bump-pulls.yml` finds older open bump PRs
superseded by a newer open PR against the same base. The newer PR must include all
of the older PR's dependencies, with no lower version and at least one higher
version. Titles such as `Bump Arc from 19.0 to 19.1 and Chronicle to 19.2` and
`align-versions/stage-A-scene-B-screenplay-C` branches are recognized.

| Input | Default | Purpose |
|---|---|---|
| `branch-prefixes` | `align-versions/` | Space-separated head branch prefixes required for replacements, closure, and branch deletion. An empty list permits none of these. |
| `authors` | `github-actions[bot]` | Space-separated trusted bot/PAT logins whose superseded PRs may be closed. |
| `dry-run` | `false` | Print planned actions without any writes. |
| `runs-on` | `ubuntu-latest` | Runner label; use a self-hosted runner for private repositories. |
| `workflows-ref` | `main` | Trusted Cratis/Workflows ref providing the program, not a PR head ref. |

A replacement PR must have an author listed in `authors`, a head in the caller
repository, and a branch matching a configured prefix. Untrusted PRs, including
forks and non-prefix branches, never trigger comments, closure, or deletion.
The same three checks are required to close an older PR and delete its branch.
Other older PRs superseded by a trusted replacement (including human-authored
bumps) receive only one `Superseded by #Q` comment per replacement. An empty prefix
list permits no replacements or closures. Do not add human contributors to
`authors`. Dependabot PRs and `keep-open` PRs are never closed. API failures fail
the job. The workflow **never checks out PR code**: it checks out only
Cratis/Workflows at `workflows-ref` to run the supporting program.

Example caller (choose a repository-specific, nonzero schedule minute):

```yaml
name: Close superseded bumps
on:
  pull_request_target:
    types: [opened, reopened, edited]
  schedule:
    - cron: '23 * * * *'
  workflow_dispatch:
permissions:
  pull-requests: write
  contents: write
jobs:
  close-superseded:
    uses: Cratis/Workflows/.github/workflows/close-superseded-bump-pulls.yml@main
    with:
      branch-prefixes: align-versions/
      authors: github-actions[bot]
```

The hourly sweep also catches PRs opened with `GITHUB_TOKEN`, whose events do not
normally start other workflows. Start with `dry-run: true` before enabling writes.

## Reviewed package updates

`.github/workflows/update-packages.yml` updates detected NuGet, NPM, Gradle and
Mix dependencies in the **calling repository**. Existing callers retain their
runner, token fallback, ecosystem commands, and legacy direct-push/rebase behavior.
Nothing opts a repository into PRs or a release policy automatically.

NPM keeps the existing `npx npm-check-updates -u -w -x "$exclusions"` command,
without a `--dep` override. ncu's default sections (which exclude peers) and each
caller's `.ncurc` remain authoritative, including opt-in peer updates and exclusions
of production or `packageManager` updates. ncu's peer-range behavior is unchanged.

After ncu, an unchanged `^<pin>` or `~<pin>` peer that exactly matched a local
production/development pin before the update follows its new exact pin **only if
the old peer range still admits it**. Carets stay within the same major for 1.x+,
the same minor for 0.x, and the identical version tuple for 0.0.x; tildes stay
within the same major/minor. SemVer lower bounds and prerelease restrictions also
apply. An out-of-range pin leaves the peer unchanged and emits a warning naming
the manifest, package, old range and new pin; it does not fail the update. Such
compatibility changes require manual review. Unions, lower bounds, bounded ranges,
wildcards, unmatched peers and peers already changed by ncu are not synchronized.
The snapshot covers the root and declared workspaces (`workspaces` or
`workspaces.packages`), not unrelated fixtures or templates; non-object manifests
are skipped with a notice and null/non-object dependency sections are treated as
empty. Symlinked manifests remain refused.

After installation, Yarn 2.2+ dedupes compatible transitive resolutions so a bumped
direct range does not leave a second older instance. Yarn 1 and Yarn 2.0/2.1 skip
dedupe.

| Optional input | Default | Contract |
|---|---|---|
| `post-update-command` | Empty (skipped) | Trusted Bash command in the caller root, after **all** ecosystem updates and before **any** build. Runs with `-euo pipefail`; a nonzero exit blocks both publication modes. It runs even when the updaters changed nothing, because its own changes can be the update. |
| `publication-mode` | `direct` | `direct` retains legacy publication; `pull-request` uses the validated-candidate rules below. Other values fail. |
| `pull-request-label` | Empty (no label) | One existing, caller-chosen label, only in `pull-request` mode. Must match `[a-zA-Z0-9][a-zA-Z0-9_.:/-]*`. No comma lists, whitespace, or implicit release intent. |
| `node-version` | `24` | Node.js version for NPM updates. Satisfies the Cratis JavaScript repositories' engine floors; a caller may override it, but the minimum is Node 22 (the peer sync script needs `fs.globSync` with array `exclude`, available from 22.14/23.7). |
| `runs-on` | `ubuntu-latest` | Existing runner-label input; private callers should retain their approved self-hosted label. |
| `npm-package-exclusions` | `typescript` | Existing comma-delimited exclusions, supplemented by the caller's `.github/update-packages-npm-exclusions`. |
| `nuget-package-exclusions` | `StyleCop.Analyzers` (its 1.3.x betas fail the build of repositories on 1.2.0-beta.556; a caller that sets this input replaces the default and lists the package itself to keep it held) | Comma-delimited exact NuGet package IDs, matched case-insensitively after trimming whitespace. Applies to top-level project references, including centrally managed versions in `Directory.Packages.props`; transitive-only IDs warn as not directly referenced (updating a parent can still change transitive versions). Prerelease references retain the updater's prerelease eligibility; stable references do not gain prerelease-only candidates. Invalid/incomplete listings and divergent per-framework versions for eligible packages fail before updating. As with the default path, SDK exit 3 (no changes written) skips NuGet updates for that run. |

**No-op runs cost no build.** After the updaters and the post-update command, the
workflow checks `git status` (tracked changes plus new files that are not ignored).
When nothing changed it logs `No package changes; skipping builds and publication.`
and skips every ecosystem build, the candidate commit, the direct-mode push and the
pull request; the job succeeds. A Git error in that check fails the job instead of
reading as "no changes".

**NuGet packages come from the Actions cache.** When .NET is detected, the workflow
restores `~/.nuget/packages` with `actions/cache/restore` under
`nuget-v1-<runner OS>-<hash>`, hashed before any update from `**/packages.lock.json`,
`**/*.props`, `**/*.targets`, `**/*.csproj`, `**/[Nn]u[Gg]et.[Cc]onfig`, `*.slnx` and
`global.json`, with the `nuget-v1-<runner OS>-` prefix as fallback. A caller whose own
.NET builds cache `~/.nuget/packages` under the same key shares that cache. `actions/cache` with
`lookup-only` saves the cache at the end of a successful job when that key does not
exist yet. Without any key file the run restores packages from the feeds as before.

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
      nuget-package-exclusions: Cratis.Fundamentals
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
  untracked/ignored build outputs are permitted but never published. If nothing changed
  after the updates and the hook, builds are skipped and no branch or PR is created. If
  only untracked files changed, builds run but no branch or PR is created.
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
- **At most one open package-update PR.** Before reserving a branch, the workflow lists
  the caller's open PRs and considers only its own: head branch in the caller
  repository (never a fork) named `automation/package-update-<run>-<attempt>`, the
  same base, and a head that is a single `chore: update packages` commit by
  `github-actions[bot]` whose parent is on the base. Any other PR, including one of
  its PRs a person has pushed to, is never compared, closed or deleted.
  - If one of those PRs carries a byte-identical update diff (`git diff --binary
    --full-index` against its own parent), no branch or PR is created and a notice
    names that PR. The newest such PR is kept, and every other workflow-owned
    package-update PR, identical or not, is superseded by it, so exactly one stays open.
  - Otherwise the new PR is created as above, and only after its readback succeeds
    are the older ones superseded.
  - Superseding a PR means commenting with a link to the kept PR, closing it, and
    deleting its branch when that branch still points at the inspected commit
    (GitHub's **Restore branch** on the closed PR undoes this). A PR whose state, head
    or base changed after the lookup is left open with a warning.
  - A failed or unexpected lookup fails the job before anything is published; a
    failed comment, close or branch deletion fails the job after naming the PR. No
    step is retried.

These stronger staging and identity guarantees do **not** retrofit legacy `direct`
mode's inherited `git add -A` and rebase loop. Use `pull-request` for reviewed delivery.

### Tokens, permissions and operational limits

The secret remains optional and the caller interface is unchanged. Only the
`publish` job uses `PAT_WORKFLOWS || github.token`, for its fresh checkout and GitHub
writes. The `update` job checks out with `github.token` and
`persist-credentials: false`; its API reads use that built-in token, never the PAT.
All package tooling, the post-update command and builds stay in `update`. A
one-day artifact carries the frozen commit as a Git bundle plus base, branch,
publication options and PR text. After a successful update with changes, `publish`
checks out the recorded base and verifies the artifact's metadata, parent, commit
and tree before applying it. It runs only inline Git/gh publication logic, never
repository scripts or package tooling. Direct mode still commits after builds and
retries rejected pushes with its existing rebase loop; PR mode still freezes before
builds and never rebases. The artifact name is stable within a workflow run, so
re-running only a failed publish job reuses the successful update's artifact; a
rerun of update replaces it.

Both modes refuse the entire candidate if it changes `.github/` (including
workflows, actions, CODEOWNERS and Dependabot), `.cratis/ai/hooks/`, `.pi/`,
`.claude/`, `.mcp.json`, `.vscode/mcp.json`, `.gitattributes`, `.gitmodules`, or Git
configuration/hook paths. Git paths are conservatively protected whenever any
path component starts with `.git` (including `.gitconfig` and `.githooks/`).
This includes additions, deletions, renames in either direction, file type or
executable-bit changes, and replacing protected ancestors with files or symlinks.
The update job checks before upload; publish independently
checks the bundled commit before checkout or any write. In particular,
`post-update-command` can no longer change these paths: make such changes in a
separately reviewed change, not a package-update candidate. Failures list every
offending path.

No permissions block or repository/organization setting is changed by the reusable
workflow. The built-in token must have repository read access even when publishing
uses a PAT. Provision Bash, Git and `gh` on the chosen runner in addition to the
existing ecosystem prerequisites. Use isolated, ephemeral runners: splitting jobs
does not make a persistent self-hosted runner safe if dependency code can leave
processes or alter tools for a later job.

- A classic PAT needs `repo`; a fine-grained PAT needs repository **Contents: write**,
  **Pull requests: write**, and **Metadata: read**, including access to the selected
  existing PR label. Repository branch/ruleset policies must allow the new automation
  branch, and deleting a superseded one. PR mode does not require default-branch
  bypass. The same permissions cover listing, commenting on and closing superseded
  PRs; no further permission is needed.
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
leftovers loudly. Inspect ownership and state before any manual recovery; such
failed-run leftovers are never cleaned up automatically, no branch is reused, and
nothing is retried. The only automatic cleanup is superseding the workflow's own
earlier open PRs described above; a base-only reservation branch without a PR is
not considered. A rerun starts from a fresh checkout and validates anew on a new
attempt branch; it skips publication when an open PR already carries the same
update, and otherwise supersedes it. The workflow never makes an old PR eligible
for automatic merge.

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
python3 .github/scripts/tests/bootstrap-schedules.test.py
actionlint .github/workflows/update-packages.yml .github/workflows/verify-package-updates.yml \
  .github/workflows/bootstrap-common-workflows.yml
```

The tests extract and execute the actual inline Bash using temporary local bare Git
repositories and stub gh/toolchains. They cover ordering, failure gates, exact-tree
publication, atomic expected-absent reservation (including a same-base competing
branch race), skipping or superseding the workflow's own open PRs (and leaving every
other PR alone), skipped builds and publication when nothing changed, the SHA-pinned
NuGet cache, API failure gates, default compatibility, stale bases, candidate drift,
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
- changes only `.cratis/ai.json`, the matching exact Pi package source in
  `.pi/settings.json`, and the managed Pi runtime-state block in `.gitignore`;
- preserves Pi skill filters and unrelated settings;
- rejects partial APM-managed updates until their lockfile can be refreshed;
- supports explicit rollback to a lower exact release;
- scopes the GitHub App token to one authorized repository; and
- opens a normal PR whose repository checks must pass before a human merges it.

When a subscriber selects Pi, an update also ensures this managed block in its
`.gitignore` (the dry-run receipt lists `.gitignore` among the changes and
reports `piRuntimeIgnore` as `missing`, `incomplete` or `current`; a rollback
leaves it alone):

```gitignore
# cratis-ai: pi runtime state
.ai-work/
.pi/delegate/
.pi/fusion/
.pi/tasks/
.pi/*-session-*/
# end cratis-ai: pi runtime state
```

`.ai-work/` is included only when no other line in `.gitignore` already ignores
it. Malformed (unmatched, nested or duplicate) managed markers are rejected before
any writes, preserving unrelated ignore rules. `verify-no-work-records.yml` fails
when these directories are tracked and the caller's path filters trigger it.

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
paginated and validated before any DELETE. The only listing-error exception is
HTTP 401/403 on the organization package inventory: reusable calls and dry runs
report a neutral warning and delete nothing, without treating it as an empty
inventory. Manual apply with an approved manifest fails instead. All other
listing errors, invalid JSON, malformed pagination, and identity drift fail the
job. A genuinely empty inventory succeeds with a zero count.

Every planned package association and version identity is read again during full
preflight and immediately before deletion. Each DELETE must return 204 or 404,
then a fresh package read must still confirm ownership, the version GET must return
404, and the active-version inventory must exclude the ID. Authentication errors
are not waived. Other DELETE failures stop immediately, without blind retries.
Readback mismatch makes the run red even if earlier deletes succeeded.

The program is inline in the reusable workflow: it neither checks out caller code
with the package token nor fetches a helper from a moving `main` reference.

**Cleanup token:** optionally create a classic PAT with `read:packages` and
`delete:packages`, package administration rights, and organization SSO
authorization if enforced. Store it as the organization secret
`PACKAGE_CLEANUP_TOKEN`, accessible to the calling repositories and Workflows for
manual runs. Cleanup uses it when provided; otherwise it falls back to
`PAT_WORKFLOWS` (still required by the reusable caller contract). Private
repository visibility may also require `repo`; manual apply additionally needs
read access to the target pull request. Fine-grained PATs do not support these
organization Packages endpoints.

The bootstrapped cleanup wrapper and the manual setup above use `secrets: inherit`,
so they automatically pass `PACKAGE_CLEANUP_TOKEN` when it is available to the
caller. Callers with an explicit secrets map must add
`PACKAGE_CLEANUP_TOKEN: ${{ secrets.PACKAGE_CLEANUP_TOKEN }}` to opt in; callers
that pass only `PAT_WORKFLOWS` are unaffected.

Permission failures remain blocking except for the organization inventory
HTTP 401/403 warning described above. HTTP 401 also covers an expired or revoked
token: it now produces a warning in reusable cleanup calls and dry runs, but
manual apply and other workflows using that secret still fail.

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
  .github/workflows/verify-workflow-programs.yml
```

The regression suite extracts the actual inline production program and substitutes
an in-memory HTTP API. It checks Bash syntax and positive/negative inventory,
scoping, pagination, approval, deletion/readback, and redaction behavior without
GitHub credentials or network requests. `Verify Workflow Programs` runs it on
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
> See [publish.template.yml](/.github/templates/publish.template.yml) for an example of a publish workflow that this auto-approve workflow will watch. Copy it to `.github/workflows/publish.yml` in the consuming repository and replace the placeholder package-publishing steps. It lives outside `.github/workflows` here because it is an incomplete example, not an executable workflow.
>
> The template never cancels or drops a release. `cratis/release-action` releases one merged pull request per run, and GitHub's default concurrency keeps a single pending run per group and cancels any older pending one, so merges in quick succession would lose releases. Its concurrency group therefore uses `queue: max`, which keeps up to 100 pending runs and starts them in order, and leaves `cancel-in-progress` out (GitHub rejects it together with `queue: max`). A pull request closed without merging gets a group of its own, so it never takes a place in front of a real release. Keep this block when customizing the copy; [`verify-release-concurrency.yml`](#verify-release-concurrencyyml) checks it across the organization.

---

## Batched releases

By default `cratis/release-action` releases every merged pull request on its own. A repository can opt in to **batched releases** instead: the publish workflow also runs on a schedule, and a run releases everything merged since the last release once the oldest pending merge is older than a window. Many small merges then become one release. A scheduled run with nothing pending, or a window that has not elapsed, ends in seconds without releasing. Behavior is unchanged until a repository opts in; the [publish template](/.github/templates/publish.template.yml) carries the opt-in as a commented-out block (Stage is the pilot).

### Opting in

Two edits to `publish.yml` (the template shows both, commented out):

```yaml
on:
  schedule:
    - cron: "6,21,36,51 * * * *"   # minutes from the helper below
# ...
      - uses: cratis/release-action@v1
        with:
          release-window: 60   # minutes; 0 (the default) releases per merge as before
```

Keep the concurrency group constant (no per-run or per-commit key) and keep `queue: max`.

### Choosing the minutes

Repositories must not all tick at the same minutes. Run the helper with the repository name and use the line it prints:

```bash
.github/scripts/batched-release-cron.sh Chronicle
# 12,27,42,57 * * * *
```

It derives an offset from 0 to 14 from the name (a checksum, like the weekly package-update schedule) and ticks four times an hour at the offset, +15, +30 and +45. Stage predates the helper and keeps its `7,22,37,52`.

### Releasing sooner

- A pull request labelled `hotfix` releases on its own merge push, without waiting for the window.
- A manual `workflow_dispatch` run with the version left empty or `0.0.0` releases everything pending now.
- If a hotfix merges while a `major` pull request is pending, the run started by the hotfix's own merge fails, because the pending major cannot ship inside a hotfix; the other runs only warn.

### Staleness check

GitHub cron is best effort and can skip or lag. [`check-batched-releases.yml`](#check-batched-releasesyml) sweeps hourly and fails when a pending merge is older than the window plus 60 minutes.

---

## Shared CodeQL configuration

The common workflow bootstrap also propagates **`.github/codeql/codeql-config.yml`** to repositories. The shared config currently excludes rule `ca1031`.

---

## Shared cache actions

Two composite actions replace hand-written `actions/cache` steps. Each gives a repository one key and one path list per ecosystem. That matters because the path list is part of the cache version: a cache saved by a step with one path list is never restored by a step with another, even under the same key.

Both follow the same rule:

- **Pull requests only restore**, through `actions/cache/restore`. They never save, so they can no longer push main's caches out of GitHub's 10 GB repository limit.
- **Saves happen only** on `push` to the default branch, on `schedule`, and on `workflow_dispatch` against the default branch. The save runs at the end of the job, and is skipped when a cache for the exact key already exists.
- `mode: restore` never saves and `mode: save` always can; the default `mode: auto` applies the rule above.

A pull request can only restore what main saved. **Add a push-to-main or nightly job that calls the action**, usually the build job you already run on `push: main`, so main always has a cache to restore.

Outputs: `cache-hit` (`true` only when the exact key was restored), `matched-key` (the restored key, empty on a miss), `key` and `mode` (`restore` or `save`, as resolved).

### `dotnet-cache`

Caches NuGet packages only. Build output (`obj`) is not cached because it goes stale; reuse it within a run through a build-once artifact instead.

```yaml
# after checkout and actions/setup-dotnet
- uses: Cratis/Workflows/.github/actions/dotnet-cache@main
- run: dotnet restore
```

| Input | Default |
|---|---|
| `path` | `~/.nuget/packages` |
| `key-files` | `**/packages.lock.json`, `**/*.props`, `**/*.targets`, `**/*.csproj`, `**/[Nn]u[Gg]et.[Cc]onfig`, `*.slnx`, `global.json` (one per line) |
| `key-prefix` | `nuget-v1` |
| `mode` | `auto` |

The key is `<key-prefix>-<runner.os>-<hashFiles(key-files)>`, with `<key-prefix>-<runner.os>-` as the restore key. The action fails when `key-files` matches no file, because a key without a content hash would make every run share one stale cache.

### `yarn-cache`

Caches Yarn's package cache. It never caches `node_modules` (Yarn rebuilds it from the package cache on install) or `yarn.lock` (a restore would overwrite the checked-out lockfile before `yarn install --immutable` validates it). It fails when `working-directory` has no `yarn.lock`.

Yarn 4 keeps its cache in a global folder (`~/.yarn/berry/cache`) unless `enableGlobalCache: false`, so by default the action caches the folder `yarn config get cacheFolder` reports in `working-directory`. That needs Yarn on `PATH`: run `actions/setup-node` and `corepack enable` before the action. Without Yarn, or when Yarn reports no folder (Yarn 1 prints `undefined`), it falls back to `<working-directory>/.yarn/cache`. An explicit `path` always wins and Yarn is not asked.

```yaml
# after checkout, actions/setup-node and corepack enable
- uses: Cratis/Workflows/.github/actions/yarn-cache@main
  with:
    working-directory: Source/JavaScript
- run: yarn install --immutable
  working-directory: Source/JavaScript
```

| Input | Default |
|---|---|
| `working-directory` | `.` |
| `path` | Yarn's `cacheFolder`, else `<working-directory>/.yarn/cache` |
| `key-prefix` | `yarn-v1` |
| `mode` | `auto` |

The key is `<key-prefix>-<runner.os>-<hashFiles(<working-directory>/yarn.lock, <working-directory>/.yarnrc.yml)>`, with `<key-prefix>-<runner.os>-` as the restore key.

`verify-cache-actions.yml` runs `.github/scripts/tests/cache-actions.test.py` (mode resolution, shared path and key, pinned actions) and calls both actions against `.github/scripts/tests/fixtures/cache-actions/` under `selftest-*` prefixes, so the self-test never touches a real cache.

---

## Verifying release intent and release notes

**`verify-pull-request-policy.yml`** combines release notes, warn-only release-note drift,
release intent and the work-record guard into one reusable job. A caller job named
`policy` produces the check **`policy / verify`**. Existing required-check settings
must be migrated explicitly when replacing older callers; bootstrap does not change
branch protection or rulesets.

The caller runs on pull-request events `opened`, `edited`, `reopened`, `synchronize`,
`labeled`, `unlabeled`, `ready_for_review`, and pushes to `main`, with one cancelling
concurrency group per PR/ref. Push events run only the work-record policy. Intent
applies only to PRs targeting `main` and can be disabled with `release-intent: false`.
Notes retain the default-branch, release-label and Dependabot rules below. The
reusable declares no permissions; callers grant `contents: read` and
`pull-requests: read`. Releasing repositories also have a separate
`dependabot-labels` job with `pull-requests: write`, limited to Dependabot PRs into
`main`; `policy` runs even after skipped or failed normalization. The intent gate
itself never mutates labels and never fails Dependabot for its labels.

```yaml
permissions:
  contents: read
  pull-requests: read

jobs:
  policy:
    uses: Cratis/Workflows/.github/workflows/verify-pull-request-policy.yml@main
    with:
      runs-on: ${{ vars.RUNNER_GATE || 'ubuntu-latest' }}
      release-intent: false # A repository that does not release
```

This sample shows only the gate job. A complete caller also declares the triggers and
the concurrency group above and, in a releasing repository, the `dependabot-labels`
job (with `policy` declaring `needs: dependabot-labels` and `if: ${{ !cancelled() }}`).
Notes and intent run only for `pull_request` events; a caller on another event gets
only the work-record policy.

The merge-tree checkout uses `persist-credentials: false` and is scanned by the
same commit- and SHA-256-pinned work-record guard. `extra-allowed` and
`extra-allowed-paths` retain the guard's meanings below. Drift fetches the PR head
and base into the shared repository and uses a separate detached worktree so its
unchanged program sees the PR head as `HEAD`, not the merge commit. Fetch or
worktree failure warns without failing policy. Each blocking gate runs with its
own recorded outcome; the final step fails on a failed, unexpectedly skipped or
missing gate, including guard exit `2` (could not run), and writes a per-gate summary.
The job checks `node`, `git` and `curl` availability and has a ten-minute timeout.

### Legacy callers (kept for pinned callers)

`verify-release-notes.yml`, `verify-release-intent.yml` and
`verify-no-work-records.yml` remain unchanged and available for pinned or
hand-maintained callers. The older release gates behave as follows:

- **`verify-release-intent.yml`** (called by each repository's `verify-semver-label.yml`) requires exactly one of `major`, `minor`, `patch` or `no-release`, and only `no-release` on a Dependabot pull request: Dependabot applies `major`/`minor`/`patch` from the dependency's own version, and release-action never releases its pull requests. It never fails a Dependabot pull request for its labels: it replaces `major`/`minor`/`patch` with `no-release` itself when its token can write, and passes with a notice when it cannot, so callers that predate the normalizer (the repositories the bootstrap ignores) keep working. **`normalize-dependabot-labels.yml`** does the same ahead of the gate in the bootstrapped caller, whose gate job only has `pull-requests: read`.
- **`verify-release-notes.yml`** checks the pull request description, because [release-action](https://github.com/Cratis/release-action) publishes it verbatim as the GitHub release and closes every issue written as `(#n)` outside code.

Both gates read the labels (and the release-notes gate the description, author and base) from the pull request as it is now, not from the event payload, so "Re-run" sees the current state instead of replaying the event that started the run. A caller grants `pull-requests: read` for that read; without it the gates fall back to the payload with a notice. The reusable workflows set no `permissions` of their own on purpose: a called workflow can only narrow its caller's grant.

The release-notes gate checks every pull request into the default branch except Dependabot's. One that carries any of `major`, `minor` or `patch` is release-bound and fails on a violation, also when it carries a second intent label by mistake. One labelled `no-release`, or not labelled yet, gets the same checks as warnings and passes, since any pull request may become releasable; a `no-release` description without a change list is fine. A pull request into another base passes with a notice. The check re-runs when a label is added or the description is edited. It enforces the release-note contract:

- An optional summary, first: either a short `## Summary` section of prose or one lead paragraph without a heading, not both. Then only `## Added`, `## Changed`, `## Fixed`, `## Removed`, `## Security` and `## Deprecated`, in that order, each once and only when it has bullets: prose or a code example alone under a section, such as a `None.` filler, fails, and so does a bullet that only says `None`, `N/A` or `Nothing` (also in bold or italics, with closing punctuation), which is a filler and not a change, so a section holding only such bullets is a section without bullets. `###` sub-headings may group bullets inside a section (there even `### Added` is only a sub-heading; before or between sections a `###` named like a section fails as a wrong-level section).
- A delivered issue is `(#n)` at the end of its bullet (only more issue references, including `(part of #n)`, `(see #n)` and `(Cratis/Repo#n)`, closing emphasis, `<br>` and sentence punctuation may follow; a `:` may introduce nested bullets). Write one `(#n)` per issue: `(#56) (#57)`, never `(#56, #57)`, which closes nothing. `(part of #n)` or `see #n` keeps an issue open, and `Cratis/Repo#n` names another repository. `Closes`, `Fixes`, `Refs` and the other closing or linking keywords never appear, with `#n` or an issue URL, also when wrapped in bold, italics or a link (`**Closes** #n`, `Closes [#n](url)`, `[Closes #n](url)`, and `[Closes](url)` where the link starts a clause). A `(#n)` in the summary, in a heading, in the middle of a bullet or inside an HTML comment fails because release-action would still close it. For a `(#n)` in the middle of a bullet the error quotes the text that follows it (`(#12)` is followed by `, see #11 ...`) and asks for that text to move before the `(#n)`.
- No Overview, Test plan, Verification, Notes or other headings (also written as `<h2>`, `<b>`, `<i>`, `*italic*` or `<summary>`, which may span lines), no review, verification, test-result or provenance notes (which agent or model wrote it, a stand-alone `Reviewed by` line, CI results), no relative links, no template placeholders, no unclosed `<!--` and no Copilot "Original prompt" transcripts. Headings are checked like any other line for keywords, `(#n)`, relative links, placeholders and provenance. Provenance is recognised at the start of a clause (`Reviewed with a cross-provider review.`, `Review: Opus-only review`, `The review workflow passed.`, `Co-Authored-By:`); inside a bullet, a `## Summary` or the lead paragraph the note must also end the clause (`- Cross-provider review pending (#3)`), and `Reviewed by ...` is not read there at all (`- Reviewed by status is now shown on the dashboard (#4)` passes), so a bullet that merely mentions review or validation as a product change, such as `- Validation: rules now apply to commands (#3)` or `- Adds same-provider review routing to the review workflow (#3)`, passes. Reviewer-facing information goes in a pull request comment.
- No verification or scope notes and no reviewer instructions, recognised by their phrasing wherever they stand (a bullet, the summary, the lead paragraph or a line of their own): how or where a change was checked (`Verified by building ...`, `verified in Storybook`, `Tested locally`), what was not (`not yet verified`, `Storybook only`), results (`specs 492/492`, `9 of 9 runs`, `Tier 1 PASS`, `0 warnings, 0 errors`, a `Local:`, `CI:`, `Checks:` or `Checked locally:` line, a `Checks (local)` heading), provenance (`Verified against Arc.TypeScript main (v0.34.0)`, `Tested against a real kernel`; the error asks for compatibility as a fact instead, such as `Requires @cratis/arc 0.34 or later`), and stacking, retargeting, merge or deploy order and draft status (`Stacked on #207`, `Retarget to main`, `This PR should be deployed separately`, `Draft:`, `Do not merge`), reported as a reviewer instruction. The patterns name the phrasing, not the topic: `Events are now verified against their schema`, `Toasts are stacked on top of each other` and `- CI: builds now cache packages (#3)` pass.

Fenced code and inline code are exempt. Closed HTML comments are not shown on the release page, so the heading, keyword, link and placeholder rules ignore them, but a `(#n)` inside a comment still closes the issue and fails, so never put issue references in comments. Each violation is reported as its own error, naming the rule, the offending line and the fix. Whether a summary states a cohesive theme, whether a bullet is user-facing and whether an issue number exists cannot be decided from text; reviewers keep those.

In the legacy release-notes reusable, a second job, `drift`, compares the notes with the diff and only ever warns: a bullet whose code spans (paths and identifiers) the diff no longer touches, because the change landed on main another way or was dropped in conflict resolution; a version the diff only removes; a registry (PyPI, npm, NuGet, Maven Central, Docker Hub, GHCR) that no workflow publishes to; and a merge from the base branch that changed which files the pull request touches. It checks out the pull request head with full history (blobs on demand), skips Dependabot, and runs on label changes too, because the caller's concurrency group cancels the whole run when a label changes and the check would otherwise stay cancelled.

A repository calls the gate with a job named `release-notes`, so the checks read `release-notes / verify` and `release-notes / drift` and do not collide with other `verify / verify` gates. The caller runs on `opened`, `edited`, `reopened`, `synchronize`, `labeled`, `unlabeled` and `ready_for_review`:

```yaml
permissions:
  contents: read
  pull-requests: read

jobs:
  release-notes:
    uses: Cratis/Workflows/.github/workflows/verify-release-notes.yml@main
```

The release-intent caller names its job `release-intent`, so its check reads `release-intent / verify` rather than `verify / verify`, and corrects a Dependabot pull request's labels before the gate:

```yaml
permissions:
  contents: read

jobs:
  dependabot-labels:
    if: github.event.pull_request.user.login == 'dependabot[bot]'
    uses: Cratis/Workflows/.github/workflows/normalize-dependabot-labels.yml@main
    permissions:
      pull-requests: write

  release-intent:
    needs: dependabot-labels
    if: ${{ !cancelled() }}
    uses: Cratis/Workflows/.github/workflows/verify-release-intent.yml@main
    permissions:
      contents: read
      pull-requests: read
```

The optional `runs-on` input (default `ubuntu-latest`) names the runner label, as in `verify-no-work-records.yml`; a caller in a private repository passes its self-hosted scale-set label so the gate does not use billed GitHub-hosted minutes:

```yaml
  release-notes:
    uses: Cratis/Workflows/.github/workflows/verify-release-notes.yml@main
    with:
      runs-on: cratis-arc
```

The pull request template's comment states the same contract; it is propagated to every repository that is not in the template propagation's ignore list, including those where the check is not installed, so it says editing the description re-runs the check "where it is installed". Its placeholder bullets carry no `(#n)`: the comment shows the syntax, and a description that only fills in the placeholders must not arrive with a literal `(#123)`.

The common workflow bootstrap now installs `verify-pull-request-policy.yml` in every
non-archived Cratis repository outside `REPOS_TO_IGNORE`. It removes each old
`verify-release-notes.yml`, `verify-semver-label.yml` and `verify-no-work-records.yml`
caller only when its blob matches a known historical bootstrap payload for that
path; customized callers are kept and logged. Replacement and deletions share one
Git tree and commit, and a failed replacement aborts the update. Ref updates remain
nonforce; a non-fast-forward retries once after re-reading the fresh HEAD and files.

An old semver caller, an existing policy caller with `release-intent: true`, or a
workflow using `Cratis/release-action` enables intent and the normalizer. Detection
errors skip the repository rather than installing a nonreleasing caller. Repositories
that never release receive `release-intent: false`; Arc.Kotlin also receives false
because it retains its pinned semver caller. The canonical policy caller passes
`runs-on: ${{ vars.RUNNER_GATE || 'ubuntu-latest' }}` to the gate. The bootstrap reads each repository's visibility and renders that default per repository: a public repository gets `ubuntu-latest` (its `pull_request` jobs never run on self-hosted runners, per `.cratis/ai/rules/github-actions.md`), a private repository gets `cratis-arc-small`, also for the Dependabot label normalizer, and a repository whose visibility cannot be read is skipped. `RUNNER_GATE` is an outage escape hatch, normally unset: a public repository sets it temporarily to reroute the job when the default runner is unavailable, and deletes it afterwards. It is not how a repository chooses its runner permanently.

A repository that keeps its own copy of one bootstrapped file, and takes every other one, is listed for that file in the script's `SKIP_FILE_REPOS`: Arc.Kotlin keeps its SHA-pinned `verify-semver-label.yml`.

Repositories in `REPOS_TO_IGNORE` never receive the callers automatically and need them added through their own reviewed change. That covers the repositories that hold their own governance (AI, Templates, release-action, Stage, Dockerfiles, and Chronicle.Dapr and Chronicle.Wolverine once they release), the private repositories (Studio, Direct, Ensemble, Infrastructure, Experiments, Strategy, Identity, KeldaKraft, studio-cluster-verification) and the public repositories that customize `update-packages.yml` on purpose (Ante, Chronicle.Elixir, Components, Orleans). Private repositories have no GitHub-hosted Actions budget and run their wrappers on `cratis-arc`, so the bootstrap must not overwrite those wrappers with `ubuntu-latest` ones, and a repository that removed a wrapper on purpose must not get it back. Such a repository copies the canonical caller and gives the gate its own runner fallback, in the same shape as its other wrappers: `runs-on: ${{ vars.RUNNER_GATE || 'cratis-arc' }}`, or, in Ensemble, `runs-on: ${{ vars.ENSEMBLE_RUNNER || 'cratis-arc' }}`. It does not set `RUNNER_GATE` permanently. A new private repository, or a new repository with a deliberate customization, is added to `REPOS_TO_IGNORE` with the reason in the comment beside it (the bootstrap's spec asserts that the private repositories and the customized ones stay listed). AI.Distribution is no longer in use and is ignored by both the bootstrap and template propagation. Arc.TypeScript, Chronicle.Python and the two Eventmodelers build kits were never bootstrapped, so a run would also install every standard wrapper there for the first time; they are held back until that is decided.

The policy specs execute the combined inline programs, verify that the live-read,
notes and drift copies remain byte-identical to the legacy reusable, and cover
head-versus-merge drift, fetch failure and blocking aggregation. Offline bootstrap
specs cover atomic migration, customizations, failed replacement/discovery,
idempotence, release continuity and the bounded non-fast-forward retry.

The check's program is inline in the reusable workflow, so the rules that run are exactly the ones at the ref the caller names. `verify-release-notes-program.yml` runs `.github/scripts/tests/verify-release-notes.spec.mjs` against that exact program, including a corpus of over 900 published Cratis release bodies (`.github/scripts/tests/fixtures/release-notes/corpus.json`) whose recorded rules any rule change must keep or deliberately update, and `.github/scripts/tests/verify-release-intent.spec.mjs` against the release-intent gate and the Dependabot normalizer.

---

## Verifying workflow changes

`smoke-workflows.yml` runs on `pull_request` changes to workflows, actions,
templates, the actionlint configuration/installer, and its tests and fixtures.
It calls reusable workflows through `./.github/workflows/` so GitHub uses the
PR merge commit, not the version on `main`. The lint and public-runner calls also
pass `${{ github.sha }}` as `workflows-ref` for their supporting files. Permissions
are read-only, no secrets are passed, and public PRs use `ubuntu-latest`.
The path filter is intentional: this workflow is **not a required check**.

| Coverage | Reusable workflow | Limit |
| --- | --- | --- |
| Smoke call | `workflow-lint.yml` | All workflows plus the publish template; shellcheck enabled |
| Smoke call | `docker-cleanup-gate.yml` | Read-only scan; its checker checkout still defaults to `main` |
| Smoke call | `check-public-runners.yml` | Read-only scan using the PR merge commit's checker |
| Smoke call | `verify-no-work-records.yml` | Uses its existing immutable script pin |
| Smoke call | `verify-discoverability.yml` | Good HTML fixture at `https://example.cratis.io`; existing script pin |
| Not smokeable | `update-packages.yml`, `cleanup-pr-artifacts.yml` | Secrets and repository writes/deletions |
| Not smokeable | `trigger-documentation-build.yml` | Cross-repository dispatch and a secret |
| Not smokeable | `normalize-dependabot-labels.yml` | Writes labels |
| Not smokeable | `verify-pull-request-policy.yml`, `verify-release-notes.yml`, `verify-release-intent.yml` | Holds Workflows PRs to consumer release policy; decide separately |

`auto-approve-publish-deployments.yml` uses `workflow_run`, not `workflow_call`,
so it is outside the reusable coverage ratchet. Action changes trigger this
workflow, but these smoke calls do not execute composite actions. The offline test
requires every reusable workflow to be called here or have an explicit reason
in `NOT_SMOKEABLE`; adding a reusable without either fails the test.

### Linting locally and in consumers

The shared installer pins actionlint 1.7.12 and verifies the Linux amd64/arm64
archive checksum. `workflow-lint.yml` accepts `runs-on` (default `ubuntu-latest`),
`workflows-ref` (default `main`), `shellcheck` (default `false`), and `extra-files`
(a space-separated list of additional paths). It always lints all caller workflows,
including when extra files are supplied. It prefers the caller's
`.github/actionlint.yaml` or `.github/actionlint.yml`, otherwise uses the shared
configuration. The shared config recognizes Cratis runner labels and ignores
only actionlint's unsupported `concurrency.queue` key under workflows/templates;
remove that exception when actionlint supports it. Pyflakes is disabled.

```bash
actionlint
actionlint .github/templates/publish.template.yml
python3 -B .github/scripts/tests/workflow-lint.test.py
```

The test proves a broken workflow fails with `expression`, `job-needs` and
`syntax-check` rule IDs, and that `queue: max` passes with the shared config.

### Manual fallback for changes smoke cannot exercise

For a reusable that can safely be dispatched with its existing permissions,
use a scratch branch in a consumer such as Arc. Point its caller at
`Cratis/Workflows/.github/workflows/<file>@<your-branch>`, ensure that caller
supports `workflow_dispatch`, push the scratch branch, then run:

```bash
gh workflow run <caller-file> --repo Cratis/Arc --ref <scratch-branch>
```

Dispatch is not a dry run: use only explicitly authorized secrets and effects.
Link the exact run in a **PR comment**, not the release-note description.
Cleanup is an exception: its manual dispatch is main-only and `dry_run` is not a
reusable input. Do not dispatch a scratch cleanup caller (it deletes artifacts).
After the reviewed change lands, use `dry_run: true` in Workflows following
[Retry an already-closed PR safely](#retry-an-already-closed-pr-safely); do not
weaken that main-only boundary to test a branch.

For consumer `pull_request_target` or publish workflows, use actionlint only
before merge and watch the first authorized run on `main`. Smoke calls do not
prove privileged event semantics, publishing, deployments, or secret-backed
writes, and are not a consumer rollout or a bootstrap installation.

## Workflows in this repository

### `cleanup-pr-artifacts.yml`

**Triggers:** `workflow_call` for closed-PR callers; `workflow_dispatch` in Workflows
for an explicitly targeted manual dry run or approved retry.

Deletes only exactly matching, repository-associated container/NuGet versions after
complete preflight, and verifies their absence. HTTP 401/403 from the organization
package inventory reports **could not run: package inventory not authorized** as a
neutral warning and deletes nothing for reusable calls, scheduled runs and dry runs;
a manual apply with an approved manifest fails instead. This is not a completed
cleanup. The optional secret `PACKAGE_CLEANUP_TOKEN` (classic PAT with
`read:packages` and `delete:packages`, package admin, SSO-authorized) takes
precedence over `PAT_WORKFLOWS`. All other errors still fail closed. The reusable
contract remains the required numeric `pull_request` input and `PAT_WORKFLOWS`
secret, plus the optional `PACKAGE_CLEANUP_TOKEN` secret. See
[Cleaning up PR artifacts](#cleaning-up-pr-artifacts) for matching rules, permissions,
manual approval inputs, recovery limitations, and offline verification.

---

### `verify-no-work-records.yml`

A reusable guard that fails when AI session work records are tracked. It applies five rules:

| Rule | Fails when tracked |
|---|---|
| `ai-work` | anything under `.ai-work/` |
| `pi-runtime` | anything under `.pi/delegate/`, `.pi/fusion/`, `.pi/tasks/` or `.pi/*-session-*/`; `.pi/settings.json` and `.pi/extensions/` stay allowed |
| `root-document` | a root-level SCREAMING-CASE `.md` file outside the root allowlist (`README`, `LICENSE`, `AGENTS`, `CLAUDE`, `GEMINI`, `CODE_OF_CONDUCT`, `CONTRIBUTING`, `SECURITY`, `CHANGELOG`, `CREDITS`, `RESOURCES`, `BRAND`, `MESSAGING`, `PAGES`, `SITE`, `PRIVACY_POLICY`, `ROADMAP`, `START-HERE`, `CHRONICLE`, `COMPATIBILITY`, `NOTICE`, `SUPPORT`, `GOVERNANCE`, `VERSION`, `DECISIONS`) |
| `session` | explicit session prefixes at any depth, case-insensitively (`Handover-*`, `PROMPT-*`, `NEXT-SESSION`, `Session-*`, with `-` or `_` separators); legacy embedded all-caps names such as `HANDOVER.md` are also caught; never exempted by a documentation directory. Bare `prompt.md`, `session.md` and `handover.md` remain ordinary product files |
| `work-record-shape` | a `PLAN`, `DESIGN`, `REPORT` or `STATUS` file at any depth (`PLAN-foo.md`, `Plan-foo.md`, `Status.md`), outside the documentation directories and product `templates/` directories |

Decision records are documentation. `decisions/`, `Documentation/`, `docs/`, `Knowledge/`, `evidence/` and `governance/` at the repository root exempt the `work-record-shape` rule, case-insensitively. Product `templates/` directories at any depth also exempt the shape rule (for example Direct's prompt sections), not the session/runtime rules. Files under `.claude/`, `.github/`, `.pi/`, `.agents/` and `.cratis/` are skipped by the name rules. Work-record prefixes match every letter case, including `pLaN-notes.md` and `design-notes.md`, outside the documentation directories.

Inputs:

| Input | Default | Meaning |
|---|---|---|
| `runs-on` | `ubuntu-latest` | Runner label |
| `extra-allowed` | empty | Comma-separated additional root-level file names, with or without `.md`; exempts the root-document and work-record-shape rules, not session/runtime rules |
| `extra-allowed-paths` | empty | Comma-separated additional documentation directory prefixes, such as `product-docs/` |

Existing `@main` callers retain their root exemptions. `evidence/` and `governance/`
are documentation by default, so Strategy's reviewed documents require no coordinated
new-input rollout. Other documentation directories can use `extra-allowed-paths`
after the reusable workflow exposing that input is available.

Callers must include `.pi/**` in both `pull_request.paths` and `push.paths` for the
`pi-runtime` rule to run on runtime-only changes; updating the reusable workflow
alone does not update caller triggers. Keep their branch filters, runner and other
settings unchanged. For each event's paths, use:

```yaml
paths: ["**.md", ".ai-work/**", ".pi/**"]
```

Without `.pi/**`, runtime-only changes do not run the guard. Updating the bootstrap
caller template is an organization-wide rollout and is reviewed separately; no
fleet-wide writes are performed by this change or by the checker itself.

Exit codes: `0` ran and found nothing, `1` ran and found violations (one line per file with the rule id), `2` could not run (`git ls-files` failed or listed no files). A green run prints `scanned: <n> tracked files, rules: 5, violations: 0`.

The checker is `.github/scripts/verify-no-work-records.mjs`. The reusable workflow downloads it from an immutable Workflows commit into the runner's temporary directory and runs it over the caller's checkout; download or prerequisite failure exits `2`. The downloaded script's SHA-256 must match the workflow's reviewed checksum before execution. When changing the script, commit it first and update the workflow's script pin and checksum. Offline tests compare the checksum with the tested source without needing the script commit in a shallow or squash-merged checkout.

Run `node .github/scripts/verify-no-work-records.mjs --self-test` to plant one defect per rule in temporary repositories and check the exit codes and clean counts; `VERIFY_SELF_TEST_BREAK=1` makes the self-test fail. `verify-workflow-programs.yml` runs `.github/scripts/tests/verify-no-work-records.test.py` on every guard change, including the self-test, mixed-case fixtures, NUL-delimited filenames, listing failures and the pinned workflow wrapper.

---

### `verify-discoverability.yml`

A reusable check that reads a built static site and reports search (SEO), answer-engine (AEO) and generative (GEO) discoverability findings in the job summary and as workflow annotations. It is warn-only by default; set `fail-on: error` to fail the job on error-level findings.

Per HTML page (pages with `noindex` or a meta-refresh redirect are skipped):

| Check | Level |
|---|---|
| `<html lang>` present | error |
| exactly one non-empty `<title>`; more than 60 characters | error; warning |
| meta description present; more than 160 characters | warning |
| `<link rel="canonical">` is an absolute URL on the `base-url` host | error |
| every `application/ld+json` block parses as JSON | error |
| `og:image` present, with `og:image:alt` | warning |

Per site: `robots.txt` exists and has a `Sitemap:` line (error); `sitemap.xml` or `sitemap-index.xml` exists, and every URL in it (following sitemap indexes) is on the `base-url` host and maps to a built page (error); `llms.txt` exists (warning).

Inputs:

| Input | Default | Meaning |
|---|---|---|
| `site-path` | required | Directory holding the built site, relative to the repository root |
| `base-url` | required | Production origin, for example `https://blog.cratis.io` |
| `fail-on` | `none` | `none` reports only; `error` fails when any error-level finding exists |
| `ignore` | empty | Comma- or newline-separated globs relative to `site-path` (`*` and `?` stay in one segment, `**` crosses directories), for example `404.html,drafts/**` |
| `build-command` | empty | Command that builds the site into `site-path`; leave empty for plain-HTML or prebuilt sites |
| `node-version` | `24` | Node.js version for the build and the check |
| `runs-on` | `ubuntu-latest` | Runner label; private repositories pass their scale-set label |

Caller:

```yaml
name: Verify Discoverability

on:
  pull_request:

permissions:
  contents: read

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  discoverability:
    uses: Cratis/Workflows/.github/workflows/verify-discoverability.yml@main
    with:
      site-path: dist
      base-url: https://blog.cratis.io
      build-command: npm ci && npm run build
      ignore: 404.html
      # fail-on: error   # once the site is clean
```

Exit codes of the checker: `0` no failing findings, `1` errors with `fail-on: error`, `2` could not run (bad arguments, missing site directory, download or checksum failure).

The checker is `.github/scripts/verify-discoverability.mjs`, Node built-ins only. Like the work-record guard, the reusable workflow downloads it from an immutable Workflows commit and verifies its SHA-256 before running it; when changing the script, commit it first and update the workflow's pin and checksum. `verify-discoverability-check.yml` runs `.github/scripts/tests/verify-discoverability.test.py` on every change: it checks a good and a bad fixture site (`.github/scripts/tests/fixtures/discoverability/`), the summary output, `fail-on`, ignore globs, the exit codes, and the pinned download step.

---

### `verify-release-concurrency.yml`

**Triggers:** `pull_request` and `push` to `main` when the checker, its tests and
fixtures, the publish template or the workflow change; weekly `schedule`; and
`workflow_dispatch`.

Fails when a Cratis publish, release or deploy workflow can cancel a release or drop
a pending one. Pull requests and pushes run
`.github/scripts/tests/verify-release-concurrency.test.py` (fixtures and the publish
template) and the checker's `--self-test`. The schedule and manual runs sweep every
non-archived Cratis repository's default branch with `PAT_WORKFLOWS` (read only).

The checker, `.github/scripts/verify-release-concurrency.py`, looks at workflow files
whose name contains `publish`, `release` or `deploy`. It skips `verify-*`,
`auto-approve-*` and names containing `release-notes`, `release-intent` or
`documentation` (a documentation workflow only triggers a site build, where the newest
build covers the ones before it). A file whose `on:` lists `workflow_call` and no other
trigger is exempt from every check, because it can only run as a reusable piece inside
its caller's group; a file that also has its own trigger (for example `workflow_call`
plus `workflow_dispatch`) is checked. For each remaining file:

- any `cancel-in-progress` at workflow or job level that is not literally `false` is a
  violation, including an expression;
- a publish or release workflow (name not starting with `deploy`) must queue its
  pending runs. GitHub applies the workflow-level group before any job group, so a
  workflow-level concurrency block needs `queue: max` itself, unless its group is unique
  per run or commit (it contains `github.run_id` or `github.sha`); then a job's
  concurrency needs `queue: max`. Without a workflow-level block, a job's concurrency
  needs `queue: max`. Other job groups without a queue (for example a latest-wins deploy
  job) are fine. Deploy workflows need no queue: deploying the newest commit covers the
  ones before it.

A workflow that deliberately keeps a single pending slot is listed in the script's
`ALLOWLIST` with its repository, file and reason (today: Studio's `publish.yml`, which
releases every unreleased pull request in one run, and Arc's `chronicle-published.yml`,
a Chronicle dependency bump with one group per version that releases nothing itself). The exemption covers only the
missing `queue: max`, never cancelling, and the sweep fails an entry once its file no
longer needs it.

Run it with `--dir <workflows directory> [--repo <name>]`, `--org` (needs `GH_TOKEN`) or
`--self-test`. Exit codes: `0` ran clean, `1` found violations (one
`<repository>/<file>: <reason>` line each), `2` could not run (GitHub API error, no
repositories, no workflow files or release subjects to check, or a release workflow
that does not parse). A clean run prints
`checked <n> repositories, <m> workflow files, <k> release subjects, 0 violations`.

---

### `check-batched-releases.yml`

**Triggers:** `pull_request` and `push` to `main` when the check, the cron helper, their
tests, the publish template or the workflow change; hourly `schedule`; and
`workflow_dispatch`.

Fails when a repository that releases in [batches](#batched-releases) has releasable
merges waiting too long. Pull requests and pushes run
`.github/scripts/tests/batched-releases.test.py` (the template's opt-in block, the cron
helper and the staleness decision) and the check's `--self-test`. The schedule and manual
runs sweep every non-archived Cratis repository with `PAT_WORKFLOWS` (read only, no
writes or dispatches).

The check, `.github/scripts/check-batched-releases.py`, looks for repositories whose
default-branch `.github/workflows/publish.yml` sets `release-window:` above 0. For each it
lists the pull requests labelled `major`, `minor` or `patch` merged after the latest
non-draft release. If the oldest is older than the window plus 60 minutes it prints an
`::error::` naming the repository, the pull requests and the age; otherwise it prints one
summary line. Exit codes: `0` ran clean, `1` found a stale repository, `2` could not run
(GitHub API error, or no repository opts in, so nothing was checked).

---

### `verify-public-runners.yml` and `check-public-runners.yml`

Fails when a public repository's `pull_request` or `pull_request_target` job resolves to a
self-hosted runner (`self-hosted`, `cratis-arc*`, or a runner group), because any fork could then
run code on our infrastructure. The checker, `.github/scripts/verify-public-runners.py`, follows
literal `runs-on`, `${{ vars.X || 'label' }}` and `${{ inputs.X || 'label' }}` fallbacks, `matrix`
lists and reusable workflows (`./` and `Cratis/<repo>/...@ref`) with the caller's `with:` inputs and
the callee's input defaults. An expression it cannot resolve is reported as `unresolved runner` and
fails: unknown is not pass. Private repositories are never reported.

`verify-public-runners.yml` runs the checker's tests (fixtures, the bootstrap's rendered public
callers, this repository's own workflows) and `--self-test` on pull requests and pushes, and sweeps
every public Cratis repository weekly with `PAT_WORKFLOWS`, also treating a set variable such as
`RUNNER_GATE` that points at a self-hosted label as a finding. Other repositories can call
`check-public-runners.yml@main`, which checks their own workflows when the repository is public.
The reusable gate (`--dir` mode) does not read repository, organization or environment variables:
it treats `vars.X` as unset and uses the fallback, so a set `RUNNER_GATE` is caught only by the weekly
sweep (which reads repository and organization variables, not environment-level ones). Set
`workflows-ref` to the same ref the caller pins for `check-public-runners.yml`, so the checker and the
reusable workflows it follows match.

Run it with `--dir <workflows directory> [--repo <name>] [--visibility public|private]
[--local-repo Workflows=<directory>] [--fetch-remote]`, `--org` (needs `GH_TOKEN`) or `--self-test`.
Exit codes: `0` ran clean, `1` found violations, `2` could not run.

---

### `verify-docker-cleanup.yml`

**Triggers:** `pull_request` and `push` to `main` when the checker, its tests and
fixtures, the workflow or `docker-cleanup-gate.yml` change; weekly `schedule`; and
`workflow_dispatch`.

Fails on Docker cleanup that is not scoped to a run. Each `cratis-arc` job has its own
Docker daemon, but an unscoped prune inside a job still removes containers that parallel
steps or Testcontainers in the same job need (Cratis/StudioIssues#905). Pull requests and
pushes run `.github/scripts/tests/verify-docker-cleanup.test.py` and the checker's
`--self-test`. The schedule and manual runs clone every non-archived Cratis repository
shallowly with `PAT_WORKFLOWS` (used read-only; the token itself has write access) and scan it.

The checker, `.github/scripts/verify-docker-cleanup.py`, reads workflow files, composite
actions, `.sh .bash .ps1 .py .mjs .js .ts .cs` scripts, `Dockerfile*`, `Makefile` and
`justfile` by logical line (comments ignored). It fails on `docker system|container|image|
volume|network|builder|buildx prune` without `--filter label=...`, and on `docker rm|kill|
stop|rmi` (or `volume rm`, `network rm`) fed by `$(docker ps|ls|images ...)` or
`| xargs` without `--filter label=...` on the listing. `label!=` and `until=` do not scope
a cleanup. A deliberate exception goes in the script's `ALLOWLIST` with its reason; the
sweep fails an entry once its file no longer has a violation.

Another repository gates its own checkout by calling the reusable
`docker-cleanup-gate.yml` (a reusable workflow sees the caller's event, so the gate is its
own file, with only a `workflow_call` trigger). The caller names its job `docker-cleanup`,
so the check reads `docker-cleanup / gate`:

```yaml
on:
  pull_request:

permissions:
  contents: read

jobs:
  docker-cleanup:
    uses: Cratis/Workflows/.github/workflows/docker-cleanup-gate.yml@main
```

The gate checks out the calling repository and `Cratis/Workflows` (into
`.cratis-workflows`, excluded from the scan) and runs the checker with `--root .`; the
optional `runs-on` input (default `ubuntu-latest`) names the runner label. Exit codes: `0` clean, `1` violations
(`<repository>:<path>:<line>: <reason>` each), `2` could not run (a repository that cannot
be cloned, no repositories, or no files to scan). A clean run prints
`Scanned <n> files in <m> repositories, found 0 violations`.

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

A run does not start every push-triggered workflow in every repository at once
([#163](https://github.com/Cratis/Workflows/issues/163)):

- **Canary.** The first repository that needs a change is pushed normally and keeps
  its CI run. Repositories named in `CANARY_PREFERENCE` (default `Fundamentals`) are
  tried first, so the canary is the first of them that needs a change, or else the
  first repository that needs one. Before pushing it, the run checks that the PAT
  can list that repository's Actions runs and stops with exit code 1, before any
  push, when it cannot. The run then waits for the workflow runs GitHub starts for
  the canary's commit. They pass only once `CANARY_SETTLE_SECONDS` have elapsed
  since the push and two consecutive reads show the same completed runs, because
  GitHub creates a commit's runs over several seconds. It stops with exit code 1, before any other
  repository is changed, when one of them ends with a conclusion other than
  `success`, `skipped` or `neutral`, when they are not complete after
  `CANARY_TIMEOUT_SECONDS` (default 2700), or when the runs cannot be read three
  times in a row. A canary that starts no run within `CANARY_SETTLE_SECONDS`
  (default 300) is logged and the rollout continues. Reading the runs needs the PAT's
  **Actions: Read** permission.
- **`[skip ci]` on the rest.** Every later bootstrap commit touches only the
  bootstrapped wrapper files and ends with ` [skip ci]`, an approved exception to
  the rule against `[skip ci]` on bot commits: the canary's CI run is the check that
  sees a merged result. Package-update and other bot commits are unchanged.
- **Stagger.** After each push, the next repository waits `STAGGER_SECONDS`
  (default 20) before its HEAD is read. A run with nothing to change waits for no
  canary.

The workflow sets none of these, so the script's defaults apply; they are
environment variables of `.github/scripts/bootstrap-common-workflows.sh`.
`python3 -B .github/scripts/tests/bootstrap-rollout.test.py` exercises the rollout
offline with a fake `gh` and a recording `SLEEP_CMD`.

---

### `propagate-pr-templates.yml`

**Trigger:** `push` to `main` (when template files change) or `workflow_dispatch`

Propagates the Pull Request and Issue templates from this repository (`Cratis/Workflows`) directly to the default branch of every other non-archived Cratis repository. Silently skips repositories where files are already up to date.

**Excluded repositories:** `Workflows`, `cratis.github.io`, `StudioIssues`, `Dockerfiles`, `.github`, `cratis.studio`, and the repositories that keep a deliberately customized pull request template (Arc, Arc.Kotlin, Arc.TypeScript, Chronicle, Chronicle.Dapr, Chronicle.Kotlin, Chronicle.Python, Chronicle.Wolverine, Prompter, Scene, Screenplay, Screenplay.CritterStack, Screenplay.Generation, Strategy, Studio, Synopsis). The push replaces a template that differs, so a repository whose template has content beyond a version of the org template is added to `REPOS_TO_IGNORE`, with the reason in the comment beside it. Excluding a repository skips it entirely, so these repositories also stop receiving issue-template updates. `.github` is the organization default template (`Cratis/.github`), shown in repositories without their own; it is kept in step by a pull request in that repository. `AI.Distribution` is excluded because it is no longer in use.

**Secrets required:** `PAT_WORKFLOWS` — classic PAT with `repo` scope, or fine-grained PAT with **Contents** read/write + **Metadata** read. The PAT owner must be a bypass actor on each target repository's branch protection ruleset.

---

## Part of the Cratis ecosystem

These workflows power CI/CD across the [Cratis](https://github.com/Cratis) open-source ecosystem — [Chronicle](https://github.com/Cratis/Chronicle) (event sourcing database and runtime), [Arc](https://github.com/Cratis/Arc) (CQRS for ASP.NET Core), [Components](https://github.com/Cratis/Components) (React), the [CLI](https://github.com/Cratis/cli), and more. Documentation lives at [cratis.io](https://www.cratis.io).
