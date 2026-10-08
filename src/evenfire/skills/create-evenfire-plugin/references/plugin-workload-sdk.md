# Plugin Workload SDK: desktop notifications and LLM calls

The Plugin Workload SDK lets a backend workload of your plugin do two things
without holding any platform or provider credential:

- **clientNotifications**: notify named Evenfire users. They get an entry in
  the Desktop app's notification bell and a native OS notification.
- **promptBridge**: make a one-shot LLM call through a provider, model and
  credential an operator chose for your recipe.

Verified against evenfire-ai/evenfire `dev` at `0b26101eb`
(`workflow-recipes/src/{reconciler,workflow}/pluginWorkloadSdk*`,
`mcp-host/src/pluginWorkloadSdk/`, `control-api/src/routes/admin/pluginWorkloadSdk.ts`,
`control-api/src/services/pluginWorkloadSdk*`).

## Four things must all be true

1. The recipe declares `spec.pluginWorkloadSdk` (and `spec.agent` for
   promptBridge).
2. The cluster operator has enabled the SDK on the recipe controller
   (`PLUGIN_WORKLOAD_SDK_ENABLED=true` on `workflow-recipes`). On a cluster
   without it, a recipe without `steps` that declares the block deploys
   nothing at all (status message `Plugin Workload SDK disabled after confirmed
   teardown`), and `status.pluginWorkloadSdk.state` reads `disabled`.
3. An operator has saved a grant for each family in Control API (Control UI:
   Plugins, the `⋯` menu, Plugin SDK; or the admin API). Without a grant every
   call is refused, whatever the recipe says.
4. Your workload calls the injected endpoint with its injected token.

## Declaring it

No `steps`, no keepalive step and no `triggers` are needed: a stepless recipe
with the block gets an always-on SDK host. On a stepless recipe the block is
not allowed together with `triggers`, `scheduling` or `coordinatorImage`, and
the recipe needs at least one workload.

```yaml
spec:
  agent:                       # promptBridge only: the host's bootstrap provider/model.
    provider: claude           # Omit spec.agent on a notifications-only recipe without
    model: claude-sonnet-4-6   # steps: admission rejects it there (R1).
  pluginWorkloadSdk:
    clientNotifications:
      allowedEventTypes: [my-plugin.assigned, my-plugin.mentioned]   # no wildcards (PS2)
      allowedUserRefs: true
    promptBridge:
      allowedModels: [claude-sonnet-4-6]                               # no wildcards (PS3)
    allowedCallers: [api]      # workload ids; must exist (PS4)
  workloads:
    - id: api
      ...
```

- `promptBridge` requires a resolvable agent: `spec.agent`, or on a workflow a
  step agent with `provider` and `model`. Otherwise the recipe fails with
  `pluginWorkloadSdk.promptBridge requires spec.agent or a step agent with
  provider + model`.
- What the grant says is what runs. In the recipe, `allowedEventTypes`,
  `allowedModels` and `allowedCallers` are validated and used to prefill the
  grant form; at call time only the grant is checked. Several other CRD knobs
  are not enforced at this revision: `maxRequestsPerRun`,
  `maxNotificationsPerRun` (deprecated), `maxInvocationsPerMinute`,
  `maxNotificationsPerMinute`, `maxConcurrentInvocations`,
  `maxRequestContentBytes`, `maxTitleBytes`, `maxBodyBytes`,
  `resultRetentionHours`, `maxAttachments`, `maxAttachmentBytes` and
  `idempotencyKeyPattern` (the platform uses its own fixed limits, below).
- On a recipe with `steps`, PS4 and the pattern check are not run, and callers
  that do not exist are silently dropped.

## What your workload receives

For each eligible workload WRC sets:

| Variable | Value |
|---|---|
| `PLUGIN_WORKLOAD_SDK_ENDPOINT` | `http://wf-<recipe>-mcp-host.sandbox-recipes.svc.cluster.local:8099/sdk` |
| `PLUGIN_WORKLOAD_SDK_TOKEN` | from Secret `wf-<recipe>-plugin-workload-sdk-token`, key `caller-<workloadId>` |

- Eligible: non-MCP workloads other than the UI workload, filtered by
  `allowedCallers` when it is non-empty. The UI workload and MCP workloads
  never get a token, so call the SDK from your backend.
- The NetworkPolicies to reach the SDK host on port 8099 are created for you.
- The SDK host runs as the pod `<recipe>-mcp-host` in `sandbox-recipes` and
  reads caller tokens when it starts. If a caller added after install gets
  `401 unauthorized`, delete that pod; WRC recreates it.
- Read both variables at startup and treat a missing endpoint as "SDK not
  available" (local development, or a cluster without the SDK) instead of
  crashing.

## HTTP API

All calls: `Authorization: Bearer $PLUGIN_WORKLOAD_SDK_TOKEN`, JSON body (max
1 MB). Routes exist only for the families the recipe declared.

### Send a notification

```
POST $PLUGIN_WORKLOAD_SDK_ENDPOINT/v1/client-notifications
{
  "idempotencyKey": "assigned-7f3c9a",          // ^[a-zA-Z0-9_-]{1,128}$, required
  "eventType": "my-plugin.assigned",            // must be in the grant, exact match
  "userRef": "<platform user UUID>",            // or "target": {"targetRef": "..."}; exactly one
  "notification": {
    "title": "Assigned: Fix login bug",         // required, max 256 bytes
    "body": "Ana assigned you TASK-42",         // required (may be ""), max 4096 bytes
    "data": {},                                 // optional object
    "actionRef": {"type": "task", "id": "TASK-42"}   // optional
  }
}
-> 200 {"notificationId", "status": "accepted"|"delivered", "target", "eventType", "title", "body", "createdAt", ...}
```

- `userRef` is the platform user's UUID: the value the platform puts in
  `X-Clerum-User` (see [ui-embed.md](ui-embed.md)). Store it when a user first
  uses your UI. Emails and phone-like values are rejected.
- `GET $PLUGIN_WORKLOAD_SDK_ENDPOINT/v1/client-notifications/recipients`
  returns `{recipients: [{userRef, displayName}]}` (display name = email) for
  the users the grant allows.
- A repeat with the same key and payload returns the same `notificationId`;
  the same key with a different payload is `422 idempotency_conflict`. Keys are
  unique per recipe and family (not per workload) for 24 hours, so derive them
  from your own event id.

What the user sees: a bell entry labelled "Plugin notification" (text = body,
else title) and a native notification (subject to their notification
settings). Clicking it opens the Desktop's Plugins section with your recipe
selected; `actionRef` and `data` do not navigate anywhere at this revision. A
closed Desktop receives the notification when it reconnects, for up to 72
hours. If the Desktop has not picked it up after a grace period (90 s by
default), the platform can deliver it through the user's connected Telegram,
Slack or Teams account.

**Access and recipients are separate lists.** Giving a team access to the
plugin does not let its members receive notifications, and removing access
does not remove them from the grant. Keep the grant's user list in step with
who uses the plugin.

### Call an LLM

```
POST $PLUGIN_WORKLOAD_SDK_ENDPOINT/v1/prompt-bridge
{
  "idempotencyKey": "classify-9f2e",            // required, same pattern as above
  "purpose": "classification",                  // required: summarization, classification, extraction,
                                                //   generation, translation, question_answering, analysis
  "messages": [                                 // required, roles system|user|assistant; content <= 128 KiB total
    {"role": "system", "content": "Answer yes or no."},
    {"role": "user", "content": "Is this a duplicate of TASK-12? ..."}
  ],
  "maxTokens": 256,                             // optional, clamped to the grant's maxOutputTokens
  "temperature": 0,                             // optional
  "targetRef": "...",                           // optional selectors: targetRef, provider+model, model,
  "metadata": {}                                //   modelPolicyRef; none = the grant's default target
}
-> 200 {"invocationId", "model", "servedTarget": {"targetRef", "provider", "model", ...},
        "fallbackUsed", "usage": {"inputTokens", "outputTokens"}, "content",
        "finishReason": "complete"|"length"|"content_filter", "correlationId", "createdAt", "policyRevision"}
```

- Synchronous: the call blocks until the model answers (up to 120 s by
  default). No streaming, no tools, no attachments, no conversation state.
- The operator's grant lists ordered targets (provider, model, credential);
  the first is the default and must equal `spec.agent`'s provider and model.
  On a provider outage or rate limit the platform may fall back to the next
  targets in that order.
- Idempotency: a key that already succeeded is answered `422
  idempotency_conflict` and the text is **not** returned again (the platform
  does not store completions). Persist the result yourself; use a new key for a
  new request.
- With `codex-subscription` the grant needs a connected subscription and
  `maxOutputTokens` cannot be enforced (the contract cap is 16384).

### Errors

Body: `{"error": code, "message", "retryable", "reason"?}`.

| HTTP | `error` |
|---|---|
| 400 | `invalid_request` (also what an unexpected 4xx from the platform becomes) |
| 401 | `unauthorized` (missing or unknown token) |
| 403 | `capability_not_declared`, `caller_not_allowed`, `scope_denied`, `target_not_allowed`, `event_type_not_allowed`, `provider_policy_denied` |
| 409 | `ambiguous_model` (a bare `model` offered by more than one provider) |
| 413 | `payload_too_large` |
| 422 | `idempotency_conflict` |
| 429 | `quota_exceeded` |
| 503 | `provider_unavailable`, `protocol_mismatch`, `sdk_unavailable` (the SDK host's binding changed; retry) |

`reason` (when present): `rate_limited`, `insufficient_quota`, `auth`,
`provider_unavailable`, `credential_unavailable`, `configuration`, `timeout`,
`network`, `outcome_unknown`. With `outcome_unknown` the provider may have run:
investigate before retrying, and retry with a new key. A promptBridge call
with no ready grant fails with `403 provider_policy_denied`.

### Limits

- Per caller workload, at the SDK host: 100 requests per minute and 10
  concurrent requests (`429 quota_exceeded` / `503 provider_unavailable`).
- Per recipe and family, in Control API: 600 promptBridge calls and 750
  notifications (per event type) per minute by default; a grant's
  `quotaLimits` can change them.

## The grant (operator side)

Plugin authors cannot create grants; document what the operator must enter in
`spec.description`. Admin API: `POST /api/v1/admin/plugin-workload-sdk/grants`
(upsert per recipe and family), `GET` and `DELETE .../grants/:id`.

- Both families: `recipeNamespace` (`sandbox-recipes`), `recipeName` (the
  in-cluster name), `capabilityFamily`, and a non-empty `allowedCallers`.
- clientNotifications: non-empty `allowedEventTypes` (exact strings), and
  `allowedUserRefs` (user UUIDs) and/or `allowedTargetRefs`, with at least one
  user that exists.
- promptBridge: `provider`, ordered `promptTargets[] {targetRef, provider,
  model, credentialSlot, connectionRef?}`, `defaultTargetRef` equal to the
  first target, which must match `spec.agent`.
- Wildcards are rejected everywhere.
- Renaming an event type in code without updating the grant stops delivery
  for that type with `403 event_type_not_allowed`.
- Grants of a recipe that was uninstalled, or whose SDK block was removed,
  stay `disabled` and block saving a new grant for the same in-cluster name;
  delete them on the Plugin SDK page first.

## Status

`status.pluginWorkloadSdk.state`:

| State | Meaning | Fix |
|---|---|---|
| `validated` | Host ready and every declared family has a ready grant | |
| `awaiting_policy` | A grant is missing or does not match (`grant_missing`, `bootstrap_target_mismatch`, `policy_disabled`, ...) | Create or fix the grant |
| `degraded` | The provider is unavailable, or the host is still proving readiness | Usually transient; check the provider |
| `disabled` | The SDK is not enabled on this cluster | Operator |

The recipe phase stays `active` while the state is `awaiting_policy`; only the
message (`Plugin Workload SDK operator policy pending (<reason>)`) and this
field show it. A grant change is picked up within seconds.
