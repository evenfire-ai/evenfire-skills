---
name: run-debug-evenfire
description: >-
  Bring up an Evenfire/Clerum stack and debug it: start the full stack locally in
  minikube, deploy to a dev GKE cluster, launch the desktop app with the right
  port-forwards, and systematically diagnose a broken WorkflowRecipe, MCP server,
  sandbox UI, webhook, OAuth integration, or workflow run. Covers the allowed
  kubectl contexts, the make targets, the port map, seed data, and a pod-level
  failure playbook. Use when the task is to run Evenfire, stand up a test cluster,
  launch the desktop app, or figure out why a plugin/recipe/agent is not working.
---

# Running and debugging Evenfire

> **Verified against Evenfire `dev` at commit `f9e8d0487`.**
> CRD API `clerum.io/v1alpha1`, `clerum-crds` chart `0.8.0`. Status enums,
> endpoints, and `make` targets below match that revision.

This skill assumes you have the platform monorepo checked out (the `Makefile`,
`scripts/minikube/`, and `deploy/` live there); the minikube and gcp make targets
are not in the open-source service worktree. All commands are verified against the
platform `Makefile` and scripts.

## 1. Allowed clusters (safety-critical)

Only ever run `kubectl` against these contexts, and always pass `--context`
explicitly. Never rely on `kubectl config current-context`, the machine may have
unrelated production clusters, and a bare `kubectl apply|delete|patch` against the
wrong one is a destructive accident.

| Context | Cluster | Use |
|---|---|---|
| `clerum-test` | minikube (local) | local dev and testing |
| `clerum-codex-*` / `clerum-detached-*` | minikube (generated profile) | branch/PR validation only |
| `gke_eventfire-491421_us-central1-a_clerum-dev` | GKE dev | dev environment |
| `gke_eventfire-491421_us-central1-a_clerum` | GKE prod | production (every mutation needs `CONFIRM=yes`) |
| `gke_eventfire-491421_us-central1-a_evenfire-hub` | GKE registry | the shared MCP/recipe registry |

When unsure whether an action targets the right cluster, confirm first:

```bash
kubectl config current-context
kubectl --context=<target> get nodes -o name | head -1
```

## 2. Local full stack (minikube)

```bash
make minikube-setup                 # cluster -> CRDs -> keys -> secrets -> BUILD :test images -> deploy -> verify (REBUILDS the DB)
make minikube-setup ARGS="--skip-build"     # redeploy, reuse the already-loaded images
make minikube-setup ARGS="--skip-uis"       # build backend services only (skip Control/Profile UI + Desktop)
REUSE_DB=true make minikube-setup           # preserve the postgres PVC (setup rebuilds it by default; ARGS="--keep-db" is equivalent)
make minikube-status                # OK when readyReplicas==replicas, else "!! <ready>/<desired>"
```

Facts that trip people:

- `make minikube-setup` BUILDS the images from your local source (tagged
  `clerum/*:test`) and loads them into minikube by default; it does not pull
  published images. `--skip-build` reuses the images already loaded. `--skip-uis`
  (or `SKIP_UIS=true`) deploys backend services only: it skips the Control/Profile
  UI and Desktop builds AND drops the Control/Profile UI cluster deployments
  (it renders the `minikube-no-uis` overlay), so the Control UI is not reachable
  after it. A local code change is picked up by a plain `make minikube-setup`.
- `make minikube-setup` REBUILDS the postgres volume by default: it deletes the
  `control-postgres-data` PVC and redeploys, wiping admin/setup data every run
  (the migration gate rejects a stale volume). Preserve an existing volume with
  `REUSE_DB=true` (or `ARGS="--keep-db"`). `--reset-db` only forces the default;
  `make minikube-db-reset` resets standalone. No confirmation gate — a reset is
  immediate.

Port-forwards (keep running in a terminal):

```bash
make minikube-pf-all        # UI 3000, profile-ui 3001, control-api 8090, external-rest-api 8091,
                            # rpc-proxy 8094, registry-api 8085, approval-reader 8098, mcp-host 8080
make minikube-pf-desktop    # control-api 8090, external-rest-api 8091, rpc-proxy 8094 (desktop app only)
```

First-time data and keys:

```bash
CONTEXT=clerum-test scripts/minikube/seed-test-data.sh   # user -> agent associations (fixes "no agents after login")
make minikube-gen-keys                                   # JWT keys; auto-runs the sync below
make minikube-sync-auth-key                              # copy rpc-proxy public key into mcp-host-config (fixes chatllm 401)
make minikube-logs SVC=<deploy> NS=<namespace>           # tail a service
```

For Control UI marketplace flows locally (browsing or installing published
registry entries), also stand up the registry beside the stack:
`make minikube-deploy-evenfire-registry`.

Branch/PR profiles (`clerum-codex-*` / `clerum-detached-*`) must use their own
profile-owned random ports, not the shared fixed ports; the port-forward stack
hard-fails if a branch profile tries to bind the shared defaults.

## 3. Dev GKE cluster

Dev targets do not gate; every prod mutation requires `CONFIRM=yes`.

```bash
make gcp-dev-deploy-service SVC=<svc>    # build linux/amd64 -> push sha tag -> bump overlay -> apply
make gcp-dev-pf-desktop                  # control-api 8090, external-rest-api 8091, rpc-proxy 8094
make gcp-dev-status
make gcp-dev-logs SVC=<svc> NS=<ns>
make gcp-dev-db-reset
```

GKE port-forwards use the gcloud auth plugin; when the token expires they fail
non-interactively (`Reauthentication failed ... run: gcloud auth login`), which
you cannot do from a non-interactive shell. If port-forwards die overnight or
after a pod roll, the usual cause is an expired gcloud token, re-run
`gcloud auth login`. A resilient wrapper that respawns kubectl on pod rolls lives
at `scripts/dev/resilient-kubectl-port-forward.sh <context> <ns> <svc> <local> <remote>`.

## 4. Desktop app

```bash
cd desktop-app
npm install                    # after any pull that changed deps
npm run build                  # tsc main + vite renderer
env -u ELECTRON_RUN_AS_NODE npm start
```

`env -u ELECTRON_RUN_AS_NODE` is required from an Electron-based shell (VS Code,
Cursor): those shells export `ELECTRON_RUN_AS_NODE=1`, which makes electron start
as plain Node and silently never open a window. The localhost profile talks to
`127.0.0.1:8091` (external-rest-api) and `127.0.0.1:8094` (rpc-proxy), so it needs
`make *-pf-desktop` running. Cloud profiles (an `externalRestApiBaseUrl` that is a
public host) reach the cluster over the public ingress and need no port-forwards
or gcloud, prefer those when available. A backgrounded `npm start` often reports
a non-zero exit while the app keeps running detached; verify by process and by an
established connection to `:8091`, not by the task exit code.

## 5. Debug playbook for a deployed recipe

### 5.1 Find the live objects

The in-cluster recipe name is `recipe-<entry-slug>-v<version>-<hash>` for a
registry install (version dots become hyphens, for example
`recipe-evenfire-worktracker-v1-1-0-66ce9c41`) or the bare `metadata.name` for a
hand-apply. Workload naming depends on whether the recipe has `spec.steps`: a
non-workflow recipe (no steps — the typical installed plugin) keeps the bare
`<workloadId>` for non-MCP workloads and names MCP workloads
`<recipeName>-<workloadId>`; a workflow recipe (has steps) hashes every workload
`<recipeName>-<workloadId>-<8hex>`. So never guess a name, resolve by label:

```bash
K="kubectl --context=<ctx>"
# Recipe objects are not labeled with the base name; match the generated name
# (recipe-<entry-slug>-v<ver>-<hash> for a registry install, metadata.name for a hand-apply).
NAME=$($K get workflowrecipes -n sandbox-recipes -o name | sed 's|.*/||' | grep -E '(^|-)<name>(-|$)' | head -1)
$K get workflowrecipe "$NAME" -n sandbox-recipes -o jsonpath='{.status.phase}'; echo
```

### 5.2 Read status correctly

- The healthy phase is **`active`**. The full enum is `candidate, pending-approval,
  approved, pending, pending-operator-input, deploying, testing, active, degraded,
  failed, rolling-back, deprecated, rollback-failed`. There is no `ready` or
  `reconciling` phase.
- `.status.workloads[].ready` for a deployment/statefulset/daemonset reflects
  real replica readiness (`readyReplicas >= desired`), so `ready: true` there
  means those pods are actually Ready. It is a per-reconcile snapshot, and MCP
  transport workloads (materialized by HCC) and one-shot job workloads report
  readiness differently, so for real-time pod state, and for MCP servers, still
  check pods directly. WRC does not surface kubelet failures into
  `.status.conditions[]`.
- A recipe can read `active` with zero pods in its namespaces when the stateless
  lifecycle has suspended it (idle hosts/workloads scale to 0). That is not an
  error, the next request wakes it. Confirm with `kubectl get deploy` replica
  counts before treating "0 pods" as a fault.
- `.status.conditions[]` use the K8s shape; for problem types like
  `WebhookSecretMissing`, `status: False` is the healthy form. Read by
  `(type, status, reason)`, not by `type` alone.
- `.status.workflowExecution.phase` (only when the recipe has `steps`):
  `pending, initializing, running, recovering, completed, failed, cancelled`.

```bash
$K describe workflowrecipe "$NAME" -n sandbox-recipes
$K get workflowrecipe "$NAME" -n sandbox-recipes \
  -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.message}{"\n"}{end}'
```

### 5.3 Check real pod state in every namespace

A split plugin has pods in more than one namespace; the UI can render blank
because a backend in another namespace is crash-looping.

```bash
for ns in sandbox-recipes sandbox-ui mcp-server; do
  echo "== $ns =="; $K get pods -n "$ns" -l clerum.io/recipe="$NAME"
done
$K describe pod <pod> -n <ns>     # events: image pull, secret, scheduling
$K logs <pod> -n <ns>             # app crash reasons
```

Pod failure to cause:

| Symptom | Cause | Where it shows |
|---|---|---|
| `ImagePullBackOff` / `ErrImagePull` | image not public, missing `imagePullSecrets`, wrong repo path (422 cross-org), or nonexistent tag | pod events |
| `CreateContainerConfigError` | a required `envSecret` key references a missing Secret/key (often the Secret exists in `sandbox-recipes` but the MCP workload needs it in `mcp-server` too) | pod events (`secret "X" not found`) |
| `CrashLoopBackOff` | app crash: unresolved env, bad config, DB unreachable | pod logs |
| stuck `ContainerCreating` | missing PVC, fsGroup mismatch, CNI | pod events |
| pod Running, calls `ETIMEDOUT` | undeclared egress (default-deny): missing `egressBindings` / `spec.ui.egress` / `runtimeEgress` | pod logs (timeouts, never a clean error) |

### 5.4 Feature-specific checks

- MCP tools missing in chat: confirm the McpServer is Ready in `mcp-server`, then
  grep mcp-host for `[McpManager] Added server: <name>` (a following
  `Removed server` means the Context change reverted). mcp-host re-discovers on a
  ~30s poll, so no restart is needed once the server is Ready and in the Context.
- Sandbox UI blank: HTML 200 but JS never runs is almost always absolute asset
  paths (need `<base href="./">` + relative paths) or a CSP violation
  (`connect-src 'self'` blocks cross-origin fetch). A `502 port_not_allowed` at
  view time means `ui.port` is not 8080.
- OAuth not connecting: a missing client Secret returns
  `503 integration_not_configured` from the embed's authorize-url/token call.
- Webhook: `WebhookSecretMissing` condition (required secret) or a `410` with
  `X-Clerum-Webhook-State: dormant` (optional webhook, secret not yet created).
- Workflow stuck in `running`: the coordinator is blocked (NetworkPolicy, expired
  token, mcp-host unreachable). Read `kubectl logs -n control-plane
  deploy/workflow-recipes` and the coordinator pod.
- Artifacts from a run: download via control-api
  `GET /api/v1/admin/recipes/:name/artifacts/:artifactName/download`.
- Controller logs (all in `control-plane`): `make minikube-logs SVC=workflow-recipes NS=control-plane`
  (WRC reconciler + coordinator), `SVC=host-context-controller` (MCP servers,
  NetworkPolicies, desktop pods), `SVC=control-api` (admin API, registry, auth).
  On dev use `make gcp-dev-logs SVC=<svc> NS=control-plane`.

### 5.5 Quick read-only health sweep

The bundled `scripts/evenfire-doctor.sh <context> [recipe-base-name]` runs a
read-only sweep: platform namespaces present, control-plane deployments Ready,
recipe `.status.phase`, and any non-Running pods across `sandbox-recipes` /
`sandbox-ui` / `mcp-server`. It never mutates anything.

```bash
scripts/evenfire-doctor.sh gke_eventfire-491421_us-central1-a_clerum-dev my-plugin
```

## 6. Common issues and fixes

| Symptom | Fix |
|---|---|
| 409 on first-time admin setup | re-run `make minikube-setup` (rebuilds the DB by default), or `make minikube-db-reset` |
| Pod `ImagePullBackOff` in minikube | `make minikube-setup` (rebuilds the `:test` images and loads them; do not pass `--skip-build`) |
| `401 Invalid token` from chatllm | `make minikube-gen-keys` then `make minikube-sync-auth-key` |
| Port-forward dropped | re-run `make minikube-pf-all`; on GKE check gcloud auth (`gcloud auth login`) |
| No agents after login | `CONTEXT=clerum-test scripts/minikube/seed-test-data.sh` |
| Desktop app never opens a window | launch with `env -u ELECTRON_RUN_AS_NODE npm start`, prefer a cloud profile |
