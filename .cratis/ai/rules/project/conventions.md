---
applyTo: "**/*"
---

## Conventions

- Shell scripts under `.github/scripts/` are bash with `set -euo pipefail`;
  test what can be tested under `.github/scripts/tests/`.
- Organization-wide write workflows (bootstrap, PR-template propagation) push
  only through `PAT_WORKFLOWS` owned by a dedicated service account configured
  as a bypass actor; changes to watched files need explicit review before
  merge.
- Never commit secret values; workflows reference organization secrets
  (`PAT_WORKFLOWS`, `PAT_DOCUMENTATION`) by name only.
