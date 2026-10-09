# Workflows: steps, triggers, snippets, outputs

A recipe with at least one entry in `spec.steps` is a workflow. Verified
against evenfire-ai/evenfire `dev` at `0b26101eb`
(`workflow-recipes/src/workflow/`, `packages/workflow-runtime-core/`,
`mcp-host/src/workflow/internalTools.ts`).

## Before you add steps to a plugin

Adding `steps` changes how WRC treats the whole recipe:

- **No webhooks.** The webhook gateway is built only for recipes without steps.
- **Long-running workloads stop following the recipe.** Once a workflow recipe
  is `active` and waiting for runs, WRC skips the workload pass, so a new image
  or env value on a workload is not applied. The exception is a recipe that
  also declares `spec.pluginWorkloadSdk`: it re-applies its workloads on each
  reconcile (except while a run is in progress).
- **`includeWhen` is ignored**; every workload is deployed.
- **Snippet steps need the operator's runtime flag** (`WRC_ENABLE_SNIPPET_RUNTIME`
  on WRC). It is off in the base manifests; without it the recipe fails with
  `snippet workflow runtime is disabled`.

A UI plugin that only needs desktop notifications or LLM calls does not need
steps; see [plugin-workload-sdk.md](plugin-workload-sdk.md). A plugin that
needs periodic work but also webhooks can run its own `cronjob` workload.

## Kinds of workflow

| Kind | How WRC classifies it | Runs on |
|---|---|---|
| Agentic | any step with `instruction` | an LLM agent (mcp-host) with tools |
| Snippet | every step has `run` | the platform snippet runner (TypeScript, no LLM) |
| Custom | `spec.coordinatorImage` set | an operator-approved coordinator image (`WRC_ENABLE_CUSTOM_COORDINATOR_IMAGE`) |

## steps[] (max 100)

| Field | Values | Notes |
|---|---|---|
| `id` | `^[a-z][a-z0-9-]*$`, max 63, unique | |
| `instruction` | max 10000 characters | Agentic step; exactly one of `instruction`/`run` (R8, R9) |
| `run` | `{type: snippet, language: typescript, code (max 20000), capabilities}` | No `agent` on a run step (R10) |
| `dependsOn[]` | step ids | Order only; data flows through templates and `previousOutputs` |
| `mcpServers[]` | ids, max 20 | An MCP workload id of this recipe, or a `spec.mcpServers[]` id with `endpoint`; anything else fails the recipe |
| `allowedTools.include[]` | `<server>__<tool>`, max 50 by default (operator ceiling 100) | Only these tools are shown to the model |
| `toolChoice` | `auto`, `none`, `required` | |
| `maxIterations` | 1 to 100, default 50 | Tool-calling rounds |
| `timeoutSeconds` | default 300 | |
| `maxRetries` | 1 to 5, default 2 | Transient failures only |
| `backoffSeconds` | 1 to 3600, default 30 | |
| `agent` | `{provider, model, soul}` | Per-step override |
| `requiresApproval` | `{target: {userId} or {teamId}, message (max 2000), timeoutSeconds 30-604800, default 3600}` | The step waits for a human; no decision in time means rejected |

`spec.mcpServers[]` items are `{id, endpoint?}`; for an MCP workload in the
same recipe WRC computes the endpoint
(`http://<server>.mcp-server.svc.cluster.local:<port><path, default /mcp>`).

### Agent and providers

`spec.agent` (default) and `steps[].agent` (override) take `provider`, `model`
(`^[A-Za-z0-9._:/-]{1,128}$`), and on `spec.agent` a `secretRef {name,
namespace}` holding the provider key. Providers: `openai`, `claude`, `zai`,
`bailian`, `vertex`, `openrouter`, `gemini`, `deepseek`, `groq`, `together`,
`fireworks`, `mistral`, `xai`, `cerebras`, `deepinfra`, `perplexity`,
`moonshot`, `nebius`, `novita`, `minimax`, `codex-subscription`,
`grok-subscription`. `bedrock` and `azure` are rejected on purpose. The two
`*-subscription` providers use a subscription a platform admin connects; they
take no `secretRef`, and a recipe may use only one of them (D13).

### Templates inside an instruction

| Template | Becomes |
|---|---|
| `{{inputs.KEY}}` | the resolved input (no spaces inside the braces) |
| `{{<stepId>:output}}` | the output of a finished step, wrapped in `<step-output id="..." data-only="true">` and cut to 8192 characters by default (operator range 1024 to 65536) |
| `{{workflow:name}}` | the workflow name |
| `{{soul:content}}` | the SOUL text |

Unknown references are left as written. Snippet `code` gets no template
substitution at all; it reads `sdk.inputs` and `sdk.previousOutputs`.

## Snippet steps

The code is the body of `async (sdk) => { ... }`: use `await`, and `return`
the step output (redacted of secret values).

```yaml
steps:
  - id: summarize
    run:
      type: snippet
      language: typescript
      capabilities:
        http: { allowedHosts: [api.example.com] }        # exact-host is the default
        secrets:
          - { alias: apiKey, secretRef: { name: my-plugin-secrets, key: api-key } }
          - { alias: pgPassword, secretRef: { name: my-plugin-secrets, key: pg-password } }
        postgres: { workloads: [db], access: read }
      code: |
        const key = await sdk.secrets.get('apiKey')
        const data = await sdk.http.fetchJson('https://api.example.com/v1/items', {
          headers: { authorization: `Bearer ${key}` },
        })
        const rows = await sdk.postgres.query(
          { workload: 'db', database: 'app', user: 'app', passwordSecretAlias: 'pgPassword' },
          { sql: 'select count(*)::int as n from items' },
        )
        await sdk.artifacts.writeJson('summary.json', { remote: data, local: rows })
        return { items: rows[0]?.n ?? 0 }
runtimeEgress:
  http:
    allowedHosts: [api.example.com]                       # every snippet host, again here (R13)
```

`sdk` surface (declare the matching capability or the call is refused):

| Member | Notes |
|---|---|
| `sdk.inputs`, `sdk.previousOutputs` | read-only data |
| `sdk.http.fetchJson(url, init)`, `sdk.http.fetchText(url, init)` | `GET`, `HEAD`, `POST`, `PUT`; response capped at 1 MiB |
| `sdk.secrets.get(alias)` | aliases from `capabilities.secrets` (max 20) |
| `sdk.postgres.query/queryOne/execute(ref, query)` | `ref {workload, database, user (default postgres), passwordSecretAlias}`, `query {sql, values?, limit?}`; one statement; anything but `select`/`show`/`explain` needs `access: readWrite`; max 1000 rows |
| `sdk.mongo.find/aggregate/insertOne/insertMany/updateOne/updateMany/deleteOne/deleteMany` | `capabilities.mongo`; max 1000 rows |
| `sdk.mcp.callTool(serverId, toolName, args)` | `capabilities.mcp {servers, allowedTools.include}`, explicit names only (R11, R14) |
| `sdk.artifacts.writeJson(name, data)`, `writeMarkdown(name, body)` | names `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`, default max 20 per step |
| `sdk.log.info/warn/error(message, meta?)` | |
| `sdk.sleep(ms)` | at most 300000 ms and the step timeout |

The source may not use `process`, `require(`, `import(` or `import.meta`, Node
system modules, `eval`/`Function`, `global`/`globalThis`, or the words
`constructor`, `prototype`, `__proto__` (even in strings). Snippets cannot call
the Plugin Workload SDK.

`http.egressClass: public-web` (no `allowedHosts`) is allowed only when
`spec.runtimeEgress.http.egressClass` is also `public-web`.

## Agentic steps: built-in tools

Without any MCP server, an agentic step can use `clerum__generate_markdown`,
`clerum__generate_pdf`, `clerum__generate_docx`, `clerum__generate_xlsx`,
`clerum__generate_pptx`, `clerum__generate_chart`,
`clerum__generate_dashboard`, `clerum__list_workflows`,
`clerum__read_workflow` and `clerum__trigger_workflow`. In a workflow run,
generated files are written under `/output`. Restrict what the model sees
with `allowedTools.include`.

## When a run starts

This section is about recipes with `steps`. A recipe without them settles on
`active` with the message `All workloads deployed` (with the SDK and no grant
yet, `Plugin Workload SDK operator policy pending (<reason>)`).

- If any step needs an agent host (an `instruction`, a step `agent`,
  `requiresApproval`, or `mcpServers` on an agentic step), or the workflow
  also declares `spec.pluginWorkloadSdk` on a cluster with the SDK enabled,
  installing only registers the workflow: the recipe goes `active` with
  "Workflow trigger infrastructure registered", and each run starts from a
  trigger as its own run-scoped child recipe.
- A workflow made only of snippet steps (and no SDK) runs once as soon as it is
  installed; the recipe's phase then follows that run (`active` when it
  completes, `failed` when it fails).

## Triggers and schedules

- `spec.triggers.onDemand: {}` enables manual runs (Desktop app or agent).
  `requiresApproval` defaults to `true` (every manual run goes through the
  approval gateway first). `allowedActors` is any of `user`, `autonomous`,
  `scheduled` (default `[user]`).
- `spec.triggers.schedule: {cron, timezone (default UTC), concurrencyPolicy
  Forbid|Replace|Allow (default Forbid), suspend}` and the older
  `spec.scheduling` both register a schedule with Control API's schedule worker
  (not a Kubernetes CronJob, whatever the CRD text says).
- **A recipe with `steps` must declare `triggers.onDemand` or
  `triggers.schedule`.** The platform's admission policy and Control API reject
  it otherwise.
- **A scheduled recipe needs the label `clerum.io/workflow-team-id: <team
  UUID>`** (the team its runs are attributed to); without it the recipe fails
  with `scheduled WorkflowRecipe requires clerum.io/workflow-team-id`. A
  registry install keeps none of your labels, so a scheduled recipe installed
  from the Marketplace fails this way; install it with the admin API or
  `kubectl` and the label.

Starting a run:

- Desktop: users with access can run an on-demand workflow. With
  `requiresApproval` (the default) the click creates an approval request, and
  the run starts once it is approved. A user with a direct grant approves their
  own request; with access through a team, that team must also be listed on
  the plugin's Approval targets tab, or the click fails with
  `approval_target_not_allowed`.
- Control UI: an admin's Run skips the approval.
- Chat agents (`clerum__trigger_workflow`): only when `allowedActors` includes
  `autonomous`, and after an approval.
- API callers send an `Idempotency-Key` header; recipes with MCP workloads must
  be `active` (or `failed`) to accept runs.
- Run outputs: the Control UI's run page lists artifacts (`GET
  /api/v1/admin/workflows/<ns>/<name>/runs/<runId>/artifacts/<file>/download`);
  `status.steps[].output` keeps only a preview (32 KiB by default).

## Outputs and retention

- `spec.output {destination: configmap|secret|stdout|pvc, format:
  pdf|xlsx|json|text|html|multi, claimName (pvc only, an existing PVC),
  storageSize (default 256Mi)}`. Files live under `/output`; `status.artifacts[]`
  lists them.
- `spec.runRetention {successfulHistoryLimit 0-50 (default 5),
  failedHistoryLimit 0-100 (default 20), ttlSecondsAfterFinished (default and
  max 2592000), maxRunDurationSeconds}`. **If you declare `runRetention`, set
  `maxRunDurationSeconds` to 86400 or less**: the CRD fills in 604800, but the
  controller's ceiling is 86400 (24 h), and the recipe fails with
  `spec.runRetention.maxRunDurationSeconds must be at most 86400`.
- `status.steps[]` holds a bounded preview of each step's output
  (`outputTruncated`, `outputLength`); complete files belong in artifacts.
