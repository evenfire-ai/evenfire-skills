# WorkflowRecipe field reference

Every field of `apiVersion: clerum.io/v1alpha1`, `kind: WorkflowRecipe`, with
the limits the platform enforces. Verified against
[evenfire-ai/evenfire](https://github.com/evenfire-ai/evenfire) `dev` at
`0b26101eb`: the CRD schema and CEL rules in
`charts/clerum-crds/crds/workflowrecipe.yaml` and the controller in
`workflow-recipes/` (WRC). Some CRD `description` texts are older than the
controller; where they disagree this file follows the controller and says so.

Fields the CRD does not define never take effect. `kubectl apply` (strict
field validation, the kubectl default) and `kubectl-validate` reject them;
Control API (Marketplace, admin API) sends recipes without that option, so the
API server drops them silently. A typo such as `when:` or `startupProbe:`
therefore fails loudly with kubectl and disappears through Control API.

## Object and metadata

- `metadata.name`: lowercase RFC 1123 label, at most 63 characters. Control
  API's create route rejects anything else, and a longer name breaks the
  `clerum.io/recipe` label WRC puts on every object.
- The recipe object lives in `sandbox-recipes`. Leave `metadata.namespace` out
  (or set exactly `sandbox-recipes`): the cluster's admission policy rejects any
  other namespace, and WRC refuses to reconcile one.
- A registry install renames the object (see
  [operate.md](operate.md)), so code and scripts must never assume the recipe
  name you wrote.
- WRC reads two optional metadata entries: the label
  `clerum.io/workflow-team-id` (scheduled workflows, see
  [workflows.md](workflows.md)) and the annotation `clerum.io/pvc-retention`
  (`retain` by default; `delete` makes an uninstall delete the recipe's
  `resources[]` PVCs, never StatefulSet volumes). A registry install keeps none
  of your labels or annotations; the admin API keeps only the team label and
  drops your annotations. The annotation is read at uninstall time, so an
  operator can add it later with `kubectl annotate`; `upgrade-recipe` keeps
  the object's existing labels and annotations.

## spec at a glance

| Field | What it does | Detail |
|---|---|---|
| `description` | Free text shown to operators | Put install prerequisites here (Secrets, grants) |
| `workloads[]` | Containers WRC deploys | Below; max 25 |
| `ui` | Marks one workload as the Desktop app view | Below; [ui-embed.md](ui-embed.md) |
| `bindings[]` | MCP workload to backend network link | Below; [networking.md](networking.md) |
| `resources[]` | PVCs (and Secrets/ConfigMaps) created with the recipe | Below |
| `security` | Pod isolation level, `allowContextRef` | Below |
| `contextRef` | Register MCP workloads in an existing Context | Below |
| `pluginWorkloadSdk` | Desktop notifications and the LLM prompt bridge | [plugin-workload-sdk.md](plugin-workload-sdk.md) |
| `agent` | Default LLM provider/model | Workflows, or the prompt-bridge bootstrap |
| `steps[]`, `triggers`, `scheduling`, `runRetention`, `output`, `mcpServers[]`, `runtimeEgress`, `coordinatorImage` | Workflow runs | [workflows.md](workflows.md) |
| `webhooks[]`, `oauthClients[]` | Inbound provider webhooks, OAuth clients | [webhooks-and-oauth.md](webhooks-and-oauth.md) |
| `inputs`, `inputContract`, `profiles`, `activeProfile`, `computed` | Install-time values for templates | Below |
| `gfs` | Global File System intents | Below |
| `dependencies[]` | Declared in the CRD | Not acted on by WRC at this revision |

## workloads[]

Required: `id`, `type`, `image`.

| Field | Values and limits | Notes |
|---|---|---|
| `id` | `^[a-z][a-z0-9-]*$`, max 63, unique | Labels pods as `clerum.io/workload=<id>` |
| `type` | `deployment`, `statefulset`, `cronjob`, `job`, `daemonset` | `cronjob` requires `schedule` (CEL) |
| `image` | string | Use an immutable tag (see `imagePullPolicy`) |
| `imagePullPolicy` | `Always`, `IfNotPresent`, `Never` | Accepted but not honored: WRC renders `IfNotPresent`, and for `stdio` MCP servers the platform's own setting applies. Never reuse a tag |
| `port` | integer | Primary container port; the Service and `{{id:port}}` use it |
| `replicas` | 0 to 20, default 1 | The UI workload must be 1 (R16); an operator may set a lower runtime cap for StatefulSets |
| `command`, `args` | string arrays | Templates are resolved here |
| `env[]` | `{name, value?}` | Strings only. No `valueFrom`, no `envFrom` |
| `envSecret` | `{name, keys[{secretKey, envVar, optional?}]}` | The only Secret-to-env path; see [secrets-and-images.md](secrets-and-images.md) |
| `volumeMounts[]` | `{name, mountPath, subPath?, readOnly?}` | `name` = a `resources[]` PVC id, a `volumeClaimTemplates[].name`, or anything else for an emptyDir |
| `volumeClaimTemplates[]` | `{name, storageClass, accessMode, size}`, max 4 | StatefulSet only; all four fields required. Name a class the cluster has (`kubectl get storageclass`): an empty string turns off dynamic provisioning instead of selecting the default |
| `resources` | `requests`/`limits` with `cpu`, `memory` | Kubernetes quantities as strings |
| `healthCheck` | see below | One block drives both liveness and readiness |
| `dependsOn[]` | workload ids | Deploy order only; opens no network path |
| `imagePullSecrets[]` | Secret names | Ownership-checked like `envSecret`; see [secrets-and-images.md](secrets-and-images.md) |
| `oauthClientRefs[]` | max 8, `^[a-z0-9-]{1,63}$` | Background OAuth; not on MCP or UI workloads |
| `egressBindings[]` | max 20 | See [networking.md](networking.md) |
| `includeWhen` | `{{inputs.KEY}}` | Not CEL despite the CRD text; see below |
| `transport` | `{type: streamableHttp\|sse\|stdio, path}` | Makes the workload an MCP server in `mcp-server`; needs `port`; `stdio` only on a `deployment` |
| `security` | see below | Per-workload overrides |
| `schedule`, `timeZone` | five-field cron, IANA zone | `cronjob` only |
| `serviceName` | string | `statefulset` headless Service name |
| `backoffLimit` | integer, default 3 | `job` only |

Where a workload runs is decided by rule, never by you:

- has `transport` -> namespace `mcp-server`
- is `spec.ui.workloadRef` -> namespace `sandbox-ui`
- anything else -> namespace `sandbox-recipes`

### Names WRC gives your objects

Pods run without a Kubernetes service-account token, and every workload except
MCP servers runs at the preemptible priority class `clerum-batch`, so the
scheduler may evict it when the cluster is short of capacity.

On first deploy WRC assigns every workload a recipe-scoped name,
`<recipe>-<workloadId>-<8 hex>` (trimmed to 63 characters; StatefulSets and
CronJobs to 52), and stores the mapping in `status.workloadInstances`. The
Service has that same name. Pods carry the labels `clerum.io/recipe=<recipe>`
and `clerum.io/workload=<id>`, which is how you find them. Never hardcode
`<id>.<namespace>.svc.cluster.local` as an address: use `{{<id>:host}}`.

### healthCheck

`{type: http|tcp|exec, path, port, command[], initialDelaySeconds,
periodSeconds, timeoutSeconds, failureThreshold}`.

- The same check becomes the liveness and the readiness probe. When you leave
  the timing out, liveness starts after 10 s every 15 s and readiness after 5 s
  every 10 s. Values you set apply to both probes; an unset `timeoutSeconds`
  or `failureThreshold` takes the Kubernetes default (1 s, 3).
- `http` defaults: `path` `/health`, `port` the workload `port` (else 8080).
  Set `path` explicitly; `/health` is rarely what an app serves.
- There is no `startupProbe` and no `successThreshold`. Because the check is
  also the liveness probe, a health path that fails while the app starts
  (migrations) gets the container restarted after about `initialDelaySeconds +
  (failureThreshold - 1) x periodSeconds` (40 s with the defaults). For a slow
  start, raise `initialDelaySeconds` or `failureThreshold` until that window
  covers it; do not answer 503 until ready.

### security (per workload)

- `runAsUser`, `runAsGroup`, `fsGroup`: integers, minimum 1 (root is rejected).
  Setting `runAsUser` also forces `runAsNonRoot: true`.
- `addCapabilities`: all capabilities are dropped first; only `CHOWN`,
  `FOWNER`, `DAC_OVERRIDE` and `NET_BIND_SERVICE` can be added back (other
  values are rejected).
- `prepareVolumeOwnership: true`: adds a root init container that chowns the
  writable mounts to `runAsUser`. Requires `runAsUser` and a writable mount, and
  an image with `sh`, `chown`, `chmod` (not distroless). Only for storage that
  ignores `fsGroup`.

### includeWhen

The value must be exactly `{{inputs.KEY}}`. The workload is deployed only when
the resolved input is truthy; `false`, `"false"`, `0`, `"0"`, `""`, null and a
missing key exclude it. Anything else, including a CEL expression such as
`inputs.x == true`, resolves to nothing and always excludes the workload.
Bindings and `dependsOn` entries that point at an excluded workload are
dropped. `includeWhen` is applied only to recipes without `steps`.

## ui

Required: `workloadRef`, `port`.

| Field | Values | Notes |
|---|---|---|
| `workloadRef` | an existing workload id | Must be `type: deployment`, `replicas` 1 or unset, no `transport` (R15, R16) |
| `port` | 1 to 65535 | Must equal the UI workload's `port`, and rpc-proxy only allows its configured ports (default `8080`). Either mistake passes the CRD and the app does not open (`502 port_not_allowed`, or a lookup error for a mismatch) |
| `title` | max 100 characters | Name in the Desktop app picker |
| `icon` | `data:<type>;base64,...`, max 32768 characters | Remote URLs are rejected |
| `defaultPath` | `^/([^/\s][^\s]*)?$`, default `/` | No scheme, no `//` (R18) |
| `egress.internal[]` | `{workloadRef, port}`, max 25 | UI to backend; the target must not be an MCP workload (R17) |
| `egress.external[]` | `{fqdn, port, reason?}`, max 20 | DNS names only, no IPs or CIDRs |

Only one UI per recipe. The UI's own outbound access is `ui.egress`, never
`egressBindings` (WRC skips `egressBindings` on the UI workload).

## bindings[]

`{from, to, port, protocol?}` (`TCP` default, or `UDP`). WRC rejects a binding
unless it connects exactly one MCP transport workload with one non-transport
workload. It is how an MCP server in `mcp-server` reaches its backend in
`sandbox-recipes`. Details in [networking.md](networking.md).

## resources[]

`{id, type: pvc|secret|configmap, ...}`; `id` matches `^[a-z][a-z0-9-]*$`.

- `pvc`: `storageClass`, `size`, `accessMode`. A workload mounts it by listing
  a `volumeMounts[]` entry whose `name` is the resource `id`. The PVC is created
  in the namespace of the first workload that mounts it.
- `secret` / `configmap`: `data` (key/value), and for Secrets `generateKeys`
  (random 32-character values, created once and never rotated).

Every resource gets a recipe-scoped physical name
(`<recipe>-<id>-<hash>`, recorded in `status.resourceInstances`). Workloads
cannot reach a Secret or ConfigMap resource by its `id`: `envSecret.name` is a
literal Secret name, and a `volumeMounts[]` entry only becomes a PVC when it
names a `pvc` resource (any other name becomes an empty directory). Use
`resources[]` for PVCs; have operators create the Secrets your workloads read.
`{{<id>:<KEY>}}` resolves only keys you wrote in `data`, never generated ones.

## security (recipe level)

- `isolationLevel` (no CRD default; WRC uses `minimal` when unset):
  - `minimal`: may run as root, writable root filesystem, all capabilities
    dropped, no privilege escalation, seccomp `RuntimeDefault`.
  - `standard`: `minimal` plus `runAsNonRoot` and a read-only root filesystem.
    Mount an emptyDir (any `volumeMounts[]` name that is not a PVC) at every
    path the process writes, such as `/tmp`.
  - `strict`: `standard` plus pod user/group/fsGroup 65534, no service account
    token, and the PodSecurity `restricted` pod label.
  The CRD description talks about egress; at this revision the level only
  changes the security context. Network access is always default-deny. A
  `WorkflowRecipePolicy` in the namespace can require a minimum level.
- `allowContextRef`: see `contextRef`.

All three plugin namespaces enforce PodSecurity `baseline` and warn on
`restricted`.

## contextRef

Optional, even with MCP workloads (the CRD description says "required"; the
controller does not enforce it). Without it WRC creates a private Context
`wf-<recipe>` that lists the recipe's MCP servers, and no chat agent sees them
until an operator adds them to the agent's Context (the
`create-evenfire-mcp-server` skill shows how). On a recipe with `steps`,
setting `contextRef` is refused unless `spec.security.allowContextRef: true`
AND a `WorkflowRecipePolicy` in the namespace also allows it.

## Inputs and templates

Templates are resolved in `env[].value`, `command[]` and `args[]` before pods
are created. An unresolvable reference fails the recipe, so a literal `{{` in
those fields breaks the install.

| Template | Resolves to |
|---|---|
| `{{<id>:host}}` | `<scoped service name>.<namespace>.svc.cluster.local` of a workload that has a `port` |
| `{{<id>:port}}` | that workload's `port` |
| `{{inputs.KEY}}` | the resolved input value |
| `{{computed.NAME}}` | a computed value |
| `{{<resourceId>:KEY}}` | a key you wrote in a Secret/ConfigMap resource's `data` |

Input precedence, lowest to highest: `inputContract` property `default`s, then
`spec.inputs`, then `profiles[activeProfile]`, then `computed`. A profile is a
flat map that overrides input values; it does not patch other spec fields
(despite the CRD description). `computed[]` items are `{name, expression}`
where the expression is a small language over `inputs.KEY` (arithmetic, string
`+`, comparisons, ternary), not `{{...}}` templates.

Values an operator sets at install: the registry admin API (`install-recipe`
and `upgrade-recipe`) accepts `inputValues: {KEY: value}`, which replaces the
`default` of keys declared in `inputContract.properties` (checked against the
property's `type`; other keys are ignored). That is the lowest layer, so for a
key you also set in `spec.inputs` the operator's value is ignored: declare
tunable values only in `inputContract`. The Control UI Marketplace sends no
`inputValues`, and an upgrade replaces the whole spec with the new version's,
so the values must be sent again with every upgrade. Settings an operator must
be able to change without the API can live in a Secret the workload reads
through `envSecret` (an optional key); a changed value applies once the pods
restart.

## gfs

`publishTargets[]` `{drive, target}` and `mounts[]` `{drive, target,
scopes[]}` (scopes: `gfs.read`, `gfs.write`, `gfs.delete`, `gfs.manage_acl`,
`gfs.share`). Both are intents: editing them grants nothing, and a mount the
host identity is not entitled to stays pending.

## status

| Field | Meaning |
|---|---|
| `phase` | The enum has 13 values, but in practice you see `deploying`, `active`, `degraded`, `failed`, and `candidate` (set by Retry). Healthy is `active`; a fresh install is `degraded` until every workload is ready |
| `message` | Last reconcile message; on failure, the reason |
| `workloads[]` | `{id, type, phase, message, ready}`; for Deployments `ready` means updated, ready and available replicas reached the desired count |
| `workloadInstances` | workload id to Kubernetes name |
| `resourceInstances` | resource id to Kubernetes name |
| `conditions[]` | e.g. `InternalDependenciesReady`, webhook and SDK conditions |
| `pluginWorkloadSdk` | `{state: validated\|disabled\|degraded\|awaiting_policy, ...}`; see [plugin-workload-sdk.md](plugin-workload-sdk.md) |
| `workflowExecution`, `steps[]`, `artifacts[]` | Workflow runs; see [workflows.md](workflows.md) |

## Admission rules (CEL), by code

The codes are comments in the CRD; the API server returns only the message.

- Recipe: R1 `agent` needs non-empty `steps` or `pluginWorkloadSdk.promptBridge`;
  (uncoded) `pluginWorkloadSdk` without `steps` cannot set `triggers`,
  `scheduling` or `coordinatorImage`; R2 at least one workload or step; R3
  unique step ids; R4 `scheduling` needs steps; R5/R7 five-field cron; R6
  `triggers` needs `onDemand` or `schedule`; D13 at most one oauth-broker
  provider (`codex-subscription` or `grok-subscription`).
- Steps: R8 `run` xor `instruction`; R9 exactly one of them unless
  `coordinatorImage`; R10 no `agent` on a `run` step; R11/R14 snippet MCP tools
  must be listed explicitly, no wildcards; R12/R13 snippet HTTP hosts must be
  public DNS names also listed in `runtimeEgress.http.allowedHosts`.
- UI: R15 `workloadRef` exists; R16 deployment, 1 replica, no transport; R17
  `egress.internal` targets are non-MCP; R18 no scheme in `defaultPath`.
- OAuth: O1 every client has a consumer (`spec.ui` or a workload's
  `oauthClientRefs`); O3 unique ids; O4 no `oauthClientRefs` on MCP workloads.
- SDK: PS1 at least one family; PS2 no `*` in `allowedEventTypes`; PS3 no `*`
  in `allowedModels`.
- Webhooks: W1 unique ids; W4 `methods` includes POST; W7 `secretRef` unless
  `jwt-bearer-jwks`; W8 `replay` with `hmac-sha256-timestamp-body`; W9/W12 JWKS
  settings; W13 GET only with `setupHandshake`; W14 `meta-hub-challenge` needs
  its `secretRef` and GET.

The cluster also runs an admission policy on every create and update
(including `kubectl apply`): the namespace must be `sandbox-recipes`, clients
may not set `ownerReferences`, and a recipe with `steps` must declare
`triggers.onDemand` or `triggers.schedule`.

WRC adds its own checks after admission (W2 webhook target, PS4 SDK callers,
egress, bindings, Secret ownership, limits) and Control API adds more before
admission; see [operate.md](operate.md).
