---
applyTo: "**/*"
---

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
