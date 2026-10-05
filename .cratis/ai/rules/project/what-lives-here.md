---
applyTo: "**/*"
---

## What lives here

| Path | Holds |
| --- | --- |
| `.github/workflows/` | the reusable (`workflow_call`) and organization-wide workflows |
| `.github/templates/` | incomplete workflow examples to copy and customize in consuming repositories |
| `.github/scripts/` | the scripts those workflows run, plus `update-ai-profile-subscription.mjs` (the subscription update controller) |
| `.github/scripts/tests/` | script tests wired into `verify-*` workflows |

The publish example lives at `.github/templates/publish.template.yml`, outside the
executable workflow directory. Neither bootstrap nor PR-template propagation
consumes it; repositories copy and customize it manually.

Key workflows: `cleanup-pr-artifacts`,
`auto-approve-publish-deployments`, `bootstrap-common-workflows` (installs the
common wrappers organization-wide), `propagate-pr-templates`, `verify-*` gates
(including `verify-bootstrap-schedules.yml` for deterministic weekly package-update
schedules), and `update-ai-profile-subscription.yml` (reviewed Cratis AI profile updates —
see README's *Reviewed Cratis AI profile updates*).

The legacy Copilot synchronization system (all-to-all propagation,
per-repository sync wrappers, corpus bootstrap, propagation control) is
**retired and removed**. Cratis repositories carry the `.cratis/` AI contract
instead (see README's *AI setup in Cratis repositories*).
