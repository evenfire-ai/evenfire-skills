# Validate, install, verify, update, recover

Verified against evenfire-ai/evenfire `dev` at `0b26101eb`
(`control-api/src/routes/admin/{recipes,registry}.ts`,
`workflow-recipes/src/reconciler/workflowRecipeReconciler.ts`,
`control-ui/app/workflow-recipes/`).

## Validation happens in three places

| Layer | Runs on | Checks |
|---|---|---|
| CRD admission (schema + CEL) | every create and update, including `kubectl apply` | types, enums, limits, the R/W/O/PS rules |
| Control API, registry install | Marketplace installs | limits, `public-web` on non-MCP workloads, Secret references (reserved names, owned by another recipe) |
| Control API, create/edit/validate | the Control UI recipe editor and `/api/v1/admin/recipes` | all of the above plus name, inline secrets in `env`, template references, namespace policies, name availability, the scheduled-recipe team label |
| WRC (the recipe controller) | every reconcile, after install | bindings shape, `egressBindings`, internal dependencies, unresolved templates, SDK callers (PS4), webhook targets (W2), Secret ownership, DNS of egress hosts |

`kubectl apply` skips the Control API layers. An admin session can run the
second layer without creating anything: `POST
/api/v1/admin/recipes/validate?mode=create` with the recipe as JSON returns
`200 {valid: true, pendingCredentials}` or `422 {valid: false, errors}`.

Check layer 1 before installing, without a cluster, with
[kubectl-validate](https://github.com/kubernetes-sigs/kubectl-validate) and
the CRD file from the platform repository:

```bash
kubectl-validate recipe.yaml --local-crds <evenfire>/charts/clerum-crds/crds --version 1.30
```

or against a cluster: `kubectl apply --dry-run=server -n sandbox-recipes -f
recipe.yaml`. Then run this skill's `scripts/recipe-preflight.py recipe.yaml`,
which checks the Control API and WRC rules that admission cannot see.

## Installing

- **From the registry** (the Control UI Marketplace, which is where the
  Plugins page's Install leads, or `POST
  /api/v1/admin/registry/install-recipe`): Control API keeps only the entry's
  `spec` (max 100 KB) and names the object
  `recipe-<entry name slug>-v<version with dots as dashes>-<8 hex>`, for
  example `recipe-acme-notes-v1-0-0-<hash>` for `@acme/notes` 1.0.0, unless the
  request sets `recipeName`. The hash comes from the entry name and version, so
  installing the same version twice fails (409). None of your labels or
  annotations are kept (so not `clerum.io/workflow-team-id` or
  `clerum.io/pvc-retention` either); Control API adds `clerum.io/managed-by:
  control-api` and the catalog id and version as annotations.
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
- The Desktop only lists the plugin while it is `active`.
- With the SDK, also read `status.pluginWorkloadSdk.state` (it can say
  `awaiting_policy` while the phase is `active`).

## Updating a running plugin

- **Without `steps`** (most UI plugins): build and push a new tag, then change
  the recipe. WRC compares a hash of each rendered object and replaces only
  what changed, so the rollout follows within seconds.
- **Changing an installed registry recipe in place** is `POST
  /api/v1/admin/registry/upgrade-recipe` with `{recipeName,
  registryEntryName, registryEntryVersion}` (admin API; the Control UI has no
  button for it). It keeps the in-cluster name, so Secrets, grants and PVC data
  carry over. Installing the newer version from the Marketplace instead
  creates a second, independent recipe with a new name.
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

- PVCs, including StatefulSet volumes (data survives; delete them yourself);
- the Secrets you created;
- for recipes without `steps`, the user and team grants (a reinstall under the
  same name inherits them);
- Plugin SDK grants, as `disabled`, which block saving new grants for the same
  in-cluster name: delete them on the Plugin SDK page before reinstalling.
