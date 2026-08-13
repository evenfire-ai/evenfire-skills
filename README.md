# evenfire-skills

Agent skills for building on Evenfire, the Kubernetes-native platform for LLM
orchestration with MCP integration and sandboxed plugins. Each skill is a
self-contained set of instructions (plus a few tested helper scripts) that an
agent loads to do one job correctly, with the exact fields, enums, endpoints, and
commands the platform actually uses.

Everything here is verified against the live platform code (the `clerum.io` CRDs,
`workflow-recipes`, `host-context-controller`, `control-api`, `rpc-proxy`,
`mcp-host`). The skills state real field names and values, not approximations, and
call out the traps that fail silently.

This repository follows the [Agent Plugins](https://agent-plugins.org/) v1.0.0
standard: one provider-neutral source of truth is built into portable and
per-provider distributions.

**Verified against Evenfire `dev` at commit `f9e8d0487`, CRD API
`clerum.io/v1alpha1`, `clerum-crds` chart `0.8.0`.** Each `SKILL.md` records the
same pin in its header; see [VERSIONS.md](VERSIONS.md) for how to re-verify after a
platform bump.

## Skills

| Skill | Use it when you need to |
|---|---|
| [create-evenfire-plugin](src/evenfire/skills/create-evenfire-plugin/SKILL.md) | Build a plugin end to end: a WorkflowRecipe with a sandbox web UI, a credentialed backend, an optional MCP server and database, webhooks, OAuth, or an agentic/snippet workflow. The credential boundary, the multi-service repo scaffold, every recipe field and enum, egress, validation rules, and the build-and-deploy loop. |
| [create-evenfire-mcp-server](src/evenfire/skills/create-evenfire-mcp-server/SKILL.md) | Build, test, and package an MCP server (a local image or a remote wrapper): the server code contract, Dockerfile hardening, the `registry.json` schema, the naming rule, local handshake testing, how the platform deploys it, and how to attach it to a chat agent. |
| [publish-evenfire-plugin](src/evenfire/skills/publish-evenfire-plugin/SKILL.md) | Publish a recipe or an MCP-server connector to the org registry, keep it private, install it, and manage versions: `efrk_` keys, image push, the exact publish payload, the imageRef-equals-name rule, and version retirement. |
| [run-debug-evenfire](src/evenfire/skills/run-debug-evenfire/SKILL.md) | Bring up a stack (minikube or dev GKE), launch the desktop app, and debug a broken recipe, MCP server, UI, webhook, OAuth, or workflow run: the allowed contexts, the make targets, the port map, and a pod-level failure playbook. |

The skills reference each other: author with `create-evenfire-plugin` or
`create-evenfire-mcp-server`, ship with `publish-evenfire-plugin`, operate and
debug with `run-debug-evenfire`.

## Repository layout

The source is provider-neutral; the client distributions are generated (and
git-ignored):

```
src/evenfire/
  skills/<skill>/SKILL.md        provider-neutral Agent Skills (the source of truth)
  adapters/openai/skills.json    OpenAI/Codex per-skill interface metadata
  adapters/claude-code/CLAUDE.md standing Claude Code guidance
schemas/agent-plugins/1.0.0/     vendored Agent Plugins 1.0.0 manifest schema
scripts/                         build + validate tooling
.agents/plugins/marketplace.json points Codex at the generated OpenAI package
dist/                            GENERATED, git-ignored (see below)
```

## Build and validate

The build fans the single source out into three distributions under `dist/`
(git-ignored):

- `dist/agent-plugins/evenfire-skills/` — a strict Agent Plugins 1.0.0 portable
  package: `plugin.json` + `skills/`, nothing client-specific.
- `dist/openai/evenfire-skills/` — an OpenAI/Codex package (`.codex-plugin/` +
  per-skill `agents/openai.yaml`).
- `dist/claude-code/evenfire-skills/` — a Claude Code overlay (`CLAUDE.md` +
  `.claude/skills/`).

```bash
python3 scripts/build_evenfire_plugins.py            # regenerate dist/
python3 scripts/build_evenfire_plugins.py --check    # verify deterministic, no drift
python3 scripts/validate_evenfire_plugins.py         # full contract: schema, parity, containment
```

The build is deterministic (byte-stable), copies files without following
symlinks, and never leaves generated files tracked. The validator schema-checks
the portable manifest against the pinned 1.0.0 schema, confirms every Markdown
reference resolves inside its skill, and asserts portable/OpenAI/Claude parity
with the source.

## Bundled helper scripts

Each is runnable on its own and has been exercised against a live system:

- `create-evenfire-mcp-server/scripts/mcp-smoke.sh <url> [tool] [json-args]` —
  drives the MCP StreamableHTTP handshake (`initialize` then `tools/list` then an
  optional `tools/call`) against any server URL.
- `publish-evenfire-plugin/scripts/publish-entry.sh ... [--dry-run]` — assembles
  and POSTs a registry entry; `--dry-run` prints the exact payload with no network.
- `run-debug-evenfire/scripts/evenfire-doctor.sh <context> [recipe-base-name]` —
  a read-only cluster health sweep that never mutates anything.

They need `bash`, `curl`, and `jq`; the doctor also needs `kubectl`.

## Using these skills

Build the distributions, then point your client at the one it understands:

- Any Agent Plugins v1.0.0 client: install `dist/agent-plugins/evenfire-skills/`.
- OpenAI/Codex: the repo `.agents/plugins/marketplace.json` resolves the generated
  `dist/openai/evenfire-skills/` package.
- Claude Code: copy a skill into a skills path your agent reads, for example
  `mkdir -p ~/.claude/skills && cp -r dist/claude-code/evenfire-skills/.claude/skills/run-debug-evenfire ~/.claude/skills/`.

Each `SKILL.md` carries a `name` and a `description`; the agent uses the
description to decide when the skill applies, so invoking is a matter of asking
for the task the description covers (or naming the skill directly).
