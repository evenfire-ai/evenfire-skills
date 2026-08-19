# The `/v1` hook contract (both lanes)

Reference for [../SKILL.md](../SKILL.md) §3. mcp-host POSTs `application/json` to
`{endpoint}{path}/v1/{point}`. The endpoint path names differ from the CRD enum —
using the enum value in the URL is a silent 404.

> **Read the code, not this file, when in doubt.** On the guardrails build the
> canonical shapes are *generated* from the running pod (a `gen-contract` script
> reads `hookProjection`, `remoteLlmHook`, `remoteToolHook`, `hookFetcher`), because
> a hand-written contract drifted into documenting a `config` key, a `state` key,
> and an `action:'reshape'` that no code path constructs. Regenerate against the pod
> whenever this disagrees with behavior.

## Endpoint ↔ CRD enum

| URL | CRD `lifecyclePoints` value | Lane | Declarable? |
|---|---|---|---|
| `/v1/pre_call` | `preCall` | LLM | yes |
| `/v1/moderate` | `moderate` | LLM | yes |
| `/v1/post_call` | `postCallSuccess` | LLM | yes |
| `/v1/on_error` | `onError` | LLM | yes |
| `/v1/pre_tool_use` | `preToolUse` | tool | **cluster-dependent** |
| `/v1/post_tool_use` | `postToolUse` | tool | **cluster-dependent** |

The engine routes all six, but a cluster's `LlmHook` CRD enum may admit only the
four LLM-lane values. If so, a CR declaring a tool-lane point is rejected at
admission and the point is never dialed. Confirm before designing a tool-lane hook.

## LLM-lane request bodies

**`pre_call` / `moderate`** — a subtraction from the completion request (system
messages stripped; `messages` absent entirely for a `contentAccess: metadata`
`pre_call` hook):

```ts
{ messages: ChatMessage[],   // non-system only
  tools: ToolDefinition[],
  max_tokens?: number, temperature?: number,
  tool_choice?: 'auto' | 'none' | 'required' }
```

**`post_call`** — constructed, not projected:

```ts
{ response: { content, tool_calls, finish_reason }, usage }
```

**`on_error`** — the request is **nested**; messages are at `body.request.messages`,
and `error.message` is redacted and truncated to 120 chars:

```ts
{ request: { messages, tools, … }, error: { code?, message?, retryable? } }
```

**Never delivered, to any point: `config` and `state`.** `defaultConfig` from your
entry is stored as `spec.config` and stops there. There is no `model` on a request
body; `usage` appears on `post_call` only.

## LLM-lane responses (gated by the named capability)

| Point | Success response | Gated by |
|---|---|---|
| `pre_call` | `{action:'continue'}` optionally `+ patch:{messages?, params?}` · or `{action:'reject', code, message}` | `may_rewrite` · `may_deny` |
| `moderate` | any `2xx` passes · `4xx {code,message}` denies | `may_deny` |
| `post_call` | `{ response: { content?, tool_calls? } }` | `may_substitute_result` |
| `on_error` | `{action:'recover', response:{content}}` · anything else = no recovery | `may_substitute_result` |

- Only `temperature`, `max_tokens`, `tool_choice` are read from `patch.params`;
  everything else is dropped. These fields arrive **top-level** on the request and
  go back **nested under `params`**.
- `{}` is **not** a valid `pre_call` action — return `{action:'continue'}` to mean
  "no change". Returning `{}` there falls to the fail-mode.
- There is no `reshape` action. Only `recover` is honored on `on_error`, and
  recovery is text-only (any `tool_calls` are dropped — rule 3, subtractive).

## A real `pre_call` body

```json
{
  "messages": [
    { "role": "user", "content": "the plain string form" },
    { "role": "assistant", "content": [
      { "type": "text", "text": "the block form" },
      { "type": "tool_use", "id": "toolu_01", "name": "search", "input": { "q": "…" } }
    ] },
    { "role": "user", "content": [
      { "type": "tool_result", "tool_use_id": "toolu_01", "content": "…" }
    ] }
  ],
  "tools": [ { "name": "search", "description": "…", "input_schema": {} } ],
  "max_tokens": 4096, "temperature": 0.2
}
```

- `content` is `string | Block[]` and **both shapes arrive**. Handle both.
- Only `type: "text"` blocks are yours to rewrite; images, `tool_use`, and
  `tool_result` envelopes pass through untouched and are enforced on your return.
- You get the whole projected conversation on **every** call, not the new turn.
  Cost your work per *call*, and make the transform idempotent.

## Tool lane (different verbs, different payloads)

```ts
// POST /v1/pre_tool_use
{ tool: <identity>, arguments: <call arguments> }
// POST /v1/post_tool_use
{ tool: { provenance, server, name }, arguments: <…>, result: { content, is_error } }
```

| Point | Success response | Gated by |
|---|---|---|
| `pre_tool_use` | `{decision:'deny', reasonCode}` · `{updatedInput:{…}}` · anything else = allow | `may_deny` · `may_rewrite` |
| `post_tool_use` | `{updatedResult:{content}}` · nothing = unchanged | `may_rewrite` |

- The verb is `decision`, not `action`; the payload is `updatedInput` /
  `updatedResult`, not `patch`.
- `updatedInput` **replaces** the arguments wholesale — there is no merge.
- `may_substitute_result` is LLM-lane only. A `post_tool_use` redactor works under
  `may_rewrite`; `is_error` is preserved through redaction, so it can never flip a
  failure into a success.
- **Fail-closed here withholds rather than passes through**: a `failMode: closed`
  `post_tool_use` that is unavailable replaces the result content with
  "This tool result was withheld by a content guardrail policy." An observer must
  never be able to do that — use `failMode: open` and no capabilities.
