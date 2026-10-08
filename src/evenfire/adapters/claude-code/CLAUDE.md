# Evenfire skills (Claude Code)

This overlay bundles four Agent Skills for building on the Evenfire/Clerum platform.
Each lives under `.claude/skills/` and Claude loads one when its description matches
the task:

- `create-evenfire-plugin` — design, wire, validate and operate a WorkflowRecipe plugin
  (Desktop UI, backend, database, MCP, Plugin Workload SDK, webhooks, OAuth, workflows).
- `create-evenfire-mcp-server` — build or wrap an MCP server/connector.
- `publish-evenfire-plugin` — publish a recipe or connector to the org registry, version
  it, and install it.
- `run-debug-evenfire` — bring up the stack (minikube or dev cluster) and debug a recipe,
  MCP server, sandbox UI, webhook, or workflow run.

## Working with these skills

- The run/debug and build commands assume the Evenfire platform monorepo is checked out;
  discover its path rather than assuming a fixed location.
- The skills reference platform code paths relative to that monorepo (for example
  `charts/clerum-crds/crds/`); when a value is not in a skill, read the cited source.
- These skills document how to author, publish, and operate plugins. They do not
  themselves authorize deploys or registry mutations — run those only when asked.
