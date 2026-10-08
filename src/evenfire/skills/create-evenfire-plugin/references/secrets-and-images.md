# Secrets and images

Verified against evenfire-ai/evenfire `dev` at `0b26101eb`
(`packages/workflow-runtime-core/src/secret-ownership.ts`,
`workflow-recipes/src/reconciler/{resourceBuilder,secretOwnership}.ts`,
`control-api/src/routes/admin/{recipes,secrets}.ts`).

## How a workload gets a secret value

`envSecret` is the only path from a Secret to a container:

```yaml
envSecret:
  name: my-plugin-secrets          # a literal Secret name in THIS workload's namespace
  keys:
    - { secretKey: pg-password, envVar: PG_PASSWORD }
    - { secretKey: smtp-pass,   envVar: SMTP_PASSWORD, optional: true }
```

- The Secret is read from the namespace the workload runs in
  (`sandbox-recipes`, `sandbox-ui` or `mcp-server`). Nothing copies Secrets
  between namespaces: if `api` and `mcp` both need a value, the operator
  creates the Secret in both namespaces with the same value.
- A required key that is missing leaves the pod in
  `CreateContainerConfigError` until the key appears.
- `optional: true` omits the variable when the key is absent at reconcile time.
  WRC watches referenced Secrets: when a key is added or removed, or an
  ownership label changes, it reconciles the recipe (after a short debounce)
  and the variable appears on the next rollout.
- Changing only a value restarts nothing. Environment variables are read at
  container start, so delete the pods (or restart the rollout) after rotating.
- There is no `valueFrom`, no `envFrom`, and no Secret volume mount.

### Ownership labels (mandatory)

WRC projects a Secret into a recipe only when it carries exactly one of:

- `clerum.io/owner-recipe=<in-cluster recipe name>`: only that recipe may use it.
- `clerum.io/shared=true`: any recipe may use it.

No label, or both labels, means denied: the workload that references it is not
rendered (an existing one is torn down), its status reads
`EnvSecretOwnershipDenied`, and the recipe is `degraded`. The same ownership
rule applies to `imagePullSecrets` (the workload is denied), webhook
`secretRef`s (the webhook is disabled) and snippet secrets. Because the owner
label must name the installed recipe, install first, then create and label the
Secrets; WRC picks them up without a reinstall.

The Control UI writes the labels for you: open the plugin, Secrets tab. It
lists every Secret the recipe references, in which namespace, and marks the
missing ones; Add opens the form pre-filled with the namespace and the owning
recipe (or choose Shared). The owner recipe must already exist. The Secret is
created in `sandbox-recipes`, `mcp-server` or `sandbox-ui` with
`clerum.io/recipe-secret=true`, which is how the Control UI finds recipe
Secrets. From a shell, add all the labels yourself:

```bash
R=<in-cluster recipe name>          # see operate.md for how to find it
kubectl -n sandbox-recipes create secret generic my-plugin-secrets \
  --from-literal=pg-password="$(openssl rand -hex 24)"
kubectl -n sandbox-recipes label secret my-plugin-secrets --overwrite \
  clerum.io/owner-recipe="$R" clerum.io/recipe-secret=true
```

### Names you cannot use

Control API rejects recipe references to Secrets whose name starts with `wf-`
(the platform's per-recipe runtime Secrets) or equals `evenfire-registry-pull`
(rules `workflowWorkloadSecretRefReserved`, `workflowSnippetSecretRefReserved`,
`workflowOauthClientSecretRefReserved`).

### Credentials never go in `env[]`

Control API's recipe create, edit and validate routes (what the Control UI
form uses) refuse a recipe whose `env[]` looks like it carries a secret (rule
`workflowInlineSecretEnv`, "move this value to envSecret"). A registry install
and `kubectl apply` do not run this check, so keep secrets out of `env[]`
regardless. It flags:

- a name containing `PASSWORD`, `TOKEN`, `SECRET`, `API_KEY`, `CREDENTIAL` or
  `PRIVATE_KEY` (case-insensitive) with a non-empty value, unless the value
  references a sensitive-looking input such as `{{inputs.api_key}}`;
- a value that looks like a JWT, an `sk-...` or `pat...` key, a PEM block
  (`-----BEGIN `), or a URL with an inline password.

This also catches harmless names such as `MAX_TOKENS: '512'`: rename them
(`MAX_OUTPUT_LENGTH`). `envSecret` `envVar` names are not checked.

### The UI workload and Secrets

The UI workload can read a Secret that exists in `sandbox-ui` with an
ownership label, but keep it credential-free when you can: it is the pod that
serves the browser. Put provider keys and database passwords in a backend in
`sandbox-recipes`, and if the UI must prove to the backend that a request came
through it, give the UI only a narrow shared token for that purpose.

### Document every Secret in `spec.description`

Operators install from the catalog and read `spec.description`. For each
Secret list its name, namespace(s), keys, which are optional, what breaks
without each, and where each value comes from.

## Images

- **Tags are immutable.** WRC renders every container with
  `imagePullPolicy: IfNotPresent` (the field in the recipe is not honored), so
  a node that already has `:1.0.0` never pulls a re-pushed `:1.0.0`. Publish a
  new tag for every build.
- **Run as a numeric non-root user** (`USER 101`, `USER 1000`). The plugin
  namespaces enforce PodSecurity `baseline` and warn on `restricted`.
  `isolationLevel: standard` sets `runAsNonRoot`, which the kubelet can only
  check against a numeric `USER` (or a `security.runAsUser` in the recipe).
- **Listen on `0.0.0.0`**, not `127.0.0.1`, or the Service cannot reach you.
- **Architecture:** the image must run on the cluster's nodes. Build
  `linux/amd64` for typical cloud nodes and add `linux/arm64` if you or your
  users run Apple silicon minikube clusters.
- **Where to host:** any registry the cluster can pull from. Images on the
  platform registry (`registry.evenfire.ai/<org>/<name>:<tag>`) need no pull
  Secret: WRC attaches the platform pull credential `evenfire-registry-pull`
  automatically to any workload whose image host is the configured registry.
  Never list `evenfire-registry-pull` yourself (install is rejected). For any
  other private registry, list your own Secret in `imagePullSecrets`. Unlike
  other Secrets it must exist **before** install, in the workload's namespace,
  with an ownership label; for a registry install use `clerum.io/shared=true`
  (or the predictable generated name as owner). Create it with kubectl, since
  the Control UI writes only Opaque Secrets:

  ```bash
  # config.json: a Docker config holding only this registry's credential
  kubectl -n sandbox-recipes create secret generic my-pull \
    --type=kubernetes.io/dockerconfigjson --from-file=.dockerconfigjson=./config.json
  kubectl -n sandbox-recipes label secret my-pull clerum.io/shared=true clerum.io/recipe-secret=true
  ```

  The `publish-evenfire-plugin` skill covers pushing to the platform registry.
