# Workflows — project context

Organization-owned reusable GitHub Actions workflows for the Cratis
organization. Not an application: no product code, no `.ai/` corpus — the
reusable workflows and their scripts are the product. Runtime validation
rejects calls from outside the Cratis organization.

## What lives here

| Path | Holds |
| --- | --- |
| `.github/workflows/` | the reusable (`workflow_call`) and organization-wide workflows |
| `.github/scripts/` | the scripts those workflows run, plus `update-ai-profile-subscription.mjs` (the subscription update controller) |
| `.github/scripts/tests/` | script tests wired into `verify-*` workflows |

Key workflows: `publish.template.yml` (publish template), `cleanup-pr-artifacts`,
`auto-approve-publish-deployments`, `bootstrap-common-workflows` (installs the
common wrappers organization-wide), `propagate-pr-templates`, `verify-*` gates,
and `update-ai-profile-subscription.yml` (reviewed Cratis AI profile updates —
see README's *Reviewed Cratis AI profile updates*).

The legacy Copilot synchronization system (all-to-all propagation,
per-repository sync wrappers, corpus bootstrap, propagation control) is
**retired and removed**. Cratis repositories carry the `.cratis/` AI contract
instead (see README's *AI setup in Cratis repositories*).

## Conventions

- Shell scripts under `.github/scripts/` are bash with `set -euo pipefail`;
  test what can be tested under `.github/scripts/tests/`.
- Organization-wide write workflows (bootstrap, PR-template propagation) push
  only through `PAT_WORKFLOWS` owned by a dedicated service account configured
  as a bypass actor; changes to watched files need explicit review before
  merge.
- Never commit secret values; workflows reference organization secrets
  (`PAT_WORKFLOWS`, `PAT_DOCUMENTATION`) by name only.

## Local AI work artifacts — `.ai-work/` only

AI-assisted sessions produce working artifacts: plans, handover documents,
session notes, continuation prompts, status boards, scratch analyses. These are
**work records, not documentation**:

- Create every such artifact inside **`.ai-work/`** at the repository root —
  never at the root itself, never under documentation folders, never anywhere
  else.
- `.ai-work/` is gitignored and must stay untracked; never `git add -f`
  anything inside it.
- A genuine follow-up that must survive the session becomes a GitHub issue,
  not a planning file. Knowledge that must outlive the session belongs in this
  repository's documentation through normal review.

## AI-assisted development

`.cratis/ai.json` records this repository's profile subscription
(`cratis/documentation` + `cratis/engineering/core`). Shared behavior arrives
via the Cratis AI marketplace plugins — see the
[harness guide](https://www.cratis.io/ai/harnesses/). General improvements are
proposed in `Cratis/AI`; repository-specific facts stay in this file.
