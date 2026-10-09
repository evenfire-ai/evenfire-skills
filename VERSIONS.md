# Versions

These skills are pinned to the Evenfire revision they were verified against.
Every field name, enum, endpoint, label, and command in a skill was checked
against its pinned source. When the platform moves, re-verify against the new
revision and update the pin.

## Current

| Skill | Evenfire source | Commit | Verified on |
|---|---|---|---|
| `create-evenfire-plugin` | [evenfire-ai/evenfire](https://github.com/evenfire-ai/evenfire) `dev` | `0b26101eb247adcc44f70d39451f3e82c3225d47` | 2026-10-08 |
| `create-evenfire-mcp-server`, `publish-evenfire-plugin`, `run-debug-evenfire` | the platform's earlier, pre-open-source history (not in the public repository) | `f9e8d0487ba9d93cb4429dfcbc306f2f75af7538` | 2026-08-12 |

Both revisions use CRD API `clerum.io/v1alpha1` and the `clerum-crds` Helm chart
`0.8.0` (appVersion `1`).

## How each skill records its pin

Every `SKILL.md` carries a "Verified against Evenfire" line under its title with
its commit. Update that line and this table together.

## Re-verifying after a platform bump

1. `git fetch` the Evenfire repository and check out the new `dev` commit into a
   clean worktree.
2. Re-run the checks in each skill against that worktree: CRD fields, enums and
   CEL rules (`charts/clerum-crds/crds/*.yaml`), the cluster admission policies
   (`deploy/base/cluster-wide/`), the recipe controller (`workflow-recipes/src/`),
   Control API routes, rpc-proxy and the Desktop app for the embed contract, the
   registry publish contract, and the Makefile targets.
3. For `create-evenfire-plugin`, also validate
   `assets/recipe-ui-plugin.yaml` against the new CRD (`kubectl-validate
   --local-crds`), run `scripts/recipe-preflight.py --crd` on it, and rebuild
   the container templates in `assets/`.
4. Update the commit here and in each re-verified `SKILL.md` header. In
   `create-evenfire-plugin`, the CRD download URL in `SKILL.md` section 9 and
   `references/operate.md` carries the full commit too.
