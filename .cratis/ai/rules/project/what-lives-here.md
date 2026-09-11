---
applyTo: "**/*"
---

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
