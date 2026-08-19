# Hardened hook pod manifest (N8)

Reference for [../SKILL.md](../SKILL.md) §5. This is the manifest to **copy** for a
`service`-target hook (the local fast-iteration lane, and any hook the cluster
cannot pull as an image). A `service` target exists behind an `LlmHook` with
`target.service`; HCC does **not** synthesize its pod, so its `securityContext` is
entirely yours.

## Why this file exists

The guardrails spec invariant **N8** requires every hook pod to run non-root, with
a read-only root filesystem, all Linux capabilities dropped, privilege escalation
forbidden, the `RuntimeDefault` seccomp profile, **and no ServiceAccount token
mounted**. HCC stamps all of this automatically **only for an `image` target**.

A hand-written `service`-target manifest that sets only `runAsNonRoot: true` and
`capabilities.drop: [ALL]` — a common shortcut, and what some reference examples
ship — still **mounts the default ServiceAccount token** (because
`automountServiceAccountToken` defaults to `true`), leaves the **root filesystem
writable** (no `readOnlyRootFilesystem`), and omits the **seccomp** profile. That
is three controls short of the pods beside it. It is usually not exploitable on its
own (the default SA can do nothing and the API server is unreachable from a hook
pod under N3/N5 default-deny egress), but it is the example people copy, so the
gap propagates. Copy the manifest below, which sets all of N8 explicitly.

## Deployment + Service

```yaml
# service-target hook. The LlmHook points at this via `target.service`
# (name: <hook>, namespace: llm-hooks, port: 8080).
#
# securityContext is deliberately at the platform's own bar (spec §N8). This
# process may read message bodies, so the question is not "what does it need" but
# "what can it do if it is wrong": no filesystem writes, no capabilities, no
# service-account token, non-root, seccomp confined.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: <hook>
  namespace: llm-hooks
  labels:
    app: <hook>
spec:
  replicas: 1
  selector:
    matchLabels:
      app: <hook>
  template:
    metadata:
      labels:
        app: <hook>
    spec:
      # N8: nothing here talks to the API server — do not mount its token.
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: hook
          image: <hook>:<tag>            # digest-pin wherever the cluster can pull
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 8080
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          readinessProbe:
            httpGet: { path: /healthz, port: http }
            initialDelaySeconds: 1
            periodSeconds: 10
          livenessProbe:
            httpGet: { path: /healthz, port: http }
            initialDelaySeconds: 5
            periodSeconds: 20
          resources:
            requests: { cpu: 25m, memory: 32Mi }
            limits: { cpu: 250m, memory: 256Mi }
---
apiVersion: v1
kind: Service
metadata:
  name: <hook>
  namespace: llm-hooks
  labels:
    app: <hook>
spec:
  selector:
    app: <hook>
  ports:
    - name: http
      port: 8080
      targetPort: http
```

## Notes

- **`readOnlyRootFilesystem: true`** means the process may not write its own
  filesystem. A dependency-free `node:http` server needs nothing writable. If a
  library insists on a temp dir, add a narrow `emptyDir` `volumeMount` (e.g.
  `/tmp`) rather than dropping this control.
- **`runAsUser: 1000`** matches the platform default and a `USER node` image (uid
  1000). Setting `runAsUser` also forces `runAsNonRoot: true`.
- **The `service` target costs digest verification** — HCC populates
  `status.observedDigest` only for `image` targets, so nothing binds the running
  code here. Prefer an `image` target (which HCC hardens for you and digest-pins)
  wherever the cluster can complete the pull; use `service` only for iteration.
- The per-hook **NetworkPolicy** is HCC-generated and admits only the referencing
  mcp-host pods over an `llm-hooks` default-deny baseline — do **not** hand-write
  network policy for the hook; declaring `egressBindings` on the `LlmHook` (image
  target) is the only supported way for a hook to reach out, and it raises the
  required trust tier.
