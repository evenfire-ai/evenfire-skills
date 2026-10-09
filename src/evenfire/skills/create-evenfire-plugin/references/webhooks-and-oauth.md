# Webhooks and OAuth

Verified against evenfire-ai/evenfire `dev` at `0b26101eb` (CRD,
`workflow-recipes/src/reconciler/webhookGatewayBuilder.ts`, `webhook-proxy/`,
`control-api/src/routes/recipeOauth.ts`).

## Webhooks (`spec.webhooks[]`, max 16)

A webhook gives a third party a public URL. The platform verifies the request,
then forwards it to one of your workloads. The pieces:

```
provider -> webhook-proxy (public, CORS) -> per-recipe webhook gateway (verifies) -> your handler workload
```

**Only recipes without `steps` get the gateway.** WRC builds the webhook
gateway in the workload reconcile path; a recipe with any `steps` (even a
dormant one) never gets it, and its webhook URLs fail. If you need both
webhooks and scheduled work, keep the recipe stepless and run the schedule in
your own `cronjob` workload.

### Fields

| Field | Values | Notes |
|---|---|---|
| `id` | `^[a-z0-9-]{1,63}$`, unique (W1) | Last segment of the public URL |
| `workloadRef` | a workload id | Must be a `deployment` without `transport` (W2, checked by WRC) |
| `path` | starts with `/`, max 256, no `.`/`..` segments, no `//`, no spaces | The path your handler receives |
| `methods` | subset of `POST`, `GET`; default `[POST]` | Must include POST (W4); GET only with `setupHandshake` (W13) |
| `maxBodyBytes` | 1024 to 10485760, default 1048576 | |
| `optional` | default `false` | `true`: while its Secret is missing the URL answers `410` with `X-Clerum-Webhook-State: dormant` instead of degrading the recipe |
| `cors.allowedOrigins[]` | max 32, exact `https?://host[:port]` | Omitted or empty: server-to-server only (browser preflight gets 403). No wildcards |
| `verification` | see below | Required |
| `replay` | `{timestampHeader, toleranceSec 10-3600 (default 300)}` | Required with `hmac-sha256-timestamp-body` (W8) |

Everything below sits under `verification`, next to `scheme`:

- `hmac-sha256-body`: `secretRef {name, key}` and `signatureHeader`. The
  gateway computes HMAC-SHA256 of the raw body with the Secret value and
  compares it with the header after removing the optional `signaturePrefix`
  (e.g. `sha256=`); `signatureEncoding` is `hex` (default) or `base64`. This is
  GitHub's `X-Hub-Signature-256: sha256=<hex>` form.
- `hmac-sha256-timestamp-body`: the same fields, plus a top-level `replay`
  block. The signed string is exactly `<timestamp>.<raw body>`, where the
  timestamp is the value of `replay.timestampHeader` in whole Unix seconds and
  must be within `replay.toleranceSec` of the gateway clock. Check that your
  provider signs that exact string: Slack, for example, signs
  `v0:<timestamp>:<body>`, which does not verify here.
- `static-bearer`: `secretRef`; optional `tokenHeader` (default
  `Authorization`) and `tokenPrefix` (default `Bearer `; an explicit empty
  string means the whole header value is the token).
- `jwt-bearer-jwks`: `jwksUrl` (https, DNS host), `issuer`, `audience`; no
  `secretRef` (W7, W9, W12). It passes admission, but at this revision nothing
  loads the JWKS into the gateway, so a request with a well-formed token gets
  `500 verifier_misconfigured`. Use another scheme.
- `setupHandshake {strategy, secretRef?}`: `meta-hub-challenge` (needs its own
  `secretRef` and `GET`, W14) answers the subscription `GET` before any
  signature check; `slack-url-verification` answers a `url_verification`
  challenge after the signature verified. `stripe-verify` passes admission
  but is not implemented: every request to that webhook gets `500
  verifier_misconfigured`.

The Secret value is used as stored, minus one trailing newline. Two webhooks:

```yaml
spec:
  webhooks:
    - id: form-intake                # last segment of the public URL
      workloadRef: api               # a deployment without transport
      path: /webhooks/form-intake    # the path the handler receives
      maxBodyBytes: 65536
      optional: true                 # 410 until the Secret exists, instead of degrading the recipe
      verification:
        scheme: static-bearer        # Authorization: Bearer <token>
        secretRef: { name: notes-webhooks, key: form-token }
    - id: github
      workloadRef: api
      path: /webhooks/github
      verification:
        scheme: hmac-sha256-body
        secretRef: { name: notes-webhooks, key: github-secret }
        signatureHeader: X-Hub-Signature-256
        signaturePrefix: 'sha256='
```

### Secrets for webhooks

`secretRef` Secrets live in `sandbox-recipes` and need the same ownership label
as any recipe Secret (`clerum.io/owner-recipe=<recipe>` or
`clerum.io/shared=true`). A webhook's Secret counts as missing when it does not
exist, lacks the key, or is not owned by the recipe.

- With `optional: true` the webhook answers 410 (condition `WebhookDormant`)
  and the rest of the recipe keeps working; creating the Secret activates it.
- On a required webhook (the default), one missing Secret deletes the whole
  webhook gateway, so every webhook of the recipe stops, and the recipe goes
  `degraded` (condition `WebhookSecretMissing`, message `Webhook gateway
  disabled: ...`). A `degraded` recipe is not served in the Desktop, so the
  plugin's UI goes offline too. It never fails the recipe. A `workloadRef`
  that is not a deployment without `transport` does the same, with
  `WebhookHandlerInvalid`.

The gateway reads the Secret file on every request, so a rotated value takes
effect once Kubernetes refreshes the mounted Secret, without a restart.

### The public URL

```
https://<webhook host>/api/v1/webhook/sandbox-recipes/<recipe name>/<webhook id>
```

- `<webhook host>` is wherever the operator exposes the platform's
  `webhook-proxy` (the base manifests ship no Ingress for it). Ask your
  operator, and make the base configurable in your plugin rather than deriving
  it.
- `<recipe name>` is the in-cluster name, which a registry install generates
  (`recipe-<slug>-v<version>-<hash>`). Read it from the cluster or the Control
  UI after installing; do not build URLs from the name in your YAML.
- The gateway forwards the verified request to the handler's `port` at exactly
  `webhooks[].path`: same method and body, but no query string. It strips
  `Authorization`, `Cookie`, the signature and replay headers and any inbound
  `x-clerum-*`, and adds `x-clerum-webhook-id`, `x-clerum-webhook-recipe`
  (`<namespace>/<recipe>`) and `x-clerum-webhook-verified-at`, so a caller on
  the internet cannot spoof them. Verification already happened; a custom
  `tokenHeader` is not on the strip list, so do not log request headers.
- Every other header is forwarded, `Content-Type` included. The handler's
  status, headers and body go back to the caller unchanged. The handler has
  30 s to answer before the gateway replies `504 {"error":"gateway_timeout"}`
  (`502 upstream_error` when it cannot be reached).

What the gateway answers before your handler runs:

| Status | `error` | When |
|---|---|---|
| 401 | `invalid_signature` | Missing or wrong signature or token |
| 405 | `method_not_allowed` | Method not in `methods` |
| 408 | `timestamp_skew`, `request_timeout` | Timestamp outside `toleranceSec`; the body stalled for 10 s |
| 410 | `integration_not_configured` | An `optional` webhook is dormant (header `X-Clerum-Webhook-State: dormant`) |
| 413 | `body_too_large` | Over `maxBodyBytes` |
| 500 | `verifier_misconfigured` | The Secret cannot be read, or a scheme or strategy that does not work at this revision (above) |
| 503 | `gateway_busy` | 256 requests already in flight in the recipe's gateway |

For browser widgets that call a webhook from a customer site, list every exact
origin in `cors.allowedOrigins`, and treat any bearer token embedded in a page
as public: it identifies the install, it does not protect data.

## OAuth (`spec.oauthClients[]`, max 8)

`{id, provider, clientIdRef {name, key}, clientSecretRef {name, key}, scopes?,
backgroundAccess?}`. `provider` is one of `salesforce`, `slack`, `notion`,
`microsoft-graph`, `google` (adapters compiled into the platform; other
providers are rejected at admission). The client id and secret Secrets live in
`sandbox-recipes`. Every client needs a consumer (O1): a `spec.ui`, or a
workload that lists it in `oauthClientRefs`.

### Background OAuth (a workload acts without a user present)

1. Declare the client with `backgroundAccess: true`. Include the provider's
   offline scope when it has one (`refresh_token` for Salesforce,
   `offline_access` for Microsoft Graph).
2. List it in the workload: `oauthClientRefs: [<client id>]` (not on MCP or UI
   workloads).
3. An operator connects the recipe-owned grant from the recipe's Integrations
   tab in the Control UI.
4. WRC mounts a rotating broker token at
   `/var/run/clerum/oauth-broker/broker-token` and sets
   `RECIPE_OAUTH_BROKER_TOKEN_FILE` to that path, plus the network path to the
   broker (do not add it to `egressBindings`). Read the file on every call:

```
POST http://control-api.control-plane.svc.cluster.local:8090/api/v1/recipe-oauth/token
Authorization: Bearer <contents of $RECIPE_OAUTH_BROKER_TOKEN_FILE>
Content-Type: application/json

{"oauthClientId": "<client id>"}
```

| Status | Body | Meaning |
|---|---|---|
| 200 | `{accessToken, expiresAt}` | Use it for the provider call |
| 404 | `{error: "no_grant"}` | The operator has not connected it (or removed it) |
| 400 | `{error: "unknown_oauth_client"}` | Not declared, or not `backgroundAccess` |
| 503 | `integration_not_configured` | The client id/secret Secret is missing |
| 502 | `{error: "refresh_failed", status, detail}` | The provider refused the refresh |
| 429 | | Per-recipe rate limit |

Ask the broker before each provider call: it returns the stored token while
it is valid and refreshes it server side shortly before it expires. It does
not force a refresh, so a provider 401 on a token the broker still considers
valid means the grant must be reconnected. The provider's own API host still
goes in the workload's `egressBindings`.
`POST /api/v1/recipe-oauth/user-token` `{oauthClientId, userId}` and
`GET /api/v1/recipe-oauth/users?oauthClientId=` serve per-user background
grants for users who consented.

### Foreground OAuth (the user connects from the plugin UI)

See [ui-embed.md](ui-embed.md#oauth-from-the-ui).
