# Validate, install, verify, update, recover

Verified against evenfire-ai/evenfire `dev` at `0b26101eb`
(`control-api/src/routes/admin/{recipes,registry}.ts`,
`workflow-recipes/src/reconciler/workflowRecipeReconciler.ts`,
`control-ui/app/workflow-recipes/`).

## Where validation happens

Three components validate a recipe: the Kubernetes API server (admission),
Control API (with different checks per route), and WRC.

| Layer | Runs on | Checks |
|---|---|---|
| CRD admission (schema + CEL) | every create and update, including `kubectl apply` | types, enums, limits, the R/W/O/PS rules |
| Control API, registry install | Marketplace installs | limits, `public-web` on non-MCP workloads, Secret references (reserved names, owned by another recipe, a missing `imagePullSecrets` Secret) |
| Control API, create/edit/validate | the Control UI recipe editor and `/api/v1/admin/recipes` | all of the above plus name, inline secrets in `env`, template references, namespace policies, name availability, the scheduled-recipe team label |
| WRC (the recipe controller) | every reconcile, after install | bindings shape, `egressBindings`, internal dependencies, unresolved templates, SDK callers (PS4), webhook targets (W2), Secret ownership, DNS of egress hosts |

`kubectl apply` skips the Control API layers. An admin session can run the
Control API create checks without creating anything: `POST
/api/v1/admin/recipes/validate?mode=create` with the recipe as JSON returns
`200 {valid: true, pendingCredentials}` or `422 {valid: false, errors}`.

Check before installing, without a cluster. Download the CRD file of the
pinned revision, run this skill's preflight with it (schema errors, then the
Control API and WRC rules that admission cannot see), and
[kubectl-validate](https://github.com/kubernetes-sigs/kubectl-validate) for
the CEL rules:

```bash
mkdir -p crds && curl -fsSL -o crds/workflowrecipe.yaml \
  https://raw.githubusercontent.com/evenfire-ai/evenfire/0b26101eb247adcc44f70d39451f3e82c3225d47/charts/clerum-crds/crds/workflowrecipe.yaml
python3 scripts/recipe-preflight.py --crd crds/workflowrecipe.yaml recipe.yaml
kubectl-validate recipe.yaml --local-crds crds --version 1.30
```

Against a cluster, `kubectl apply --dry-run=server -n sandbox-recipes -f
recipe.yaml` replaces `kubectl-validate`.

## Installing

- **From the registry** (the Control UI Marketplace, which is where the
  Plugins page's Install leads, or `POST
  /api/v1/admin/registry/install-recipe`): Control API keeps only the entry's
  `spec` (max 100 KB) and, unless the request sets `recipeName`, names the
  object `recipe-<slug>-v<version with dots as dashes>-<hash>`. The slug is the
  entry name with every character outside `a-z`, `0-9` and `-` turned into
  `-` (repeats collapsed, ends trimmed, at most 40 characters); the hash is the
  first 8 hex digits of the SHA-256 of `<entry name>:<version>`. So
  `@acme/notes` 1.0.0 becomes `recipe-acme-notes-v1-0-0-ac24adda`, and the
  name can be computed before installing (for example to label a pull Secret):

  ```bash
  printf '%s' '@acme/notes:1.0.0' | shasum -a 256 | cut -c1-8   # ac24adda
  ```

  Installing the same version twice fails (409). Keep entry names short: the
  name is cut to 63 characters before its `mcp-` prefix becomes `recipe-`, so a
  long entry name with a multi-digit version yields up to 66 characters, more
  than the `clerum.io/recipe` label can hold. None of your labels or
  annotations are kept (so not `clerum.io/workflow-team-id` or
  `clerum.io/pvc-retention` either); Control API adds `clerum.io/managed-by:
  control-api` and the catalog id and version as annotations. Both registry
  routes accept `inputValues` ([recipe-fields.md](recipe-fields.md#inputs-and-templates)).
- **Admin API** `POST /api/v1/admin/recipes` (JSON body): keeps your
  `metadata.name`, drops `metadata.namespace`, and keeps only the
  `clerum.io/workflow-team-id` label. The Control UI's recipe editor uses
  `PUT /api/v1/admin/recipes/<name>` to edit an installed recipe.
- **`kubectl apply -n sandbox-recipes`** (development clusters): keeps the file
  as written and skips the Control API checks. The cluster's admission policy
  still requires the `sandbox-recipes` namespace, no `ownerReferences`, and
  `triggers` on recipes with `steps`.

Never hardcode the in-cluster name. Find it by listing recipes and matching
your entry:

```bash
kc() { kubectl --context "$CTX" "$@"; }       # always pass an explicit context
kc -n sandbox-recipes get workflowrecipes -o name | sed 's|.*/||' | grep -E '(^|-)notes(-|$)'
```

## After installing

1. Create the Secrets the recipe needs, with ownership labels (see
   [secrets-and-images.md](secrets-and-images.md)). The install response lists
   the missing ones as `pendingCredentials`. Workloads that need a missing
   Secret wait and recover by themselves when it appears (pull Secrets for a
   third-party registry are the exception: they must exist before install).
2. Grant access on the plugin's page in the Control UI: Members (users) and
   Teams. A team grant counts while the user works in that team in the
   Desktop. Nobody sees the plugin until it is granted; being an admin does
   not change that.
3. For Desktop notifications or LLM calls, have an operator create the Plugin
   SDK grants ([plugin-workload-sdk.md](plugin-workload-sdk.md)).
4. For chat agents, have an operator add the plugin's MCP server to the
   agent's Context (the `create-evenfire-mcp-server` skill).

## Verifying

```bash
R=<in-cluster name>
kc -n sandbox-recipes get workflowrecipe "$R" \
  -o jsonpath='{.status.phase}{"  "}{.status.message}{"\n"}'          # expect: active  All workloads deployed
kc -n sandbox-recipes get workflowrecipe "$R" -o jsonpath='{.status.workloadInstances}{"\n"}'
for ns in sandbox-recipes sandbox-ui mcp-server; do
  kc -n "$ns" get pods -l clerum.io/recipe="$R"
done
kc -n sandbox-recipes get workflowrecipe "$R" -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.reason}: {.message}{"\n"}{end}'
```

- Healthy is `active`. `status.workloads[].ready` is real readiness for
  Deployments (updated, ready and available replicas).
- `degraded` means something is not ready or a webhook Secret is missing; the
  message says which. `failed` means WRC stopped reconciling (below).
- The Desktop lists and opens the plugin only while it is `active`, so a
  `degraded` recipe (one workload not Ready, the MCP server included) takes the
  whole UI offline.
- With the SDK, also read `status.pluginWorkloadSdk.state`: it can say
  `awaiting_policy` while the phase is `active`, and the message then reads
  `Plugin Workload SDK operator policy pending (<reason>)` instead of
  `All workloads deployed`.

## Updating a running plugin

- **Without `steps`** (most UI plugins): build and push a new tag, then change
  the recipe. WRC compares a hash of each rendered object and replaces only
  what changed, so the rollout follows within seconds.
- **Changing an installed registry recipe in place** is `POST
  /api/v1/admin/registry/upgrade-recipe` with `{recipeName,
  registryEntryName, registryEntryVersion}` (admin API; the Control UI has no
  button for it). It keeps the in-cluster name, so Secrets, grants and PVC data
  carry over, and it keeps the object's labels and annotations. It replaces
  the whole `spec` with the new version's: edits made in the cluster are lost,
  and `inputValues` must be sent again. Installing the newer version from the
  Marketplace instead creates a second, independent recipe with a new name.
- Removing a workload from the recipe does **not** delete its Deployment,
  Service or StatefulSet; delete them yourself.
- **With `steps` and no `pluginWorkloadSdk`:** once `active`, WRC does not
  re-apply workloads, so an image change in the recipe does not reach the
  running pods. Plan for reinstalling, or keep long-running services in a
  separate stepless recipe.
- A manual `kubectl set image` on a WRC-owned Deployment survives only until
  the next change to that workload in the recipe; WRC then writes the recipe's
  image back. Always update the recipe.
- Re-pushing the same tag never rolls out (`IfNotPresent`).
- StatefulSet fields Kubernetes treats as immutable (volume claim templates,
  service name, selector) cannot change in place; WRC reports
  `StatefulSetImmutableDrift`. Changing them means a new install and a data
  migration.

## When the recipe is `failed`

`failed` stops reconciliation for a recipe without steps: fixing the spec is
not enough. Fix the cause, then retry:

- Control UI: the plugin's page, Retry.
- API: `POST /api/v1/admin/recipes/<name>/retry` (only from `failed`; moves it
  to `candidate`, after which WRC reconciles and settles on `active` or
  `degraded`).

WRC retries by itself only after transient infrastructure errors, or when every
workload it last saw was ready. `Policy violation: ...` failures (a
`WorkflowRecipePolicy` in the namespace) stay until the spec complies.

## Common messages

| Message or symptom | Cause | Fix |
|---|---|---|
| `EnvSecretOwnershipDenied` | Secret unlabeled, labeled for another recipe, or both labels | Label `clerum.io/owner-recipe=<in-cluster name>` |
| pod `CreateContainerConfigError` | a required `envSecret` key or Secret is missing | Create it in the workload's namespace |
| `ImagePullBackOff` | wrong image or tag, a private registry without a pull Secret, or a platform-registry image on a cluster connected to another registry | Fix the image or pull Secret |
| `Unresolved template reference: "x"` | `{{...}}` that is not `inputs.`, `computed.`, `<id>:host/port` or a resource key | Fix or remove the braces |
| `InternalDependenciesReady=False` ... `not an eligible runtime Service` | a hardcoded `*.sandbox-recipes.svc.cluster.local` host | Use `{{id:host}}` |
| `public-web is only supported on MCP transport workloads` | `public-web` on a backend | List hosts as `exact-host` |
| `binding must connect exactly one MCP transport workload to one non-transport workload` | `bindings` used between two backends | Use templates or cluster-local `egressBindings` |
| `egress resolution failed` | an egress host without public IPv4 records | Fix the host name |
| `snippet workflow runtime is disabled` | snippet steps on a cluster without the snippet runtime | Operator, or avoid snippet steps |
| `Plugin Workload SDK disabled after confirmed teardown` | SDK block on a cluster without the SDK | Operator, or remove the block |
| `Webhook gateway disabled: ...` (`WebhookSecretMissing`) | a required webhook's Secret is missing, lacks the key, or is not owned by the recipe | Create or label the Secret, or make the webhook `optional` |
| webhook answers `410 integration_not_configured` (`WebhookDormant`) | an `optional` webhook without its Secret | Create the Secret |
| webhook answers `500 verifier_misconfigured` | `jwt-bearer-jwks` or `stripe-verify`, which do not work at this revision, or an unreadable Secret | [webhooks-and-oauth.md](webhooks-and-oauth.md) |
| Desktop shows "is updating" | the recipe is not `active`, or the UI pod is not Ready | Read `status.message` and the UI pod |
| Desktop `502 port_not_allowed` | the UI listens on a port other than 8080 | Use 8080 |
| Plugin API calls 401 `sandbox_ui_session_invalid` | expired embed session | `clerum.requestSessionRefresh()` ([ui-embed.md](ui-embed.md)) |
| Calls to a host hang | no network path | [networking.md](networking.md) |

## Uninstalling

Uninstall from the plugin's page in the Control UI (`DELETE
/api/v1/admin/recipes/<name>`). WRC removes the workloads the recipe declares
at that moment, their Services, the `spec.resources` Secrets and ConfigMaps,
the MCP registrations, the private Context and most of its NetworkPolicies. It
keeps:

- PVCs, including StatefulSet volumes (data survives; delete them yourself).
  With the annotation `clerum.io/pvc-retention: delete` on the recipe
  (`kubectl annotate`), WRC deletes its `resources[]` PVCs, but never
  StatefulSet volumes;
- the Secrets you created;
- for recipes without `steps`, the user and team grants (a reinstall under the
  same name inherits them);
- Plugin SDK grants, as `disabled`, which block saving new grants for the same
  in-cluster name: delete them on the Plugin SDK page before reinstalling.
