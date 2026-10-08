# Networking: addresses, Services, and NetworkPolicies

Every plugin namespace denies traffic by default (DNS excepted). A call that no
policy allows does not fail fast: it hangs until a timeout. These are the paths
WRC opens for your workloads; anything else stays closed. Verified against
evenfire-ai/evenfire `dev` at `0b26101eb` (`workflow-recipes/src/reconciler/`).

## Addresses: always `{{id:host}}` and `{{id:port}}`

WRC creates a ClusterIP Service for every non-MCP workload that declares a
`port`, named like the workload's scoped name
(`<recipe>-<id>-<8 hex>`, see `status.workloadInstances`), with
`port = targetPort = workloads[].port`. Point one workload at another with the
templates, which resolve to the real Service:

```yaml
env:
  - { name: PG_HOST, value: '{{db:host}}' }        # <scoped-name>.sandbox-recipes.svc.cluster.local
  - { name: PG_PORT, value: '{{db:port}}' }
  - { name: API_BASE, value: 'http://{{api:host}}:{{api:port}}' }
```

Never hardcode `<id>.<namespace>.svc.cluster.local` in `env`, `command` or
`args`: the scoped Service has another name, and WRC treats an unknown
`*.sandbox-recipes.svc.cluster.local` host in those fields as an invalid
internal dependency and fails the recipe.

## Path 1: backend to backend (inferred, automatic)

After templates resolve, WRC scans every non-UI workload's `env`, `command` and
`args` for cluster-local hostnames of its own Services and opens a matching
egress (on the source) and ingress (on the target) NetworkPolicy, port included
(`wr-intdep-egress-*`, `wr-intdep-ingress-*`). So `PG_HOST: '{{db:host}}'` on
`api` is enough for `api` to reach `db` on `db`'s `port`. Rules:

- If the string also names a port (`host:5432`), it must equal the target's
  `port`, or the recipe fails with `InvalidInternalDependency`.
- The target must have a `port` and must not be the UI workload.
- A `stdio` MCP workload must neither reference a sibling's host nor be
  referenced by one: WRC reports `OwnershipConflict` and the recipe fails (the
  platform's MCP controller owns those pods).
- Any issue in this lane sets the condition `InternalDependenciesReady=False`
  and the recipe goes to `failed` with the reason in `status.message`.

## Path 2: explicit egressBindings (non-UI, non-MCP workloads)

`workloads[].egressBindings[]` (max 20) items are
`{egressClass?, dns, port, protocol?}`:

- **Public host (`exact-host`, the default):** `dns` must be a lowercase public
  DNS name (no IP, no CIDR, no wildcard, no `:port` or scheme, nothing ending in
  `.local`, `.internal`, `.svc`, `.cluster.local`), and `port` is required.
  WRC resolves the name's IPv4 A records into `/32` rules and re-resolves on a
  timer, keeping recently seen addresses for an overlap window. A name with no
  usable public IPv4 address (NXDOMAIN, IPv6 only, or a private/blocked range)
  fails the recipe; a temporary resolver error degrades it and retries.
- **Sibling (`<workloadId>.<namespace>.svc.cluster.local`):** the first label is
  the sibling's workload **id** (not its Service name) and the namespace must be
  the source workload's own namespace. WRC opens egress on the source and
  ingress on the target. With templated addresses (Path 1) this is optional.
- **`public-web`:** rejected on these workloads ("public-web is only supported
  on MCP transport workloads"). List every host as `exact-host` instead.

`egressBindings` on the UI workload are ignored; the UI uses `spec.ui.egress`.

## Path 3: the UI (`spec.ui.egress`)

- `internal[] {workloadRef, port}`: lets the UI pod (in `sandbox-ui`) reach a
  backend in `sandbox-recipes`, with the matching ingress on the backend. This
  is what the UI's reverse proxy needs; without it the request times out even
  though both pods are healthy. Targets cannot be MCP workloads.
- `external[] {fqdn, port, reason?}`: public hosts the UI pod itself may call,
  resolved like `exact-host`. A browser-side call never uses this (the embed's
  CSP blocks cross-origin requests); it is for the UI server. Most plugins keep
  third-party calls in the backend instead.

## Path 4: MCP workloads

A workload with `transport` runs in `mcp-server`. WRC creates its Service and an
`McpServer` object, and the platform's MCP controller builds its policies:

- To reach its backend in `sandbox-recipes`, declare a binding:
  `bindings: [{ from: mcp, to: api, port: 8080 }]`. WRC rejects any binding
  that does not connect exactly one MCP workload with one non-MCP workload, so
  bindings are never the tool for backend-to-backend traffic.
- For outbound internet, the MCP workload's `egressBindings` accept
  `exact-host` entries and also `public-web` (public TCP 80/443 with private
  and metadata ranges blocked; it must not carry `dns`, `port` or `protocol`).
  Cluster-local `egressBindings` are not allowed on MCP workloads.

## Path 5: workflow runtimes

Snippet HTTP calls use `spec.runtimeEgress.http` plus each snippet's
`run.capabilities.http`; see [workflows.md](workflows.md). Background-OAuth
workloads (`oauthClientRefs`) get their route to the platform's token broker
automatically.

## What opens nothing

- `dependsOn` only orders deployment.
- `security.isolationLevel` changes the security context, not the network.
- Declaring a webhook opens the path from the platform's webhook gateway to the
  handler workload; it does not open anything else.

## Debugging a hang

1. Is the target addressed with `{{id:host}}`/`{{id:port}}`?
2. Does a path above cover source to target on that port? List what WRC made:
   `kubectl get networkpolicy -n <namespace> -l clerum.io/recipe=<recipe>`.
3. Read `status.conditions` (`InternalDependenciesReady`) and `status.message`.
4. For a public host, does it resolve from the cluster, and is it spelled in
   lowercase without a scheme?
