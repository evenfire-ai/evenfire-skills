---
name: create-evenfire-plugin
description: >-
  Build an Evenfire plugin end to end: a WorkflowRecipe with a sandbox web UI, a
  credentialed backend, an optional MCP server and database, webhooks, OAuth, or
  an agentic/snippet workflow. Use when the task is to design, scaffold, write,
  change, containerize, or wire a plugin or recipe for a Clerum/Evenfire cluster,
  or when you need the exact WorkflowRecipe fields, enums, validation rules,
  namespace/credential boundaries, image requirements, or the build-and-deploy loop.
---

# Building an Evenfire plugin (WorkflowRecipe)

> **Verified against Evenfire `dev` at commit `f9e8d0487`.**
> CRD API `clerum.io/v1alpha1`, `clerum-crds` chart `0.8.0`. Field names, enums,
> endpoints, and commands below match that revision.

A plugin is one Kubernetes custom resource, `apiVersion: clerum.io/v1alpha1`,
`kind: WorkflowRecipe`. The platform reconciles it into Deployments,
StatefulSets, CronJobs, MCP servers, a sandboxed web UI, NetworkPolicies, and
workflow runs. A UI plugin is normally a small multi-service repo (`ui/`, `api/`,
`mcp/`, `recipe/`) whose recipe wires those images together.

Everything in this skill is verified against the live platform code
(`clerum.io` CRDs, `workflow-recipes`, `host-context-controller`, `control-api`,
`rpc-proxy`, `mcp-host`) and two shipped plugins. Never invent a field, enum,
tool, or endpoint that is not listed here. When you need a value that is not
here, read the CRD at `charts/clerum-crds/crds/workflowrecipe.yaml` and the WRC
reconciler rather than guessing.

To publish and install what you build, use the `publish-evenfire-plugin` skill.
To run a cluster and debug a deployed recipe, use the `run-debug-evenfire` skill.

## 1. Pick the archetype first

One field decides the plugin's whole lifecycle: `isWorkflow = spec.steps.length > 0`.

| Archetype | Shape | "Healthy" means | Typical use |
|---|---|---|---|
| Pure workloads | `workloads`, no `steps` | every workload Deployment/StatefulSet applied and its pods Ready | a UI app + backend + DB, or a standalone MCP server |
| Pure workflow | `steps`, no long-running `workloads` | the run reaches `completed` | agentic or snippet pipelines, cron jobs |
| Hybrid | both | workloads Ready and runs succeed | a UI app that also emits desktop notifications, a workflow with a sidecar |

A recipe MUST populate at least one of `workloads` or `steps` (rule R2).

Deploy behavior differs by archetype and this bites people (section 9): a
recipe **with** `steps` does not re-apply workload images once it is `active`,
a recipe **without** `steps` does. Adding even a dormant keepalive step (as the
notifications pattern below does) flips a UI plugin into the "does not
auto-propagate image edits" class.

## 2. The credential boundary (the core mental model)

The platform routes each workload to one of three namespaces by rule, and you
never set `metadata.namespace` yourself:

- workload has `transport` -> `mcp-server`
- workload is the one named by `spec.ui.workloadRef` -> `sandbox-ui`
- every other workload -> `sandbox-recipes`
- the WorkflowRecipe object itself always lives in `sandbox-recipes`

`sandbox-ui` is the credential-free browser-facing zone. `sandbox-recipes` is
where Secrets live. This gives the canonical UI plugin its shape:

```
 browser (Electron WebContentsView)
   |  rpc-proxy strips Cookie/Authorization/x-clerum-*, injects x-clerum-user
   v
 ui   (sandbox-ui)      nginx-unprivileged, no Secrets, serves the SPA,
   |                    reverse-proxies /api/* to the backend
   v  spec.ui.egress.internal / spec.bindings (default-deny NetworkPolicy)
 api  (sandbox-recipes) holds Secrets via envSecret, talks to db + third parties
   |
 db   (sandbox-recipes) StatefulSet + PVC
 mcp  (mcp-server)      optional MCP server, same dataset, for the chat agent
```

Hard rules that follow from this:

- The UI workload MUST NOT declare `envSecret`, `imagePullSecrets`, or any
  credential reference. There is no Secret-resolution path in `sandbox-ui`; a UI
  pod that references a Secret sits in `CreateContainerConfigError` forever.
- Credentialed code lives on a sibling backend in `sandbox-recipes`, reached
  from the UI over `spec.ui.egress.internal[]` (the UI's nginx `proxy_pass`).
- The backend trusts identity ONLY from the rpc-proxy-injected `X-Clerum-User`
  header. rpc-proxy strips every client-supplied `x-clerum-*`, `Cookie`, and
  `Authorization` header, so the embed cannot spoof a user. Never read a user id
  from the request body or from embed JS.
- A Secret is read from the namespace of the workload that consumes it, and
  nothing copies Secrets across namespaces. If both the backend
  (`sandbox-recipes`) and the MCP server (`mcp-server`) read the same Secret, it
  must exist, with the same value, in BOTH namespaces.

## 3. The repo scaffold (multi-service UI plugin)

```
my-plugin/
  api/    Dockerfile package.json tsconfig.json src/ migrations/   -> sandbox-recipes
  ui/     Dockerfile package.json vite.config.ts src/ nginx/       -> sandbox-ui
  mcp/    Dockerfile package.json src/                             -> mcp-server (optional)
  recipe/ my-plugin.yaml my-plugin.json                            the WorkflowRecipe
  docker-compose.dev.yml   full local stack, no cluster needed
  Makefile                 build-multiarch / dev / recipe-json / publish
  README.md
```

### 3.1 The three Dockerfiles (exact, verified shapes)

Ports are fixed by convention and must match the recipe: **ui 8080, api 8080,
mcp 3000**. All images run non-root with a numeric `USER` so `runAsNonRoot` can
verify them, and must be multi-arch (`linux/amd64` + `linux/arm64`).

`ui/Dockerfile`, static SPA served by unprivileged nginx. Build the bundle once
on the native builder to avoid emulating esbuild/rollup under QEMU:

```dockerfile
FROM --platform=$BUILDPLATFORM node:20-alpine AS build
WORKDIR /app
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build                       # -> /app/dist, no inline scripts

FROM nginxinc/nginx-unprivileged:1.27-alpine
ENV API_HOST=api
ENV API_PORT=8080
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
USER 101
EXPOSE 8080
```

`ui/nginx/default.conf.template` (envsubst fills `API_HOST`/`API_PORT`):

```nginx
server {
  listen 8080;
  root /usr/share/nginx/html;
  location /api/ {
    proxy_pass http://${API_HOST}:${API_PORT};
    proxy_set_header X-Clerum-User $http_x_clerum_user;
  }
  location / { try_files $uri /index.html; }
}
```

`api/Dockerfile`, credentialed backend:

```dockerfile
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY tsconfig.json ./
COPY src ./src
RUN npm run build && npm prune --omit=dev

FROM node:20-alpine AS runtime
WORKDIR /app
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}
ENV NODE_ENV=production
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY package.json ./
USER 1000
EXPOSE 8080
CMD ["node", "dist/server.js"]
```

`mcp/Dockerfile` (only if the plugin exposes an MCP server): identical to the
api runtime but `EXPOSE 3000`. See the `create-evenfire-mcp-server` skill for
the server code contract.

### 3.2 Version stamping

Thread one `APP_VERSION` build-arg through:

- api: `ARG APP_VERSION` -> `ENV APP_VERSION` -> `process.env.APP_VERSION ?? 'dev'`
  reported by `/api/healthz`.
- ui: `ARG APP_VERSION` -> `ENV APP_VERSION` -> Vite `define: { __APP_VERSION__:
  JSON.stringify(process.env.APP_VERSION || 'dev') }`, shown in the app shell.

Do not stamp the mcp image with a build-time version unless it actually reads
one; MCP servers version through `registry.json`/`package.json`.

### 3.3 Local dev loop (no cluster)

`docker-compose.dev.yml` runs db + api + mcp + ui together. Per-service watch
mode also works: `cd api && npm run dev` (tsx watch), `cd ui && npm run dev`
(Vite dev server that proxies `/api` to the local api and injects a dev
`X-Clerum-User`). Get the plugin fully working here before touching a cluster.

### 3.4 Multi-arch build and push

```bash
docker buildx create --use --name multiarch     # one-time
for svc in api ui mcp; do
  docker buildx build --platform linux/amd64,linux/arm64 \
    --build-arg APP_VERSION="$VERSION" \
    --label org.opencontainers.image.source=https://github.com/<org>/<repo> \
    --label org.opencontainers.image.licenses=Apache-2.0 \
    -t <registry>/<name>-$svc:$VERSION --push "$svc/"
done
```

`<registry>` is a public registry (Docker Hub, GHCR, Quay) for a public plugin,
or `registry.evenfire.ai/<org>` for an org-private one. See `publish-evenfire-plugin`.

## 4. The recipe YAML

`recipe/my-plugin.yaml` wires the images. Full UI + backend + DB + MCP example:

```yaml
apiVersion: clerum.io/v1alpha1
kind: WorkflowRecipe
metadata:
  name: my-plugin                        # DNS-1123, <= 63 chars. Do NOT set metadata.namespace.
spec:
  description: |
    One-sentence summary first (operators see it in the catalog list).
    Then document EVERY Secret an operator must create before install:
    its name, keys, which namespace(s), and where each value comes from.
  security:
    isolationLevel: minimal              # minimal | standard | strict

  workloads:
    - id: db
      type: statefulset
      image: postgres:16-alpine
      port: 5432
      env:
        - { name: POSTGRES_USER, value: myplugin }
        - { name: POSTGRES_DB,   value: myplugin }
        - { name: PGDATA, value: /var/lib/postgresql/data/pgdata }   # subdir, not the mount root
      envSecret:
        name: my-plugin-secrets
        keys:
          - { secretKey: pg-password, envVar: POSTGRES_PASSWORD }
      volumeMounts:
        - { name: pgdata, mountPath: /var/lib/postgresql/data }
      volumeClaimTemplates:
        - { name: pgdata, storageClass: standard, accessMode: ReadWriteOnce, size: 1Gi }
      security:
        runAsUser: 70                    # postgres:16-alpine runs as uid 70
        runAsGroup: 70
        fsGroup: 70
        addCapabilities: [CHOWN, FOWNER, DAC_OVERRIDE]   # initdb needs these

    - id: api
      type: deployment
      image: <registry>/my-plugin-api:1.0.0
      port: 8080
      envSecret:
        name: my-plugin-secrets
        keys:
          - { secretKey: pg-password, envVar: DATABASE_PASSWORD }
      healthCheck: { type: http, path: /api/healthz, port: 8080 }
      egressBindings:
        - { dns: api.anthropic.com, port: 443 }          # every third-party host, exact-host
      dependsOn: [db]

    - id: ui
      type: deployment
      image: <registry>/my-plugin-ui:1.0.0
      port: 8080
      env:
        - { name: API_HOST, value: '{{api:host}}' }       # template var, resolved at build time
        - { name: API_PORT, value: '{{api:port}}' }
      healthCheck: { type: http, path: /, port: 8080 }

    - id: mcp                            # optional MCP server, same dataset
      type: deployment
      image: <registry>/my-plugin-mcp:1.0.0
      port: 3000
      transport: { type: streamableHttp, path: /mcp }
      envSecret:
        name: my-plugin-secrets          # note: this Secret must ALSO exist in mcp-server ns
        keys:
          - { secretKey: mcp-api-token, envVar: MCP_API_TOKEN }

  ui:
    workloadRef: ui                      # must be a deployment, replicas 1, no transport (R15/R16)
    port: 8080                           # must be 8080 (rpc-proxy allow-list), else 502 at view time
    title: My Plugin
    defaultPath: /
    egress:
      internal:
        - { workloadRef: api, port: 8080 }   # opens ui -> api (nginx proxy_pass target)

  bindings:
    - { from: mcp, to: api, port: 8080 }     # mcp (mcp-server ns) -> api (sandbox-recipes)
```

### 4.1 Field and enum reference

The exhaustive per-field and per-enum reference — every `workloads[]`,
`healthCheck`, `security`, `transport`, `egressBindings`, `volumeClaimTemplates`,
`spec.ui`, `spec.bindings`, and `spec.contextRef` value — lives in
[references/recipe-fields.md](references/recipe-fields.md). Copy values verbatim
from there; when a value is not listed, read the CRD at
`charts/clerum-crds/crds/workflowrecipe.yaml` and the WRC reconciler rather than
guessing.

### 4.2 Secrets, documented and labeled

Operators create the Secret out of band, guided by your `spec.description`. For
each Secret answer: what it is, which workload/endpoint breaks without it, where
each value comes from, any provider-side value to register, and the exact Secret
name + key names. A missing required `envSecret` key leaves the pod in
`CreateContainerConfigError`; a missing OAuth or webhook Secret surfaces as a
`503`/`410 integration_not_configured` at use time.

Recipe Secrets also carry an ownership label (enforced, and mandatory in CI): an
unlabeled Secret is refused with `EnvSecretOwnershipDenied` and every workload
that references it is torn down. Label with
`clerum.io/owner-recipe=<deployed-recipe-name>` + `clerum.io/recipe-secret=true`,
OR `clerum.io/shared=true` (any recipe may read it). Never set both, the conflict
is treated as denied. Install the recipe first so `owner-recipe` names something
that exists. When the MCP workload shares a Secret, create and label it in BOTH
`sandbox-recipes` and `mcp-server`.

### 4.3 OAuth, webhooks, notifications (add only what you need)

- OAuth (`spec.oauthClients[]`, max 8): `{id, provider, clientIdRef,
  clientSecretRef, scopes?, backgroundAccess?}`. `provider` is
  `salesforce|slack|notion|microsoft-graph|google`. Every client needs a
  consumer, either `spec.ui` (foreground embed) or a `workloads[].oauthClientRefs`
  (background, `backgroundAccess: true`, non-MCP, non-UI workloads only). The
  embed connects by navigating to `clerum://oauth?clientId=<id>`; the front-door
  endpoints live on rpc-proxy under
  `/api/v1/sandbox-ui/<ns>/<name>/oauth/{authorize-url,token,grant}`. A missing
  client Secret returns `503 integration_not_configured`.
- Webhooks (`spec.webhooks[]`, max 16): `{id, workloadRef, path, verification}`;
  `workloadRef` is a non-MCP deployment. `verification.scheme` is
  `hmac-sha256-body | hmac-sha256-timestamp-body | jwt-bearer-jwks | static-bearer`.
  `secretRef` is required for every scheme except `jwt-bearer-jwks`;
  `hmac-sha256-timestamp-body` also needs `replay`; `jwt-bearer-jwks` needs
  `jwksUrl`+`issuer`+`audience`. GET is allowed only with a `setupHandshake`
  (`meta-hub-challenge | slack-url-verification | stripe-verify`). Public URL:
  `<host>/api/v1/webhook/<ns>/<name>/<id>`. `optional: true` keeps a webhook
  dormant (`410`) until its Secret appears.
- Plugin Workload SDK (`spec.pluginWorkloadSdk`): opt the recipe into controlled
  side-effect channels. At least ONE capability family must be declared (CEL
  `PS1`). Declaring the block provisions an always-on SDK mcp-host (`:8099/sdk`)
  eagerly — no run and no keepalive step are needed. Shared fields: `allowedCallers`
  (workload ids allowed to call; each must reference a real `workloads[].id`, else
  `PS4`; empty = all declared workloads) and `idempotencyKeyPattern` (regex,
  default `^[a-zA-Z0-9_-]{1,128}$`). Every call is authorized against a matching
  control-api grant regardless of the CRD block — without the grant every call is
  `403 capability_not_declared` and nothing happens, silently. The grant is NOT
  part of the recipe YAML; an operator creates it (`POST /admin/plugin-workload-sdk/grants`),
  so document the requirement in `spec.description`.
  - `clientNotifications` (desktop notifications): `allowedEventTypes` (required,
    1-64, no wildcards `PS2`), `allowedTargetRefs` (opaque refs, ≤64),
    `allowedUserRefs` (a BOOLEAN — whether the recipe may target named users at
    all; the concrete user UUIDs live only in the operator grant, never in the
    recipe YAML), plus ceilings `maxNotificationsPerRun` / `maxNotificationsPerMinute`
    / `maxTitleBytes` / `maxBodyBytes`. Targets are opaque refs, never raw
    email/phone.
  - `promptBridge` (one-shot LLM call from a caller workload or snippet):
    `allowedModels` (≤32, no wildcards `PS3`) plus ceilings `maxOutputTokens` /
    `maxRequestsPerRun` / `maxConcurrentInvocations` / `maxInvocationsPerMinute`.
    The provider is NOT author-selectable — it is the one bound to the recipe's
    mcp-host; a request may pick a model within `allowedModels` only. It is
    inference-only: no tools, no multi-turn, no attachments. Needs a resolvable
    agent on the recipe (`spec.agent` or a step agent); a clientNotifications-only
    recipe does not.

### 4.4 Steps, triggers, snippets, and outputs (workflow archetype)

If the plugin runs work (not just serves a UI), add `spec.steps[]` (max 100).
Each step sets exactly one of `instruction` (natural language, agentic) or `run`
(a snippet), and a step id (`^[a-z][a-z0-9-]*$`).

- `dependsOn` orders steps, it does NOT pass data. Inject an upstream output with
  `{{stepId:output}}` inside the next step's `instruction`. A snippet's `run.code`
  receives NO `{{...}}` substitution, it reads run inputs via `sdk.inputs` and
  prior outputs via `sdk.previousOutputs`.
- Agentic `agent.provider` values are the 19-entry enum (`openai, claude, zai,
  bailian, vertex, openrouter, gemini, deepseek, groq, together, fireworks,
  mistral, xai, cerebras, deepinfra, perplexity, moonshot, nebius, novita`);
  `bedrock` and `azure` are intentionally rejected.
- Per-step approval: `requiresApproval: { target: {userId} XOR {teamId}, message,
  timeoutSeconds }` (default 3600, range 30 to 604800).

Snippet steps run platform TypeScript without an agent call:

```yaml
steps:
  - id: build
    run:
      type: snippet            # only value; language: typescript only; code <= 20000 chars
      language: typescript
      capabilities:
        http: { egressClass: exact-host, allowedHosts: [api.example.com] }
        secrets: [{ alias: key, secretRef: { name: my-plugin-secrets, key: api-key } }]
        postgres: { workloads: [db], access: read }     # or readWrite
      code: |
        const rows = await sdk.postgres.query(
          { workload: 'db', database: 'myplugin' },
          { sql: 'select count(*) as n from things' },
        )
        const j = await sdk.http.fetchJson('https://api.example.com/thing')
        await sdk.artifacts.writeJson('out.json', { rows, j })
        return { count: rows.length }
```

The `sdk.*` surface: `sdk.inputs`, `sdk.previousOutputs`, `sdk.http.fetchJson`,
`sdk.http.fetchText`, `sdk.secrets.get`, `sdk.postgres.query`/`.execute`,
`sdk.mongo.*`, `sdk.mcp.callTool`, `sdk.artifacts.writeJson`/`.writeMarkdown`,
`sdk.log`, `sdk.sleep`. Every capability the code uses must be declared under
`run.capabilities`, and every snippet HTTP host must ALSO appear in
`spec.runtimeEgress.http.allowedHosts`.

Triggering and inputs:

- `spec.triggers`: presence of `onDemand: {}` enables manual runs
  (`requiresApproval` default true; `allowedActors` is `[user|autonomous|scheduled]`,
  default `[user]`). Presence of `schedule: { cron, timezone (default UTC),
  concurrencyPolicy Forbid|Replace|Allow (default Forbid), suspend }` enables cron.
  A recipe with `steps` should declare `spec.triggers` (control-api validates it).
- `spec.inputContract` (JSON Schema) plus `spec.inputs`: an `inputContract`
  property `default` substitutes `{{inputs.KEY}}` at build time; runtime inputs
  override it.

Producing files:

- Internal output tools are available to agentic steps with no MCP server:
  `clerum__generate_markdown`, `clerum__generate_pdf`, `clerum__generate_docx`,
  `clerum__generate_xlsx`, `clerum__generate_pptx`, `clerum__generate_chart`,
  `clerum__generate_dashboard`, plus `clerum__list_workflows`,
  `clerum__read_workflow`, `clerum__trigger_workflow`, and the `clerum__gfs_*`
  Global File System tools.
- `spec.output`: `{ destination: configmap|secret|stdout|pvc, format:
  pdf|xlsx|json|text|html|multi, claimName (pvc only), storageSize }`. Files are
  ephemeral unless `destination: pvc`. Download a run's artifact via control-api
  `GET /api/v1/admin/recipes/:name/artifacts/:artifactName/download`.

### 4.5 Shared resources (`spec.resources[]`)

Declare PVCs, Secrets, or ConfigMaps created alongside the workloads:

```yaml
resources:
  - { id: scratch, type: pvc, storageClass: standard, size: 1Gi, accessMode: ReadWriteOnce }
  - { id: signing, type: secret, generateKeys: [hmac-secret] }   # auto-random value
```

Each item needs `id` + `type` (`pvc | secret | configmap`). PVCs use
`storageClass`/`size`/`accessMode`; secret/configmap use `data` (a key/value map)
and `generateKeys` (keys the platform fills with random values).

### 4.6 Other workload types and advanced fields

- `cronjob` workloads MUST set the workload-level `schedule` (five-field cron);
  `job` workloads may set `backoffLimit`; `daemonset` runs one pod per node.
- Fields this skill does not cover in depth, present on the CRD when you need
  them (read `charts/clerum-crds/crds/workflowrecipe.yaml` for the exact shape):
  `spec.scheduling` (a K8s CronJob for the whole workflow), `spec.computed`
  (derived template values), `spec.profiles` + `spec.activeProfile` (named
  override maps), `spec.dependencies` (other recipes this one needs),
  `spec.runRetention`, `spec.coordinatorImage` (operator-approved custom
  coordinator, feature-flagged), `spec.gfs` (Global File System publish targets
  and mounts), and `spec.runtimeEgress` (shared snippet HTTP egress intent).

## 5. Egress is default-deny

Every namespace denies egress except DNS. Declare every outbound destination or
the call hangs (`ETIMEDOUT`), never a clean error:

- UI pod: `spec.ui.egress.internal[]` (siblings) and `spec.ui.egress.external[]`
  (third-party FQDNs).
- Backend / MCP workloads: `workloads[].egressBindings[]`, one `exact-host`
  entry per hostname (or one `public-web` entry for genuinely dynamic public
  destinations). Cluster-local siblings are reached as
  `<id>.<ns>.svc.cluster.local`.
- Snippet HTTP: `spec.runtimeEgress.http.allowedHosts` plus the per-snippet
  `run.capabilities.http.allowedHosts`.

When you split a single-image recipe into UI + backend, external egress MOVES
from `spec.ui.egress.external[]` to the backend's `egressBindings[]`, because the
credentialed pod now lives in `sandbox-recipes`. Forgetting this is the classic
"third-party API that worked yesterday now times out" bug.

## 6. Validation layers

A recipe is validated three times; passing one does not guarantee the next:

1. CEL admission on the CRD: rule codes `R1-R18` (recipe-wide, workloads, ui,
   steps), `W1,W3-W14` (webhooks), `O1,O3,O4` (oauth), `PS1-PS3`
   (pluginWorkloadSdk). There are **no** `E`-prefixed codes.
2. WRC reconciler: `W2` (webhook workloadRef is a non-MCP deployment), `PS4`
   (allowedCallers reference real workloads), the `contextRef` policy gate, and
   `EnvSecretOwnershipDenied`.
3. control-api `validateRecipeBody` (400 on name/steps/triggers/limits) before
   apply, then marketplace/install checks.

## 7. Deploy and verify

Install through the Control UI Marketplace (recommended) or, for CI, the
control-api admin API. In cluster the recipe is named
`recipe-<entry-slug>-v<version>-<hash>` for a registry install (version dots
become hyphens, for example `recipe-evenfire-worktracker-v1-1-0-66ce9c41`; a
hand-applied file keeps `metadata.name`), so never look it up by the bare name:

```bash
K="kubectl --context=<allowed-context>"
# The recipe object is NOT labeled with its base name; match the generated name
# (recipe-<entry-slug>-v<ver>-<hash> for a registry install, or metadata.name for a hand-apply).
$K get workflowrecipes -n sandbox-recipes -o name | sed 's|.*/||' | grep -E '(^|-)<name>(-|$)'
NAME=$($K get workflowrecipes -n sandbox-recipes -o name | sed 's|.*/||' | grep -E '(^|-)<name>(-|$)' | head -1)
$K get workflowrecipe "$NAME" -n sandbox-recipes -o jsonpath='{.status.phase}'   # expect: active
```

The healthy phase is **`active`** (the 13-state enum has no `ready`/`reconciling`).
`.status.workloads[].ready` tracks real replica readiness for
deployment/statefulset/daemonset workloads (so `true` there means those pods are
Ready), but it is a per-reconcile snapshot and MCP/job workloads report readiness
differently, so confirm real pod state and check BOTH namespaces for a split
plugin:

```bash
$K get pods -n sandbox-recipes -l clerum.io/recipe="$NAME"
$K get pods -n sandbox-ui      -l clerum.io/recipe="$NAME"
$K get pods -n mcp-server      -l clerum.io/recipe="$NAME"
```

The recipe is invisible to end users until an operator grants it on the recipe's
Members tab. Full debugging playbook: the `run-debug-evenfire` skill.

## 8. Deploying a code change (the image-propagation trap)

Editing `spec.workloads[].image` on the CR behaves differently by archetype:

- Recipe **without** `steps` (pure workloads): the CR image edit re-applies on
  the next reconcile.
- Recipe **with** `steps` (including a dormant keepalive): once `active`, the
  reconciler short-circuits and does NOT redeploy workloads from the CR.

So for a UI plugin that carries a keepalive step, the real deploy path is: build
and push the new image, then update the running Deployment directly, targeting it
by label (the name is recipe-scoped and usually hashed, so never guess the bare
`api`):

```bash
DEP=$($K get deploy -n sandbox-recipes -l clerum.io/recipe="$NAME",clerum.io/workload=api \
        -o jsonpath='{.items[0].metadata.name}')
$K -n sandbox-recipes set image deploy/"$DEP" api=<registry>/my-plugin-api:1.0.1
```

Then patch the CR's image tag too so a future full reconcile converges. The MCP
workload is different: WRC creates an `McpServer` CRD and HCC materializes that
Deployment, so update the MCP image through the McpServer/recipe, not a raw
`set image` on the HCC-owned Deployment.

## 9. Silent-failure gotchas (get these right the first time)

- `includeWhen`, not `when`, is the conditional-deploy field. An unknown key
  like `when:` is pruned at admission and the workload deploys unconditionally.
- No `startupProbe`, no `env.valueFrom`, no `envFromConfigMap`, no ConfigMap
  mount. Unknown keys are silently pruned by strict decoding.
- `ui.port` must be 8080. Any other value passes CRD validation but the embed
  returns `502 port_not_allowed` at first view.
- The UI/backend must listen on `0.0.0.0`, not `127.0.0.1`, or the ClusterIP
  Service cannot reach them.
- The embed runs under a hard CSP (`script-src 'self'`, `connect-src 'self'`,
  no inline scripts, no CDN, no cross-origin fetch, WebSocket returns 426). Bundle
  JS same-origin, relay every third-party call through the backend, use SSE or
  polling. Use `<base href="./">` + relative asset paths, or absolute `/assets`
  paths resolve outside the mount and the panel renders blank.
- `dependsOn` and `bindings`/`egress` are orthogonal: order vs network. The UI
  cannot reach the backend without an `egress.internal` (or binding) entry even
  though both pods are healthy.
- The `sandbox-ui` namespace enforces PodSecurity `baseline` and only warns on
  `restricted`, so a root UI image is warned, not hard-rejected. Ship a non-root
  numeric `USER` anyway (nginx-unprivileged is uid 101).

## 10. Pre-flight checklist

- [ ] `apiVersion: clerum.io/v1alpha1`, `kind: WorkflowRecipe`, no `metadata.namespace`.
- [ ] `metadata.name` DNS-1123, <= 63 chars.
- [ ] At least one of `workloads` / `steps`.
- [ ] UI workload: `type: deployment`, `replicas: 1` (or omitted), no `transport`,
      `ui.port: 8080`, image runs non-root on `0.0.0.0:8080`, health path returns 2xx.
- [ ] Embed JS is same-origin external `.js`, no inline scripts, no CDN, no
      cross-origin fetch, `<base href="./">` + relative paths.
- [ ] Every third-party host is in some workload's `egressBindings[]` or
      `spec.ui.egress.external[]`; every sibling call has an `egress.internal`
      entry or a `binding`.
- [ ] Each step sets exactly one of `instruction` / `run`; `{{stepId:output}}`
      injected wherever a step needs prior data.
- [ ] Every referenced Secret documented in `spec.description` (name, keys,
      namespace(s), source of each value) and, if shared with the MCP workload,
      created + labeled in both `sandbox-recipes` and `mcp-server`.
- [ ] StatefulSet with a PVC sets `security.fsGroup` = `runAsGroup`.
- [ ] Images are multi-arch, non-root, immutable-tagged, and pullable
      (`docker logout && docker pull <image>` from a clean shell). Verify per
      image with the `publish-evenfire-plugin` skill before publishing.
- [ ] `.status.phase: active` on a fresh install AND real pods `Running` in every
      namespace the plugin uses.

When all of these hold, hand off to `publish-evenfire-plugin`.
