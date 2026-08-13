# Recipe field and enum reference

Copy these values verbatim. When a value is not here, read the CRD at
`charts/clerum-crds/crds/workflowrecipe.yaml` and the WRC reconciler rather than
guessing.

- `workloads[].type`: `deployment | statefulset | cronjob | job | daemonset`.
  A `cronjob` workload MUST set the workload-level `schedule`. Max 25 workloads.
- `workloads[]` required trio: `id`, `type`, `image`. Common optionals:
  `port`, `replicas` (0-20), `command`, `args`, `env`, `envSecret`,
  `volumeMounts`, `volumeClaimTemplates`, `resources`, `healthCheck`,
  `dependsOn`, `imagePullSecrets`, `oauthClientRefs`, `egressBindings`,
  `includeWhen`, `transport`, `security`, `schedule`.
- `env[]` items are `{name, value}` strings only. There is **no** `valueFrom`,
  `envFrom`, or `envFromConfigMap`. The only Secret-to-env path is `envSecret`
  (`name` + `keys[{secretKey, envVar, optional?}]`).
- `healthCheck`: `{type: http|tcp|exec, path, port, command[], initialDelaySeconds,
  periodSeconds, timeoutSeconds, failureThreshold}`. There is **no** `startupProbe`
  and **no** `successThreshold`; the one `healthCheck` block serves both liveness
  and readiness. For slow starts, widen `initialDelaySeconds`/`failureThreshold`,
  or return 503 from the health path until ready.
- `security` overrides: `runAsUser`/`runAsGroup`/`fsGroup` (each min 1, so root
  uid 0 is rejected), `addCapabilities` (only `CHOWN|FOWNER|DAC_OVERRIDE|NET_BIND_SERVICE`,
  enforced twice), `prepareVolumeOwnership`. Setting `runAsUser` forces
  `runAsNonRoot: true`.
- `transport.type`: `streamableHttp | sse | stdio` (author `streamableHttp` for
  own-code HTTP servers). `path` defaults matter, set `/mcp`.
- `egressBindings[]`: `{egressClass: exact-host|public-web, dns, port, protocol}`.
  `exact-host` (default) opens one DNS host + port. `public-web` opens public TCP
  80/443 only and must NOT carry `dns`/`port`. Max 20.
- `volumeClaimTemplates[]`: `{name, storageClass, accessMode, size}`,
  `accessMode` is `ReadWriteOnce|ReadOnlyMany|ReadWriteMany`. Use `standard` for
  minikube, the cluster's real class for GKE (`standard-rwo`) or DO
  (`do-block-storage`). Max 4.
- `security.isolationLevel`: `minimal | standard | strict`, additive, default
  `minimal` (root allowed, writable root FS, all caps dropped, seccomp
  RuntimeDefault). `standard` adds `runAsNonRoot` + read-only root FS. `strict`
  adds pod identity pinned to 65534 + `automountServiceAccountToken: false` +
  the PodSecurity `restricted` label. Prefer `minimal` and use a per-workload
  `security.runAsUser` when a specific non-root uid is needed.
- `spec.ui`: `workloadRef` + `port` required; optional `title` (<=100 chars),
  `icon` (`data:` URI only, <=32 KB), `defaultPath` (starts with a single `/`),
  `egress.internal[{workloadRef, port}]`, `egress.external[{fqdn, port}]` (DNS
  only, no CIDR, no wildcard).
- `spec.bindings[]`: `{from, to, port, protocol}` (from/to/port required). Emits
  a symmetric NetworkPolicy (egress in the source namespace, ingress in the
  target namespace), so cross-namespace bindings work.
- `spec.contextRef`: OPTIONAL, even for a `transport` workload (the CRD calls it
  "required when a workload exposes an MCP transport", but that is only a
  description, not a validation rule). Omit it and WRC derives a private
  `wf-<recipeName>` Context for the recipe's own MCP servers: they are isolated to
  this recipe and the chat agent cannot see them until an operator adds them to
  the chat Host's Context (see the `create-evenfire-mcp-server` skill's attach
  procedure). Set it to a shared Context (for example `context1`) only to register
  the MCP server there directly, and only on a NON-agentic recipe: on an agentic
  recipe (any `steps`) setting `contextRef` is REJECTED unless BOTH
  `spec.security.allowContextRef: true` AND a namespace `WorkflowRecipePolicy`
  with `allowContextRef: true` exist. The only hard per-transport-workload
  requirement is `port` (and `stdio` transport is valid only on `deployment`
  workloads). This is why the common MCP-plus-notifications plugin (a transport
  workload that also declares `spec.pluginWorkloadSdk`) simply omits `contextRef`.
