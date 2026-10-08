---
name: create-evenfire-plugin
description: >-
  Build an Evenfire plugin end to end: a WorkflowRecipe that runs a web UI
  inside the Evenfire Desktop app, a backend that holds the credentials, a
  database, an MCP server for chat agents, desktop notifications and LLM calls
  through the Plugin Workload SDK, webhooks, OAuth, and agentic or snippet
  workflows. Use when designing, scaffolding, writing, containerizing,
  validating, installing, updating or debugging a plugin or WorkflowRecipe, or
  when you need the exact recipe fields, namespaces, network rules, Secret
  ownership labels, the Desktop embed contract, or why a recipe is failed,
  degraded or silently broken.
---

# Building an Evenfire plugin

> **Verified against Evenfire `dev` at commit `0b26101eb`**
> ([evenfire-ai/evenfire](https://github.com/evenfire-ai/evenfire), 2026-10-08):
> CRD `clerum.io/v1alpha1`, `clerum-crds` chart `0.8.0`. Every rule here was
> checked in that source, and the example recipe and container templates were
> run or validated. Each reference file names the source paths it follows.

A plugin is one Kubernetes object, `kind: WorkflowRecipe`. The platform's
recipe controller (WRC) turns it into Deployments, StatefulSets, Services,
NetworkPolicies, an MCP server registration, a webhook gateway, and workflow
runs. Users open the plugin's web UI as an app inside the Evenfire Desktop;
chat agents use its MCP server; operators install it from a registry and grant
access.

Never invent a field, value, endpoint or label. When something is not in this
skill, read the CRD (`charts/clerum-crds/crds/workflowrecipe.yaml`) and the
controller (`workflow-recipes/src/`) in the platform repository.

## How to use this skill

1. Choose the shape (section 1). It decides most of what follows.
2. Lay out the repository and images (section 3) from the files in `assets/`.
3. Write the recipe, starting from
   [assets/recipe-ui-plugin.yaml](assets/recipe-ui-plugin.yaml) (section 4).
4. Wire addresses and network (5), Secrets (6) and the UI (7).
5. Add only the capabilities you need (section 8).
6. Validate, install and verify (section 9), then run the checklist (11).

| Reference | Read it for |
|---|---|
| [references/recipe-fields.md](references/recipe-fields.md) | Every field, limit, template and status value, and the admission rule codes |
| [references/networking.md](references/networking.md) | Addresses, NetworkPolicies, egress, bindings, why a call hangs |
| [references/secrets-and-images.md](references/secrets-and-images.md) | `envSecret`, ownership labels, reserved names, images and pull credentials |
| [references/ui-embed.md](references/ui-embed.md) | What the UI pod receives, CSP, sessions, `window.clerum`, deep links, OAuth in the UI |
| [references/plugin-workload-sdk.md](references/plugin-workload-sdk.md) | Desktop notifications and LLM calls from the backend: recipe, API, grants, status |
| [references/webhooks-and-oauth.md](references/webhooks-and-oauth.md) | Inbound webhooks and OAuth clients |
| [references/workflows.md](references/workflows.md) | `steps`, triggers, snippet `sdk.*`, built-in tools, outputs |
| [references/operate.md](references/operate.md) | Validation layers, install paths and names, verifying, updating, recovering, messages |

Other skills: `create-evenfire-mcp-server` (the MCP server code and attaching it
to chat agents), `publish-evenfire-plugin` (registry, versions, images),
`run-debug-evenfire` (clusters and debugging).

## 1. Choose the shape first

The presence of `spec.steps` changes how the whole recipe behaves.

| You need | Shape | What you get |
|---|---|---|
| A UI app with a backend, database or MCP server | `workloads` only, **no `steps`** | Webhooks work; editing the recipe rolls out new images; `includeWhen` works |
| The same, plus Desktop notifications or LLM calls from the backend | add `spec.pluginWorkloadSdk`, **still no `steps`** | An always-on SDK host. Needs the SDK enabled on the cluster and operator grants; LLM calls also need `spec.agent` |
| Agentic, snippet or scheduled runs | `steps` plus `triggers` (required) | No webhook gateway; once `active`, long-running workloads no longer follow recipe edits unless the recipe also declares `pluginWorkloadSdk`; snippet steps need the operator's snippet runtime; scheduled recipes need the `clerum.io/workflow-team-id` label, which a registry install drops |

- Do not add a dormant "keepalive" step to get the SDK: it is not needed at this
  revision, and it turns off webhooks.
- A UI plugin that needs periodic work can run its own `cronjob` workload.
- On a cluster without the SDK enabled, a stepless recipe that declares
  `pluginWorkloadSdk` deploys **nothing**; keep that block out of recipes meant
  for such clusters.

## 2. Where things run, and who the user is

WRC places each workload by rule; you never choose the namespace:

- a workload with `transport` (an MCP server) runs in `mcp-server`;
- the workload named by `spec.ui.workloadRef` runs in `sandbox-ui`;
- every other workload runs in `sandbox-recipes`, where the recipe object
  itself lives.

```
Desktop app ──> rpc-proxy ──> ui (sandbox-ui, :8080) ──> api (sandbox-recipes) ──> db (sandbox-recipes)
                  │ strips Cookie, Authorization, X-Clerum-*            ^
                  │ adds X-Clerum-User (user UUID), X-Clerum-Recipe     │ bindings
                                                     mcp (mcp-server) ──┘ <── chat agents
```

- **Identity** is the `X-Clerum-User` header (the platform user's UUID),
  injected by rpc-proxy. Your UI server must pass it to the backend, and the
  backend keys all per-user data by it.
- **Cookies and `Authorization` never reach your UI pod**: cookie sessions do
  not work inside the Desktop.
- **Credentials belong in the backend** in `sandbox-recipes`. The UI can read a
  Secret in `sandbox-ui`, but keep the browser-facing pod credential-free.
- **Secrets are per namespace**: the backend and the MCP server each need their
  own copy of a shared value.

## 3. Repository layout and images

```
my-plugin/
  ui/      Dockerfile  nginx/default.conf.template  package.json  src/   -> sandbox-ui
  api/     Dockerfile  package.json  src/  migrations/                    -> sandbox-recipes
  mcp/     Dockerfile  package.json  src/            (optional)           -> mcp-server
  recipe/  my-plugin.yaml
```

Start from the assets (validated by building and running them):

- [assets/Dockerfile.ui](assets/Dockerfile.ui): static SPA on
  `nginxinc/nginx-unprivileged`, `USER 101`, port 8080.
- [assets/nginx-default.conf.template](assets/nginx-default.conf.template):
  proxies `/api/` to the backend, resolving it per request. A literal
  `proxy_pass http://${API_HOST}...` makes nginx exit at start when the
  backend's DNS name does not exist yet, and the UI pod crash-loops.
- [assets/Dockerfile.api](assets/Dockerfile.api): Node backend as uid 1000
  (the same shape works for an MCP server).
- [assets/embed-base.ts](assets/embed-base.ts): base URL and session recovery
  for the UI inside the Desktop.

Image rules:

- **Immutable tags.** Containers are rendered with `IfNotPresent` whatever the
  recipe says; re-pushing a tag never rolls out.
- **Numeric non-root `USER`**, and listen on `0.0.0.0`.
- **Pull access.** Images on the platform registry
  (`registry.evenfire.ai/<org>/<name>:<tag>`) get the platform pull credential
  attached automatically; never declare `evenfire-registry-pull`. Any other
  private registry needs your own `imagePullSecrets` Secret with an ownership
  label.
- **Architecture** must match the cluster's nodes (usually `linux/amd64`).

Get everything working locally first: run the UI dev server with a proxy to
the API that sets a test `X-Clerum-User`, and the API against a local
database.

## 4. The recipe

Copy [assets/recipe-ui-plugin.yaml](assets/recipe-ui-plugin.yaml) (UI, API,
Postgres, MCP server; it passes the CRD's schema and CEL rules) and adapt it.
The parts that matter most:

```yaml
apiVersion: clerum.io/v1alpha1
kind: WorkflowRecipe
metadata:
  name: notes                       # no metadata.namespace (or exactly sandbox-recipes)
spec:
  description: |                    # what operators read: every Secret, grant and step they must do
    ...
  workloads:
    - id: api
      type: deployment
      image: registry.example.com/acme/notes-api:1.0.0
      port: 8080
      env:
        - { name: PG_HOST, value: '{{db:host}}' }   # never a hardcoded *.svc.cluster.local name
        - { name: PG_PORT, value: '{{db:port}}' }
      envSecret: { name: notes-secrets, keys: [{ secretKey: pg-password, envVar: PG_PASSWORD }] }
      healthCheck: { type: http, path: /healthz, port: 8080, initialDelaySeconds: 15 }
    - id: ui
      type: deployment
      replicas: 1
      image: registry.example.com/acme/notes-ui:1.0.0
      port: 8080                    # must equal spec.ui.port; rpc-proxy allows 8080 by default
      env:
        - { name: API_HOST, value: '{{api:host}}' }
        - { name: API_PORT, value: '{{api:port}}' }
    # db (statefulset) and mcp (transport: streamableHttp) as in the asset
  ui:
    workloadRef: ui
    port: 8080
    title: Notes
    egress:
      internal:
        - { workloadRef: api, port: 8080 }     # without it the UI's calls to api hang
  bindings:
    - { from: mcp, to: api, port: 8080 }       # only for MCP <-> backend
```

Field rules that bite (full list in
[references/recipe-fields.md](references/recipe-fields.md)):

- Unknown keys are dropped silently (`when:`, `startupProbe:`, `env[].valueFrom`
  do nothing). `includeWhen` takes only `{{inputs.KEY}}`.
- `healthCheck` drives liveness and readiness; the HTTP default path is
  `/health`, so set `path`. There is no `startupProbe`: widen
  `initialDelaySeconds` for slow starts.
- The UI workload: `deployment`, 1 replica, no `transport`, `port` equal to
  `spec.ui.port` (8080).
- Any `{{...}}` in `env`, `command` or `args` must be a valid template, or the
  recipe fails.
- `security.runAsUser`/`runAsGroup`/`fsGroup` must be at least 1;
  `addCapabilities` only `CHOWN`, `FOWNER`, `DAC_OVERRIDE`, `NET_BIND_SERVICE`.
- `isolationLevel: standard` makes the root filesystem read-only: mount an
  emptyDir (a `volumeMounts` name that is not a PVC) at every writable path.
- `spec.resources` is for PVCs. Workloads cannot reference a Secret or
  ConfigMap resource by its id.

## 5. Addresses and network

Everything is denied by default, and a blocked call hangs instead of failing.
WRC opens these paths ([references/networking.md](references/networking.md)):

- **Backend to backend:** automatic when `env`/`command`/`args` reference the
  sibling with `{{id:host}}` (and the port, if written, equals the target's
  `port`).
- **UI to backend:** `spec.ui.egress.internal`.
- **MCP server to backend:** `spec.bindings` (exactly one MCP workload and one
  non-MCP workload per binding; anything else is rejected).
- **To the internet:** `egressBindings` entries, one public host and port each
  (`exact-host`). The host must have public IPv4 records when WRC reconciles,
  or the recipe fails. `public-web` (any public host) is accepted only on MCP
  workloads.
- `dependsOn` and `isolationLevel` open nothing.

## 6. Secrets

([references/secrets-and-images.md](references/secrets-and-images.md))

- `envSecret` reads a Secret from the workload's own namespace; there is no
  other way to get a Secret into a container.
- Every Secret needs exactly one ownership label:
  `clerum.io/owner-recipe=<in-cluster recipe name>` or `clerum.io/shared=true`.
  Otherwise the workload is not deployed (`EnvSecretOwnershipDenied`). The
  Control UI's Secrets tab writes the label; install the recipe first so the
  owner exists.
- Never put credentials in `env`. Control API's create and edit routes reject
  names containing `PASSWORD`, `TOKEN`, `SECRET`, `API_KEY`, `CREDENTIAL` or
  `PRIVATE_KEY` with a literal value, which also catches `MAX_TOKENS`. Never
  name a Secret `wf-*` or `evenfire-registry-pull`.
- Document every Secret in `spec.description`: name, namespaces, keys, what
  breaks without each, where each value comes from.

## 7. The UI inside the Desktop

([references/ui-embed.md](references/ui-embed.md))

- It is served under `/api/v1/sandbox-ui/<namespace>/<recipe>/view/`; your pod
  sees the path without that prefix. Use relative URLs, pin `<base>` at
  runtime, serve `index.html` for unknown paths (assets do this), and route by
  pathname with the embed root as basename.
- A fixed CSP is added to every response: no inline scripts, no CDN, no calls
  to other origins from the browser (relay through the backend), images only
  from your origin or `data:`.
- No WebSockets. Server-sent events work: `X-Accel-Buffering: no` and a
  heartbeat shorter than the proxy read timeout.
- The session cookie lasts 5 minutes. On `401 sandbox_ui_session_invalid`,
  call `window.clerum.requestSessionRefresh()` once and retry.
- `window.clerum` (only inside the Desktop) gives the theme without asking
  (`theme.get()` and `theme.changed`; do not rely on `prefers-color-scheme`),
  and, behind a consent modal (at most 3 per view; ask once, batched), the
  user's identity, team, agents, shared files and local notifications.
- Downloads are blocked, `window.open` and outside links go to the user's
  browser, the async Clipboard API is denied. Deep links:
  `<profile UI host>/open/apps/sandbox-recipes/<recipe>?path=<route>`.
- Send `Accept: application/json` on API calls; otherwise "updating" and
  "removed" answers come back as HTML pages.

## 8. Optional capabilities

- **Desktop notifications and LLM calls**
  ([references/plugin-workload-sdk.md](references/plugin-workload-sdk.md)):
  declare `spec.pluginWorkloadSdk`, call
  `$PLUGIN_WORKLOAD_SDK_ENDPOINT/v1/client-notifications` or
  `/v1/prompt-bridge` from the backend with `$PLUGIN_WORKLOAD_SDK_TOKEN`. An
  operator grant is mandatory; recipients are user UUIDs; the plugin's access
  list and the notification recipients are separate lists.
- **Webhooks** ([references/webhooks-and-oauth.md](references/webhooks-and-oauth.md)):
  stepless recipes only; the platform verifies HMAC, bearer or JWT signatures
  and forwards to your handler workload.
- **OAuth**: background (a workload gets provider tokens from the platform's
  broker) or from the UI (`clerum://oauth?clientId=...`).
- **Workflows** ([references/workflows.md](references/workflows.md)): steps,
  triggers, TypeScript snippets with `sdk.*`, built-in document tools.
- **MCP server for chat agents**: a workload with
  `transport: {type: streamableHttp, path: /mcp}`. It lands in a private
  Context `wf-<recipe>`; an operator must add it to an agent's Context
  (`create-evenfire-mcp-server`).

## 9. Validate, install, verify

([references/operate.md](references/operate.md))

1. **Admission rules, offline:**
   `kubectl-validate recipe.yaml --local-crds <evenfire>/charts/clerum-crds/crds --version 1.30`
   (or `kubectl apply --dry-run=server -n sandbox-recipes -f recipe.yaml`).
2. **Controller and Control API rules:**
   `python3 scripts/recipe-preflight.py recipe.yaml` (needs PyYAML). Optional:
   `POST /api/v1/admin/recipes/validate` with an admin session.
3. **Install** from the registry (Marketplace; the in-cluster name becomes
   `recipe-<entry slug>-v<version>-<8 hex>` and your labels are dropped), with
   the admin API (`POST /api/v1/admin/recipes`, keeps your name), or with
   `kubectl apply -n sandbox-recipes` on a development cluster. Find the
   in-cluster name before anything that uses it.
4. **Then:** create and label the Secrets; grant users or teams (Members and
   Teams tabs); for the SDK, operator grants; for chat agents, attach the MCP
   server.
5. **Verify:** `status.phase` is `active`, `status.message` is "All workloads
   deployed", and pods labelled `clerum.io/recipe=<name>` are Ready in every
   namespace the plugin uses. The Desktop lists the app only while it is
   `active` (a fresh install is `degraded` until its pods are ready).
6. **Update** with a new image tag and a changed recipe. For a registry
   install, `POST /api/v1/admin/registry/upgrade-recipe` changes it in place;
   installing the new version from the Marketplace creates a second recipe.
   Removing a workload from the recipe does not delete it.
7. **Recover** a `failed` recipe by fixing the cause and pressing Retry (or
   `POST /api/v1/admin/recipes/<name>/retry`); WRC does not leave `failed` on
   its own after a real error.
8. **Uninstall** keeps PVCs (data), your Secrets, and Plugin SDK grants
   (`disabled`, which block new grants for the same name until deleted).

## 10. Failures that do not announce themselves

| Symptom | Usual cause |
|---|---|
| A call to a sibling or the internet hangs | No network path (section 5) |
| The UI pod crash-loops with `host not found in upstream` | nginx with a literal upstream host |
| The Desktop shows a blank panel | Absolute asset or API URLs (`/assets/...`, `fetch('/api')`), or an inline script blocked by the CSP |
| The Desktop says "is updating" forever | The recipe is not `active` or the UI pod is not Ready |
| A workload silently missing | Unknown key pruned, `includeWhen` that is not `{{inputs.KEY}}`, or a Secret without an ownership label |
| New image never runs | Tag reused, or a recipe with `steps` (and no `pluginWorkloadSdk`) that is already `active` |
| Webhook URL fails | The recipe has `steps` |
| Notifications never arrive | No operator grant, user not in the grant, event type not in the grant, or the SDK is disabled |
| `promptBridge` returns `idempotency_conflict` | Key reused; results are not stored, so persist them yourself |
| User data mixed between people | State keyed by a cookie or by browser storage instead of `X-Clerum-User` |
| A workflow recipe fails right after install | `runRetention` without `maxRunDurationSeconds` of at most 86400, or a scheduled recipe without `clerum.io/workflow-team-id` |
| A plugin user cannot see the app | No Members/Teams grant, a team grant while the user works in another team, or the recipe is not `active` |

## 11. Pre-flight checklist

- [ ] Shape chosen on purpose: no `steps` unless the plugin really runs
      workflows; no SDK block for clusters without the SDK.
- [ ] `metadata.name` is a lowercase RFC 1123 label; no other namespace.
- [ ] Every sibling address is `{{id:host}}`/`{{id:port}}`; no
      `*.svc.cluster.local` literal in `env`, `command` or `args`.
- [ ] UI workload: `deployment`, 1 replica, no `transport`, port 8080 equal to
      `spec.ui.port`, `ui.egress.internal` to each backend it calls.
- [ ] Every internet host the backend calls is an `exact-host`
      `egressBindings` entry that resolves publicly; `public-web` only on MCP
      workloads; `bindings` only MCP to backend.
- [ ] No credentials in `env`; every `envSecret`/`imagePullSecrets` Secret is
      documented in `spec.description`, with its namespaces and keys.
- [ ] `healthCheck` set with an explicit `path`, and room for slow starts.
- [ ] Images: immutable tags, numeric non-root user, `0.0.0.0`, pullable by the
      cluster.
- [ ] UI: relative URLs plus runtime `<base>`, SPA fallback, no inline scripts
      or remote assets, `Accept: application/json` on API calls, session
      recovery on 401, theme from `window.clerum`.
- [ ] Backend trusts identity only from `X-Clerum-User` and keys user data by it.
- [ ] `kubectl-validate` and `scripts/recipe-preflight.py` pass.
- [ ] After install: Secrets labelled, access granted, SDK grants (if any),
      `status.phase` `active`, pods Ready in every namespace, the app opens in
      the Desktop.

When all of these hold, publish with `publish-evenfire-plugin`.
