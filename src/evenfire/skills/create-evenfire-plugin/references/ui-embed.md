# The plugin UI inside the Evenfire Desktop app

The Desktop app shows your UI workload in an isolated Chromium view, through
the platform's `rpc-proxy`. Verified against evenfire-ai/evenfire `dev` at
`0b26101eb` (`rpc-proxy/src/routes/sandboxUi.ts`, `rpc-proxy/src/services/`,
`desktop-app/src/sandboxUi*.ts`, `desktop-app/src/pluginSdk*.ts`,
`packages/desktop-app-links/`).

## How a request reaches your UI pod

```
Desktop view  https://<rpc-proxy host>/api/v1/sandbox-ui/sandbox-recipes/<recipe>/view/<route>?<query>
   |   cookie clerum_sandbox_ui_session (5 min, set by the Desktop)
   v
rpc-proxy     checks the cookie, strips Cookie, Authorization and any X-Clerum-*,
   |          adds X-Clerum-User and X-Clerum-Recipe, removes the prefix
   v
UI pod        GET /<route>?<query>      (port 8080, in sandbox-ui)
   |          your server (e.g. nginx) proxies /api/* to the backend
   v
backend       in sandbox-recipes, reached through spec.ui.egress.internal
```

What your UI server receives:

- **The path without the prefix**: `/` plus the route, percent-decoded once,
  with the original query string. No HTML or response body is rewritten and no
  `<base>` is injected.
- **`X-Clerum-User`**: the platform user's UUID (not an email). This is the
  only identity your plugin gets. Forward it to your backend and key all
  per-user data by it. A request without it is unauthenticated.
- **`X-Clerum-Recipe`**: `<namespace>/<recipe>` (for example
  `sandbox-recipes/recipe-acme-notes-v1-0-0-3f2a9c1d`).
- **No `Cookie` and no `Authorization` header, ever.** Cookies your server sets
  reach the browser, but rpc-proxy never forwards them to your server, so
  sessions based on cookies do not work. Do not put your own tokens in
  `Authorization` either.
- `Host` is the internal Service name; do not build URLs from it.

Only rpc-proxy can reach the UI pod (`sandbox-ui` denies all other ingress),
which is why `X-Clerum-User` can be trusted there. Pass it on to the backend
explicitly (the nginx asset does). Inside the recipe, other workloads that can
reach the backend (an MCP server, the webhook gateway) could send that header
too; give them their own credential (a shared token from a Secret) when the
backend must tell them apart.

## Serving the SPA under a prefix

The browser's URL carries the prefix, the pod's does not, and the page may be
opened at a nested route (deep links and the Desktop's Refresh both do this).
Three rules, all proven in a shipped plugin:

1. Build with relative asset URLs (Vite `base: './'`) and no inline scripts.
2. At startup, pin `<base href>` to the embed root computed from
   `location.pathname`, and call your backend with relative URLs. The helper
   in [../assets/embed-base.ts](../assets/embed-base.ts) does both, plus the
   session recovery below.
3. Serve `index.html` for unknown paths and map `<route>/assets/...` back to
   the real files. [../assets/nginx-default.conf.template](../assets/nginx-default.conf.template)
   does this.

Use pathname (history) routing with the embed root as the router basename
(`embedBasename` in the helper), and handle `popstate`: the Desktop delivers a
deep link by loading `defaultPath`, then calling `history.replaceState` to the
target route and dispatching `popstate`.

## Response policy you cannot change

rpc-proxy overwrites these headers on every proxied response:

```
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
  img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none';
  base-uri 'self'; form-action 'self'
Permissions-Policy: accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(),
  microphone=(), payment=(), usb=(), interest-cohort=()
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
X-Frame-Options: DENY
```

So: bundle every script and font yourself (no CDN), no inline `<script>`, no
`eval`/`new Function`, no WebAssembly compilation, images only from your origin
or `data:`, no browser calls to other origins (relay them through your
backend), and you cannot frame your own pages. Inline styles are allowed.

## Transport

- **No WebSockets.** Use server-sent events or polling.
- **SSE works** through rpc-proxy, which adds no buffering or timeout of its
  own (an edge proxy in front of it may still close idle connections). Behind
  your own nginx, send `X-Accel-Buffering: no` from the backend and a comment
  heartbeat (for example `: heartbeat` every 25 s) more often than
  `proxy_read_timeout`, and reconnect when the stream closes. The session
  cookie is checked only when a request starts, so an open stream outlives it;
  a reconnect needs a valid session (see below).
- rpc-proxy sets no request body limit for your UI; your servers' own limits
  apply.
- Send `Accept: application/json` on API calls. While the recipe is not
  serving (not `active`, or no Ready UI pod), rpc-proxy answers every request
  with `503` (`Retry-After: 5`) and after removal with `410`; with the default
  `Accept: */*` that answer is an HTML page, with JSON it is
  `{"error":"recipe_updating"|"recipe_gone"}`.

## The session

- The Desktop sets a 5-minute cookie and re-mints it every 270 s while its
  window is visible. While the window is hidden or minimized no refresh
  happens, so the first request after restoring can get `401
  {"error":"sandbox_ui_session_invalid"}` (or `..._required`).
- Recover with `await window.clerum.requestSessionRefresh()`, then retry once.
  It is limited to one call per 30 s per view and throws when called too soon.
  If a refresh fails, the Desktop stops refreshing that view and tells the
  user to reopen the app.
- Removing a user's access takes effect when their session cookie expires (up
  to 5 minutes).

## The Plugin UI SDK (`window.clerum`)

Only present inside the Desktop: feature-detect it and degrade gracefully in a
plain browser. Calls resolve to
`{ok: true, data} | {ok: false, error: {code, message, retryable}}`; still wrap
them, because an older Desktop may lack a method.

| Call | Capability | Returns | Asks the user |
|---|---|---|---|
| `clerum.theme.get()` | `theme.read` | `{theme: 'light'\|'dark'}` | no |
| `clerum.identity.get()` | `identity.read` | `{userId, email, name}` | yes |
| `clerum.org.get()` | `org.read` | `{teamId, teamName, role}` | yes |
| `clerum.agents.list()` | `agents.read` | `{agents: [{id, name, contextRef, provider, mcpServers}]}` | yes |
| `clerum.contexts.list()` | `contexts.read` | `{contexts: [{id, scope}]}` | yes |
| `clerum.mcp.list()` | `mcp.read` | `{servers: [{name, agents}]}` | yes |
| `clerum.gfs.list({drive?, resourceId?, cursor?})` | `gfs.list` | `{items, nextCursor}` | yes |
| `clerum.gfs.read({uri, as: 'text'\|'dataUrl'})` | `gfs.read` | text (max 2 MB) or an image data URL (max 10 MB; png, jpeg, gif, webp, avif) | yes |
| `clerum.gfs.open(uri)` | `gfs.open` | `{opened}`: shows a `gfs://` file in the Desktop's viewer | no |
| `clerum.notifications.notify({title, body?, ref?})` | `notifications.notify` | `{delivered, reason?}` | yes |

- Permissions: `clerum.sdk.permissions(ids?)` reads grants without prompting;
  `clerum.sdk.requestPermissions(ids)` (1 to 8 ids) shows one consent modal for
  the missing ones. A view may show at most 3 modals, 10 s apart; an
  unanswered modal (120 s) counts as denied, and a denial sticks until the
  plugin is reopened. Grants persist until the user revokes them. Ask once,
  batched, and check `permissions()` first so returning users see nothing.
- Events: `clerum.sdk.on(cb)` (returns an unsubscribe function) delivers
  `theme.changed {theme}`, `permission.changed {capability, granted}`,
  `session.changed {authenticated}` and `notification.clicked {ref}`.
- Rate limits per capability (for example `gfs.open` 6/min,
  `notifications.notify` 2/min and 20/h) and 120 calls/min per plugin in total;
  over the limit you get `rate_limited`. `clerum.sdk.capabilities()` lists what
  the running Desktop supports.
- **Theme:** call `clerum.theme.get()` at startup and follow `theme.changed`
  (there is no initial event). Do not rely on `prefers-color-scheme` inside the
  view: it does not follow the Desktop's theme setting.
- `notifications.notify` is a local OS notification from an open plugin
  (title max 120, body max 400 characters, suppressed while your plugin is in
  the foreground). To reach users who are not looking at your plugin, send
  notifications from the backend with the
  [Plugin Workload SDK](plugin-workload-sdk.md).
- `clerum.requestSessionRefresh()` and `clerum.onOauthCompleted(cb)` complete
  the surface. There is no Node, no `ipcRenderer`, and nothing else.

## Links, downloads and browser features

- Navigation inside your `…/view/` prefix stays in the view. An `http(s)`
  link elsewhere (including `target="_blank"` and `window.open`) opens in the
  user's browser. `window.open` to your own prefix does nothing. `mailto:`,
  `tel:`, `data:`, `blob:` and `javascript:` links are dropped.
- `gfs://<drive>/<resource>` links open the Desktop's file viewer.
- **Downloads are blocked** for every plugin view. Show content in the page,
  or put files where the user can reach them (for example the Global File
  System).
- Only the `fullscreen` permission is granted: the async Clipboard API, web
  notifications, camera, microphone and geolocation are denied. `window.prompt`
  is not available in Electron.

## Deep links

Link to a place in your plugin with
`<profile UI host>/open/apps/<namespace>/<recipe>?path=<route>&team=<teamId>`
(a web page that hands over to the Desktop), or the Desktop protocol
`evenfire://app/<namespace>/<recipe>?path=<route>&team=<teamId>`. The user
confirms before the Desktop opens it. `path` must start with a single `/`
(max 4096 characters, no `?`, `#`, `\` or dot segments); queries and fragments
are not carried. The Desktop's "copy link" produces the same form from the
current pathname. Plugins usually take the base
(`https://<profile UI host>/open/apps/sandbox-recipes/<recipe>`) as a
configurable env value, because the recipe cannot know it.

## OAuth from the UI

For a client declared in `spec.oauthClients[]`:

1. Navigate to `clerum://oauth?clientId=<client id>` (add `&background=1` to
   ask the user for background access as well). The Desktop opens the
   provider's consent page in the user's browser.
2. When it completes, `window.clerum.onOauthCompleted(cb)` delivers
   `{oauthClientId, provider}` to the plugin view that is open. Filter by your
   own client id.
3. Get a token with the session cookie, from the recipe's base (the embed root
   without `view/`):

```js
const root = new URL('..', document.baseURI)  // …/sandbox-ui/<ns>/<recipe>/
const res = await fetch(new URL('oauth/token', root), {
  method: 'POST',
  headers: { 'content-type': 'application/json', accept: 'application/json' },
  body: JSON.stringify({ oauthClientId: 'salesforce' }),
})
// 200 {accessToken, expiresAt} | 404 {error: "no_grant"} | 503 integration_not_configured
```

`DELETE …/oauth/grant` with `{oauthClientId}` disconnects. Failures of the
connect step are not shown to the user, so check `oauth/token` when the view
loads and show a Connect button when it says `no_grant`.

## Who can open the plugin

- An operator grants users (Members tab) or teams (Teams tab) on the plugin's
  page in the Control UI. A team grant counts only while the user works in that
  team in the Desktop. A fresh install is visible to nobody until granted;
  being an admin does not bypass it.
- The Desktop lists only plugins that are `active`.
- One plugin view is open at a time. Switching to another Desktop section
  closes it (in-memory state is lost; the route is restored), and the HTTP
  cache is cleared every time the view opens.
- `localStorage`, IndexedDB and cookies live in a partition per environment
  and plugin, shared by every user who signs in on that Desktop. Do not keep
  one user's data there without keying it by user, and keep secrets out.
- `document.title` becomes the Desktop tab title. The Desktop handles its own
  shortcuts (for example Mod+F, Mod+K, Mod+1..9) before your page sees them.
