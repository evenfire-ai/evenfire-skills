# Versions

These skills are pinned to a specific Evenfire revision. Every field name, enum,
endpoint, label, and command in them was verified against that revision's source.
When the platform moves, re-verify against the new revision and bump this file.

## Current

| Item | Value |
|---|---|
| Evenfire branch | `dev` (github.com/evenfire-ai/evenfire) |
| Commit | `f9e8d0487ba9d93cb4429dfcbc306f2f75af7538` |
| CRD API group/version | `clerum.io/v1alpha1` |
| `clerum-crds` Helm chart | `0.8.0` (appVersion `1`) |
| Verified on | 2026-08-12 |

## How each skill records its pin

Every `SKILL.md` carries a "Verified against Evenfire" line under its title with
the commit above. Bump the line and this table together when re-verifying.

## Re-verifying after a platform bump

1. `git fetch` the Evenfire repo and check out the new `dev` commit into a clean
   worktree.
2. Re-run the checks in each skill against that worktree: CRD fields and enums
   (`charts/clerum-crds/crds/*.yaml`), the reconciler and control-api routes, the
   registry publish contract, and the Makefile targets.
3. Update the commit and chart version here and in each `SKILL.md` header.
