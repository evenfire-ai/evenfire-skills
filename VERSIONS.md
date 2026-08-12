# Versions

These skills are pinned to a specific Evenfire revision. Every field name, enum,
endpoint, label, and command in them was verified against that revision's source.
When the platform moves, re-verify against the new revision and bump this file.

## Current

| Item | Value |
|---|---|
| Evenfire branch | `dev` (github.com/evenfire-ai/evenfire) |
| Commit | `21d9a7d5d7bc4158c5d2685a045439535eb593d7` |
| Commit date | 2026-08-11 |
| `git describe --tags` | `v0.3.0-915-g21d9a7d5d` |
| CRD API group/version | `clerum.io/v1alpha1` |
| `clerum-crds` Helm chart | `0.7.0` (appVersion `1`) |
| Verified on | 2026-08-11 |

The `v0.9.x` and `release/stable/*` tags live on divergent release branches and
are not ancestors of `dev`; the `dev` line's own nearest tag is `v0.3.0`, so
`git describe` on the pinned commit reports `v0.3.0-915-g21d9a7d5d`.

## How each skill records its pin

Every `SKILL.md` carries a "Verified against Evenfire" line under its title with
the commit above. Bump the line and this table together when re-verifying.

## Re-verifying after a platform bump

1. `git fetch` the Evenfire repo and check out the new `dev` commit into a clean
   worktree.
2. Re-run the checks in each skill against that worktree: CRD fields and enums
   (`charts/clerum-crds/crds/*.yaml`), the reconciler and control-api routes, the
   registry publish contract, and the Makefile targets.
3. Update the commit, date, `git describe`, and chart version here and in each
   `SKILL.md` header.
