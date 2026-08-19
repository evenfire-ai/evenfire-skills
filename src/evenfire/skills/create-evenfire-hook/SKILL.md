---
name: create-evenfire-hook
description: >-
  Author, test, publish, and install an Evenfire LlmHook — a sandboxed process
  mcp-host calls on the LLM request path of a Host to shape input before it is
  paid for (preCall), refuse a call (moderate), rewrite the response
  (postCallSuccess), recover from a provider error (onError), or guard/redact a
  tool call (preToolUse/postToolUse). Covers the four axes (lifecycle point,
  content access, egress, capabilities) and the trust tier they force, the /v1
  contract for both lanes, the hardened pod manifest, building an OCI image,
  publishing the catalog entry, installing onto a Host, and verifying by A/B. Use
  when creating, changing, testing, or onboarding a hook / guardrail / token
  optimizer / usage recorder.
---

# Authoring an Evenfire hook

> **Verified against the mcp-host guardrails build** (the `LlmHook` CRD
> `clerum.io/v1alpha1`, HCC `llmHookReconciler`, control-api install-hook saga,
> and the `/v1` contract generated from the running `mcp-host` image), and
> **validated live**: four hooks — one on each LLM-lane point (`preCall`,
> `moderate`, `postCallSuccess`, `onError`) — were built, hardened, installed, and
> A/B'd against a real agent on a running cluster, and every practice below is what
> survived that. Where this document and the running build disagree, the build
> wins — regenerate the contract cheatsheet against the pod rather than trusting
> prose.

A hook is your own process running **between the caller and the provider**, on
every call through a Host — including calls that reach a tool you did not write
from an application you do not own. It is **enforcement, not instruction**: a
system prompt can *ask* a model not to leak a credential or ramble past a budget;
a hook decides, and the decision is applied mechanically.

The reason you can put untrusted code on the production request path is that **the
platform does not trust it**: default-deny networking, a trust tier derived from
what you *declare* (not what you claim), capabilities enforced on your *response*,
an immutable system prompt, and tool calls that can only be *subtracted*. Being
untrusted is the feature.

A hook is *not* an MCP server (which adds a tool) or a plugin (which extends the
platform). It acts *on the request*. To build those instead, use
`create-evenfire-mcp-server` or `create-evenfire-plugin`. To publish what you
build here, the mechanics are in this skill (§5–§7); `publish-evenfire-plugin`
covers the shared registry-auth details.

## 1. Decide the four axes first

A hook is defined by four independent choices, not by a name. Make them before
writing code — they decide how it is delivered, what data it sees, and what trust
tier it needs.

| Axis | Values | Decides |
|---|---|---|
| **Lifecycle point** | LLM lane: `preCall`, `moderate`, `postCallSuccess`, `onError` · tool lane: `preToolUse`, `postToolUse` | Which `/v1` endpoints are dialed. One hook may serve several. **The two lanes have different request/response shapes** (§2). |
| **Content access** | `content` · `metadata` | Whether message bodies are projected in. Only a `preCall`-only hook may be `metadata`. |
| **Egress** | `egressBindings[]` or none | Whether the pod may reach the network at all. Default-deny otherwise — **not even DNS**. |
| **Capabilities** | `may_deny`, `may_rewrite`, `may_substitute_result`, `may_add_context` | Which response actions are honored. Admin-granted, enforced on the response. |

Three archetypes to copy:

- **Input optimizer** (e.g. a token compressor) — `preCall`, reads bodies
  (`content`), declares **no egress**. Everything in-process, so it installs at
  low/mid trust.
- **Observer/recorder** — `postCallSuccess` (and `postToolUse` where the tool
  lane is enabled), reads bodies, **no egress, no capabilities**. It cannot alter
  anything and installs on a Host never configured for guardrails.
- **Policy / redactor** — `moderate` to deny, or `postCallSuccess`/`postToolUse`
  to redact. Deny-authoritative ⇒ must fail closed (§7). `moderate` is the right
  shape for rejecting prompt-injection or a policy violation, because a `preCall`
  rewrite **cannot** touch `tool_result` content (only `type:'text'` blocks are
  yours, §3) — exactly where injected instructions usually arrive; a `moderate`
  hook *reads* everything and denies. But a deny blocks the whole call, so its
  false positives are expensive: match only high-signal, unambiguous phrasing. (A
  rule meant for "reveal your secrets" also fired on the benign "list the
  environment variables this image expects" until it was tightened to require the
  possessive "your" — tune until only the malicious form matches.)

## 2. Your trust posture is a consequence, not a wish

The axes force a required trust tier. The rule that matters: **content + egress
requires `high`**, because a hook that reads bodies and can reach the network is
an exfiltration path.

| Reads bodies | Has egress | Trust required |
|---|---|---|
| no | no | `low` |
| no | yes | `low` — no content to leak |
| yes | no | `low` / `mid` — inspects/transforms locally |
| yes | yes | **`high`** — vetted only |

Which tier you can actually reach depends on **who published the entry, not what
it declares**. `resolveHookTrustLevel` honors the entry's own `trust_level` only
for a *curated* org (the cluster's own org, official `@clerum`/`@evenfire`, or an
operator allowlist) and caps everyone else at `defaultHookTrustCap` (`mid` by
default). **`high` is not reachable for a self-published org.**

> **Design consequence.** If your hook reads message bodies and you are not a
> curated org, declaring **no egress is load-bearing**, not tidiness. Add one
> `egressBindings` entry and the install stops dead at
> `403 content_egress_requires_high_trust`, with nothing the operator can do short
> of curating your org. Do the work in-process, or split into two hooks.

## 3. The `/v1` contract (both lanes)

mcp-host POSTs `application/json` to `{endpoint}{path}/v1/{point}`. `{path}` is
`spec.path` (default `/`), so one pod can host many hook functions routed by path.
The **endpoint names differ from the CRD enum** — getting this wrong is a silent
404. Full request/response shapes, the traps (`config`/`state` are never
delivered; `on_error` nests the request; `content` is `string | Block[]`; params
are flat-in / nested-under-`params`-out), and the tool-lane differences are in
[references/v1-contract.md](references/v1-contract.md). Read it before writing a
handler.

Four rules the host enforces whatever you return:

1. **Capabilities are checked on the response, not trusted.** A `patch` without
   `may_rewrite` is ignored; a `reject` without `may_deny` becomes no-decision.
   Each discard is audited.
2. **The system prompt is immutable** — stripped before you see it, spliced back
   after. You can neither inject nor delete one.
3. **Hooks are subtractive on tool calls** — you may drop or redact a tool call,
   never add or synthesize one. This is why an `onError` recover is text-only.
4. **Anything that is not a valid action means unavailable** — a 5xx, a timeout,
   a non-JSON or oversized body all resolve to hook-unavailable, which hands over
   to your declared fail-mode. Never a silent allow.

> **Tool lane may not be declarable on your cluster.** The engine routes all six
> points, but a cluster's `LlmHook` CRD enum may admit only the four LLM-lane
> values (`preCall`,`moderate`,`postCallSuccess`,`onError`). If so, a CR declaring
> `preToolUse`/`postToolUse` is rejected at admission and the point is never
> dialed. Confirm with `kubectl explain llmhook.spec.lifecyclePoints` (or a
> server-side dry-run) before committing to a tool-lane shape.

## 4. Write the server

A hook is an HTTP server and nothing more. Serve exactly the points you declare
plus a health endpoint, and stay silent on the network to keep a low trust tier.
A minimal `preCall` rewriter, dependency-free (`node:http` only):

```js
import { createServer } from 'node:http';

const PORT = Number(process.env.PORT ?? 8080);
const PREFIX = process.env.HOOK_PATH ?? '';        // must match spec.path

// content is a string OR typed blocks; only type:'text' blocks are yours.
function mapText(messages, fn) {
  let changed = false;
  const rw = (t) => { const o = fn(t); if (o !== t) changed = true; return o; };
  const next = (messages ?? []).map((m) => {
    if (typeof m.content === 'string') return { ...m, content: rw(m.content) };
    if (!Array.isArray(m.content)) return m;
    return { ...m, content: m.content.map((b) =>
      b && b.type === 'text' && typeof b.text === 'string' ? { ...b, text: rw(b.text) } : b) };
  });
  return { changed, messages: next };
}

const server = createServer(async (req, res) => {
  const url = (req.url ?? '').split('?')[0];
  const send = (s, b) => { const p = JSON.stringify(b);
    res.writeHead(s, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(p) });
    res.end(p); };

  if (req.method === 'GET' && url === `${PREFIX}/healthz`) return send(200, { ok: true });
  if (req.method !== 'POST' || url !== `${PREFIX}/v1/pre_call`) {
    return send(404, { code: 'not_found', message: `${req.method} ${url}` });
  }
  try {
    const chunks = []; for await (const c of req) chunks.push(c);
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    const { changed, messages } = mapText(body.messages, transform);
    // Log the shape you declined, never the content — on the no-op path too.
    console.log(JSON.stringify({ event: changed ? 'rewrote' : 'noop',
      blocks: (body.messages ?? []).length, chars: JSON.stringify(body.messages ?? '').length }));
    if (!changed) return send(200, { action: 'continue' });   // no patch is cheaper
    return send(200, { action: 'continue', patch: { messages } });
  } catch (err) {
    console.error(JSON.stringify({ event: 'error', message: String(err) }));
    return send(200, { action: 'continue' });   // {} is NOT a valid pre_call action
  }
});
server.listen(PORT);
for (const s of ['SIGTERM', 'SIGINT']) process.on(s, () => server.close(() => process.exit(0)));
```

Six things worth doing (they are what separates a hook that works from one that
silently does nothing):

- **Handle both `content` shapes** — string and typed blocks both arrive. A hook
  that assumes one silently ignores the conversations that carry tool results.
- **Be idempotent** — `f(f(x)) == f(x)`. A rewrite is re-applied to the whole
  projected conversation on every call, so a compounding transform degrades a long
  thread turn by turn. Assert it in tests.
- **Return no patch when nothing changed** — cheaper, and keeps the audit honest.
- **Make "did nothing" observable** — a `{action:'continue'}` with no patch is
  indistinguishable, from outside the pod, from a hook that was never dialed. Log
  a line on the no-op path too, carrying counts/kinds and a reason code (never
  content). This is the difference between "nothing reached me" and "it reached me
  and I declined" when you debug the wiring.
- **Finish inside `timeoutMs`** — an absolute deadline via `AbortSignal`, not an
  idle timer. Bound your own body reads too (mcp-host caps its side).
- **Never 5xx** — an error must still answer in the "I changed nothing" shape,
  which differs per lane (`{action:'continue'}` for `pre_call`, `{}` elsewhere).

## 5. Harden the pod, then build the image

**This is where a self-authored hook most often falls short of the platform's own
bar.** The guardrails spec invariant **N8** requires every hook pod to run
non-root, read-only root filesystem, drop all capabilities, forbid privilege
escalation, use the `RuntimeDefault` seccomp profile, and **mount no
ServiceAccount token**. HCC stamps all of this automatically **only for an `image`
target** (the pod it synthesizes). For a `service` target — the local
fast-iteration lane — the `securityContext` is **yours**, and a manifest that
sets only `runAsNonRoot` + `drop:[ALL]` (a common shortcut) still mounts the SA
token and leaves the root filesystem writable. Copy the fully-hardened Deployment
in [references/hardened-hook-manifest.md](references/hardened-hook-manifest.md);
it is the manifest to reuse, not a minimal one.

Build as an **OCI** layout for the node's architecture — a Docker v2 manifest is
rejected by the registry's Zot with a `415`, and a platform mismatch installs
cleanly then dies with `exec format error`:

```bash
docker buildx build --platform linux/<arch> --provenance=false \
  --output type=oci,tar=false,dest=./oci-layout .
jq '.manifests[].platform' ./oci-layout/index.json   # confirm arch before pushing
```

Push against the registry's OCI distribution API. Two auth transports, and the
wrong one fails confusingly: `/api/v1/*` (whoami, publish) takes the `efrk_` org
key as a plain `Authorization: Bearer`; `/v2/*` (blob/manifest push) needs the
Docker OAuth2 `grant_type=password` POST (`username=orgkey`) against
`/oauth/registry/token` — GET+Basic is the pull-only transport. Push tokens are
short-lived (~5 min), so mint per request. If the registry is behind a Cloudflare
tunnel there is a ~100 MB body cap → use chunked `POST → PATCH → PUT` for large
layers. Confirm your org before choosing a repo path:

```bash
curl -s -H "authorization: Bearer $EFRK_KEY" https://<registry-host>/api/v1/whoami
# → {"type":"machine","orgName":"<org>","scopes":["registry:publish", …]}
```

The key is bound to exactly one org, which fixes your repo path and entry scope.

## 6. Publish the catalog entry (the installation artifact)

Installing comes **only** from a registry entry — a hand-applied `LlmHook` CR is
an *output* of installation, never an input. The entry is a
`POST /api/v1/entries` (scope `registry:publish`):

```json
{
  "name": "@<org>/<name>", "version": "1.0.0", "entryType": "llm-hook",
  "description": "…", "author": "<org>", "category": "guardrails",
  "origin": "agent-generated", "visibility": "private",
  "contentCreatorTag": "community", "configCreatorTag": "community",
  "llmHook": {
    "target": { "image": { "ref": "<repo>@sha256:<64 hex>", "port": 8080 } },
    "lifecyclePoints": ["preCall"],
    "path": "/"
  }
}
```

Field rules verified against the publisher and control-api:

- `name` MUST be `@<your-org>/<name>` (unscoped → `400 scope_required`; another
  org's scope → `403 scope_not_bound`). `entryType: llm-hook` is constrained to
  `owner_type = org`.
- `target.image.ref` MUST be **digest-pinned** — a tag → `422
  image_ref_not_digest_pinned`.
- **`order`, `failMode`, and `capabilities` are deliberately NOT here** — they are
  install-time arguments belonging to the admin, not the author (§7).
- **`contentAccess` cannot be declared here** — the registry's `normalizeHookMeta`
  never persists it, so the platform infers, and the projection is
  `contentAccess !== 'metadata'` ⇒ bodies **are** projected by default. A
  genuinely metadata-only hook cannot be expressed through the registry path.
- **`defaultConfig` is accepted, stored, and never delivered to your container.**
  Do not read `body.config` at runtime — bake defaults into the image or read
  `process.env` you set yourself.
- `hook_meta` is stored **field by field** — unknown keys are dropped. Read the
  entry back and diff what survived.
- **`name`+`version` is hard-immutable** — re-POSTing the pair returns `409`, and
  metadata chosen here cannot be edited by republishing. Bump the version.

## 7. Install onto a Host

Publishing puts the hook in the catalog; installing puts it in front of a model
call and carries the fields that belong to the **admin, not the author**. Same
endpoint whether done programmatically or from control-ui:

```
POST /api/v1/admin/registry/install-hook
{ "hostRef": "<host>", "registryEntryName": "@<org>/<name>",
  "registryEntryVersion": "1.0.0", "hookName": "<name>",
  "capabilities": ["may_rewrite"], "order": 5, "failMode": "open" }
```

The saga runs ten steps and rolls back on failure; the ones that bite:

| Step | Fails with | Meaning |
|---|---|---|
| Trust floor | `403 hook_below_trust_floor` | entry's resolved trust < Host `minInstalledHookTrustLevel` |
| Content/egress | `403 content_egress_requires_high_trust` | bodies + egress from a non-curated org |
| Capability ceiling | `403 capability_exceeds_ceiling` | asked for a capability outside `guardrails.capabilityCeiling` |
| Image preflight | `422 image_ref_not_digest_pinned` | a tag in `target.image.ref` |

> **Trap · the empty ceiling.** Step 6 reads
> `ceiling = guardrails.capabilityCeiling ?? []`, so a Host that declares **no
> ceiling permits nothing** — every install asking for any capability fails
> `403 capability_exceeds_ceiling`, including a hook wanting only `may_rewrite`.
> That is the safe default, not a bug, and it is the most likely reason a
> correctly published hook will not install on a Host that has never hosted one.
> The fix is the **operator's**, on the Host, not your entry — widen
> `capabilityCeiling` to admit exactly what your hook asks for. A pure observer
> (`capabilities: []`) sidesteps this entirely: the empty set is a subset of every
> ceiling.

Fail-posture is a CRD rule, not a preference: **`may_deny` requires `failMode:
closed`** (a deny-authoritative hook that fails open can be bypassed by killing
the pod). An optimizer or observer should fail **open** — being down should cost
tokens, never availability. On the tool lane, a `failMode: closed` `postToolUse`
hook that is unavailable **replaces the tool result with a withheld-content
notice** — an observer must never be able to do that, so give it `failMode: open`
and no capabilities.

Trust level is **not yours to set** — `clerum.io/trust-level` is stamped by the
installer (platform-assigned); setting it in a manifest is meaningless.

Write the intended `capabilities`, `order`, and `failMode` — and *why that
fail-mode is safe* — into the entry `description`, because an operator installing
a hook they did not write is choosing those from your description.

## 8. Verify it actually runs

Resolution is not execution. The host logs resolution when it builds the chain:

```
resolved hook <name> -> http://<name>.llm-hooks.svc.cluster.local:8080/
  points=[pre_call] caps=[may_rewrite] failMode=open
```

Then prove it was consulted:

- Send a real message and watch **your own** pod's logs for the call — this is
  where the no-op log line from §4 earns its place: a quiet pod means either
  "never dialed" or "dialed and declined", and only a both-paths log line tells
  them apart.
- **A/B the one thing your hook moves** — `input_tokens` for a rewriter, a deny
  for a policy hook, a changed response for a substituter — with the hook in the
  lifecycle list and with it removed. Use a **distinct sender each run**:
  conversation history silently inflates the input, and a `moderate` hook re-scans
  the *entire* projected conversation, so a message it denied once keeps every
  later turn in that same session denied too (a reused session makes the second
  half of an A/B meaningless).
- Check `status.conditions` on the CR (the reconciler writes conditions, not flat
  phase fields). Note `status.observedDigest` is populated **only for image
  targets** — a `service`-target hook is never digest-verified by the platform.

The direct way to A/B, with no channel or desktop app, is to drive the Host's
own runtime endpoint from inside its pod (loopback bypasses the NetworkPolicy; the
runtime edge guard is **header-based, not a JWT**):

```bash
POD=$(kubectl -n mcp-host get pod -l clerum.io/host-name=<host> -o name | head -1)
kubectl -n mcp-host exec -i "$POD" -- node -e '
  const s = "abtest-" + Date.now();                    // distinct sender => fresh session
  fetch("http://localhost:8080/v1/runtime/messages", {
    method: "POST",
    headers: {                                          // NO authorization header
      "content-type": "application/json",
      "x-clerum-edge-caller": "channel-reader",
      "x-clerum-edge-host-ref": "<host>",
      "x-clerum-edge-channel-type": "telegram",
      "x-clerum-edge-channel-id": "c-" + s,
      "x-clerum-edge-sender": s },                      // headers must match the body
    body: JSON.stringify({ content: "your test prompt", channelType: "telegram",
      channelId: "c-" + s, sender: s, timestamp: new Date().toISOString(),
      messageId: "m-" + s, hostRef: "<host>" }),
  }).then(async r => console.log(r.status, await r.text()));
'
```

The response is synchronous. Then read the **hook pod's** own log for the event
it emitted (`deny`, `redacted`, `recovered`, …) — that, not the agent's reply, is
the proof the hook fired rather than the model behaving similarly on its own.

## 9. What a hook can — and cannot — know about the caller

A recurring ask: attribute activity to the agent or human calling. **Nothing on
the wire identifies the caller.** No session or host id is in any hook body
(`projectForHook` strips `usageContext` explicitly), and the bearer is advisory.
The **one in-band signal is the hook's own path**, since mcp-host dials
`{endpoint}{path}/v1/{point}`.

So the **agent (Host)** dimension is reachable by convention, with no platform
change: install **one `LlmHook` CR per Host** with `path: /a/<host>`, and have the
server take the last path segment as the agent key. Identical images share one pod
by digest-dedup, so N Hosts cost N CRs and no extra pods. **Per-session or
per-human** attribution is *not* reachable from a hook — it would need mcp-host to
put an id on the wire.

## 10. Gotchas (by the error you see)

- `415` on push → Docker v2 manifest; build `--output type=oci`.
- `exec format error` on start → wrong architecture; `--platform` the node, check
  `index.json`.
- Hook resolves but never fires → `spec.path` and the server's path prefix
  disagree.
- Dialed but never changes anything → `content` arrived as blocks and the hook
  only handled strings.
- Settings are always the defaults → read `body.config`; it is never delivered.
  Use `process.env`.
- `onError` sees no messages → they are at `body.request.messages` (the body
  nests the request).
- Tool hook dialed and ignored → returned `{action,patch}` on a tool point; the
  tool lane speaks `decision`/`updatedInput`/`updatedResult`.
- `preToolUse` rejected at admission → the CRD enum has only the four LLM-lane
  points on this cluster.
- Tool result replaced by a withholding string → a `failMode: closed` `postToolUse`
  was unavailable (design behavior, not a bug).
- `403 capability_exceeds_ceiling` on a correct hook → the Host declares no
  `capabilityCeiling`; the operator must widen it.
- `pre_call` falls to fail-mode on error → returned `{}`; return
  `{action:'continue'}` to mean "no change".
- A `preCall` params hook (e.g. a `max_tokens` cap) "never does anything" → the
  agent often sends **no** generation params on the pre_call body (they arrive
  `undefined`), so there is nothing above the ceiling to trim; it engages only when
  a client sets one. Compare against the value you actually received, and log it.
- `ImagePullBackOff` on a published+installed hook → the install-time pull
  credential for a private image is an operator/cluster question, not an authoring
  one; confirm the digest is correct, then it is theirs.

## Checklist before you publish

- [ ] The four axes are decided, and the trust tier they imply is reachable for your org.
- [ ] The server answers exactly the `/v1/{point}` paths matching `lifecyclePoints`, plus a health endpoint — response shape matches that point's lane.
- [ ] `spec.path` and the server's path prefix agree.
- [ ] Nothing reads `body.config` / `body.state`; neither is ever delivered.
- [ ] Both `content` shapes handled; non-text blocks pass through untouched.
- [ ] The transform is idempotent: `f(f(x)) == f(x)`.
- [ ] `may_deny` paired with `failMode: closed` — or not requested.
- [ ] Logs carry counts/kinds, never content — including on the no-op path.
- [ ] The pod manifest is N8-hardened (readOnlyRootFilesystem, runAsNonRoot, drop ALL, no SA token, seccomp RuntimeDefault) — a `service` target is your responsibility.
- [ ] Image built OCI for the node's arch; `target.image.ref` digest-pinned.
- [ ] No `egressBindings` unless the hook needs it and can clear `high`.
- [ ] `hook_meta` read back and diffed — nothing silently dropped.
- [ ] Verified by A/B on a fresh session, not by the resolution log alone.
