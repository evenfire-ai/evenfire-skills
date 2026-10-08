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

`verification.scheme`:

- `hmac-sha256-body`, `hmac-sha256-timestamp-body`: `secretRef {name, key}`
  plus `signatureHeader`; optional `signaturePrefix` (e.g. `sha256=`) and
  `signatureEncoding` `hex` (default) or `base64`.
- `static-bearer`: `secretRef`; optional `tokenHeader` (default
  `Authorization`) and `tokenPrefix` (default `Bearer `; an explicit empty
  string means the whole header value is the token).
- `jwt-bearer-jwks`: `jwksUrl` (https, DNS host), `issuer`, `audience`; no
  `secretRef` (W7, W9, W12).
- `setupHandshake.strategy`: `meta-hub-challenge` (needs its own `secretRef`
  and `GET`, W14), `slack-url-verification`, `stripe-verify`. Only that exact
  handshake request skips signature verification.

### Secrets for webhooks

`secretRef` Secrets live in `sandbox-recipes` and need the same ownership label
as any recipe Secret (`clerum.io/owner-recipe=<recipe>` or
`clerum.io/shared=true`). A missing or unowned Secret on a required webhook
deletes the gateway and marks the recipe `degraded` (conditions
`WebhookSecretMissing` / `WebhookHandlerInvalid`); it never fails the recipe.

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

On a provider 401, ask the broker again once; it refreshes server side. The
provider's own API host still goes in the workload's `egressBindings`.
`POST /api/v1/recipe-oauth/user-token` `{oauthClientId, userId}` and
`GET /api/v1/recipe-oauth/users?oauthClientId=` serve per-user background
grants for users who consented.

### Foreground OAuth (the user connects from the plugin UI)

See [ui-embed.md](ui-embed.md#oauth-from-the-ui).
