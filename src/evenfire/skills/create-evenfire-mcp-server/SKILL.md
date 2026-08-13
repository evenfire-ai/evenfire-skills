---
name: create-evenfire-mcp-server
description: >-
  Build, test, and package an MCP (Model Context Protocol) server for Evenfire so
  it can live in the org catalog, install on a cluster, and be used by the chat
  agent. Covers the two server shapes (a local image you build, or a remote
  wrapper around a hosted endpoint), the streamableHttp/stdio server code
  contract, Dockerfile hardening, the registry.json schema, the naming rule,
  local handshake testing, how the platform deploys it (direct vs stdio-bridge vs
  egress-proxy), and how to attach it to a chat agent via a Context. Use when
  creating, changing, testing, or onboarding an MCP server/connector.
---

# Building an Evenfire MCP server

> **Verified against Evenfire `dev` at commit `f9e8d0487`.**
> CRD API `clerum.io/v1alpha1`, `clerum-crds` chart `0.8.0`. Field names, enums,
> endpoints, and commands below match that revision.

An MCP server exposes tools (and optionally resources/prompts) over the Model
Context Protocol. On Evenfire it reaches the chat agent through `mcp-host`, which
discovers servers a Context allows and prefixes every tool as
`serverName__toolName` (double underscore). A server is packaged as one directory
carrying a `registry.json` (and, for a local image, source + a Dockerfile).

Everything here is verified against the platform code
(`host-context-controller`, `mcp-host`, `control-api`, the `clerum.io` McpServer
and Context CRDs) and the shipped server collection. To publish what you build,
use the `publish-evenfire-plugin` skill. If instead you want the MCP server to
ship inside a larger UI plugin, add it as a `transport` workload with the
`create-evenfire-plugin` skill.

## 1. Pick the shape

| Shape | When | Directory contents | transport |
|---|---|---|---|
| Local, own code | you write the tools (call a vendor REST API, a chain RPC, etc.) | `src/`, `package.json`, `Dockerfile`, `registry.json` | `streamableHttp` |
| Local, reference wrapper (Node) | a thin image around an upstream stdio MCP server | `Dockerfile`, `registry.json` (no `src`) | `stdio` + a `command[]` |
| Local, reference wrapper (Python) | an upstream Python stdio server exposed over HTTP via `supergateway` | `Dockerfile`, `registry.json` | `streamableHttp` (no `command`) |
| Remote wrapper | the vendor already hosts a stable MCP endpoint | JUST `registry.json` (no image) | `streamableHttp` (never `stdio`) |

Prefer a remote wrapper whenever the vendor hosts a stable endpoint: nothing to
build or maintain, Evenfire fronts it with an nginx egress proxy. Write your own
code only when there is no good upstream. Name the directory `mcp-<vendor>` for a
local server, `mcp-<vendor>-remote` for a remote wrapper.

## 2. The own-code server contract (local, streamableHttp)

The server MUST listen on `0.0.0.0:PORT` (default 3000) and serve MCP over
StreamableHTTP at `/mcp`. Binding `127.0.0.1` makes the pod fail its probe. A
minimal, verified `src/index.ts`:

```typescript
import express from 'express'
import { randomUUID } from 'node:crypto'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js'
import { z } from 'zod'

const PORT = Number(process.env.PORT ?? 3000)
const API_KEY = process.env.EXAMPLE_API_KEY   // must equal a credentialSchema key name

function buildServer(): McpServer {
  const server = new McpServer({ name: 'mcp-example', version: '1.0.0' })
  server.tool(
    'get_thing',
    'Fetch a thing by id from the Example API.',
    { id: z.string().describe('the thing id') },
    async ({ id }) => {
      const res = await fetch(`https://api.example.com/things/${id}`, {
        headers: { authorization: `Bearer ${API_KEY}` },
      })
      if (!res.ok) return { content: [{ type: 'text', text: `error ${res.status}` }], isError: true }
      return { content: [{ type: 'text', text: JSON.stringify(await res.json()) }] }
    },
  )
  return server
}

const app = express()
app.use(express.json())

const transports = new Map<string, StreamableHTTPServerTransport>()

app.post('/mcp', async (req, res) => {
  const sid = req.header('mcp-session-id')
  let transport = sid ? transports.get(sid) : undefined
  if (!transport) {
    transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: randomUUID,
      onsessioninitialized: (id) => transports.set(id, transport!),
    })
    await buildServer().connect(transport)
  }
  await transport.handleRequest(req, res, req.body)
})
app.get('/mcp', async (req, res) => {
  const t = transports.get(req.header('mcp-session-id') ?? '')
  if (!t) return res.status(400).end()
  await t.handleRequest(req, res)
})
app.delete('/mcp', async (req, res) => {
  const t = transports.get(req.header('mcp-session-id') ?? '')
  if (!t) return res.status(400).end()
  await t.handleRequest(req, res)
})
app.get('/health', (_req, res) => res.json({ ok: true }))

app.listen(PORT, '0.0.0.0', () => console.log(`mcp-example on :${PORT}/mcp`))
```

Notes verified against the platform:

- The MCP session id travels in the `mcp-session-id` request header.
- `/health` is a convenience for humans and for stdio-bridge/remote deploys. For
  a LOCAL streamableHttp server the platform readiness probe is a **tcpSocket**
  probe on the port, not `httpGet /health`. What matters is that the process
  opens a TCP listener on the declared port bound to `0.0.0.0`.
- A tool handler returns `{ content: [{ type: 'text', text }], isError? }`.
- Credential env-var names read by the code (here `EXAMPLE_API_KEY`) MUST match
  the `credentialSchema.keys[].name` in `registry.json` exactly, or an
  operator-entered secret never reaches the running server.

## 3. Dockerfile (own-code, hardened)

```dockerfile
FROM node:24-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY tsconfig.json ./
COPY src/ ./src/
RUN npm run build

FROM node:24-alpine
LABEL org.opencontainers.image.title="mcp-example" \
      org.opencontainers.image.description="Example MCP server." \
      org.opencontainers.image.source="https://github.com/<org>/<repo>" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.licenses="MIT"
WORKDIR /app
COPY --from=builder /app/dist ./dist
COPY --from=builder /app/node_modules ./node_modules
COPY package.json ./
ENV PORT=3000
EXPOSE 3000
USER node
HEALTHCHECK --interval=30s --timeout=3s --start-period=25s --retries=3 \
  CMD node -e "require('net').connect(3000,'127.0.0.1').on('connect',()=>process.exit(0)).on('error',()=>process.exit(1))"
CMD ["node", "dist/index.js"]
```

`USER node` maps to uid 1000, which matches the platform default `runAsUser`.
Build multi-arch the same way as any plugin image (see `publish-evenfire-plugin`).

### 3.1 Reference-server wrappers (no own code)

To ship an existing upstream MCP server, wrap it rather than rewrite it.

Node stdio server: a thin image that installs the upstream package with NO `CMD`,
plus a `stdio` `registry.json` whose `command[]` points at the installed
entrypoint under `/mcp-bin` (an init container copies `/app` and `/mcp-app` into
the shared `/mcp-bin` volume; the stdio-bridge sidecar then spawns it):

```dockerfile
FROM node:22-alpine
WORKDIR /app
RUN npm init -y >/dev/null 2>&1 && npm install @modelcontextprotocol/server-memory@<ver> && npm cache clean --force
USER node
# no CMD: the stdio-bridge sidecar launches the server (see registry.json command[])
```

```json
{
  "name": "mcp-memory",
  "version": "1.0.0",
  "description": "Knowledge-graph memory (upstream reference server, stdio via the bridge).",
  "category": "productivity",
  "serverMode": "local",
  "transport": "stdio",
  "command": ["node", "/mcp-bin/node_modules/@modelcontextprotocol/server-memory/dist/index.js"],
  "port": 3000,
  "tools": ["create_entities", "read_graph", "search_nodes"]
}
```

Python stdio server: the Node stdio-bridge cannot spawn a Python process, so ship
`supergateway` in the image to expose the upstream stdio server over
StreamableHTTP, and use `transport: streamableHttp` with no `command`.

## 4. registry.json (the authoring artifact)

The directory name, `registry.json` `.name`, the pushed image repo segment, and
the scoped catalog name MUST all be the same string. This is a hard identity
constraint: for a scoped `@org/name` server whose image is on the Evenfire
registry, control-api rejects a mismatch with a 422 at BOTH publish and install
(`imageRef repo ... must equal the entry name ... cross-org pull would be denied`),
and the collection's `publish.sh` fails even earlier, locally, on a `dir != name`
mismatch. The name must be a DNS-1123 label (`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`).
The npm `package.json` `.name` is unrelated and is NOT the catalog name.

Local own-code server:

```json
{
  "name": "mcp-example",
  "description": "Example MCP server: reads things from the Example API.",
  "category": "data",
  "tags": ["example", "api"],
  "transport": "streamableHttp",
  "port": 3000,
  "egressSummary": { "domains": ["api.example.com"], "ports": [443] },
  "tools": ["get_thing"],
  "credentialSchema": {
    "required": true,
    "authType": "api-key",
    "keys": [
      {
        "name": "EXAMPLE_API_KEY",
        "label": "Example API Key",
        "kind": "api-key",
        "semanticType": "plain-string",
        "description": "Get one at example.com/apikey."
      }
    ]
  }
}
```

Remote wrapper (no image, `.version` required here because there is no
`package.json`):

```json
{
  "name": "mcp-example-remote",
  "version": "1.0.0",
  "description": "Example (hosted): evenfire fronts the vendor MCP endpoint via nginx egress proxy. Keyless.",
  "category": "docs",
  "tags": ["example", "remote", "wrapper"],
  "serverMode": "remote",
  "transport": "streamableHttp",
  "port": 3000,
  "tools": ["resolve", "query"],
  "remoteEndpoints": [{ "url": "https://mcp.example.com/mcp" }],
  "docsUrl": "https://example.com",
  "egressSummary": { "domains": ["mcp.example.com"], "ports": [443] }
}
```

Field rules verified against the publisher and control-api:

- Local own-code: `serverMode` and `version` are optional (default `local`;
  version comes from `package.json`). Own-code servers use `transport:
  streamableHttp` and no `command`.
- Local stdio reference wrapper: no `src`/`package.json`; `.version` lives in
  `registry.json`; `transport: stdio` and a non-empty `command[]` array (spawned
  by the stdio-bridge sidecar). Omitting `command` on stdio, or adding `command`
  on a non-stdio transport, fails publish.
- Remote: `serverMode: remote`, `version` required, `remoteEndpoints: [{url}]`
  (HTTPS only, at least one), transport must NOT be `stdio`, optional
  `authHeaders: [{header, valueTemplate}]` (the `${VAR}` placeholder must match a
  `credentialSchema.keys[].name`). Remote may carry `authHeaders`; local may not.
- `egressSummary` has two shapes. Exact-host:
  `{ "domains": [hosts], "ports": [ints] }` (bare hostnames, no scheme/path/
  wildcard/IP). Public-web: `{ "wideCidr": true }` (ports default to 80/443 and
  may only be 80 or 443). Public-web is validated but collapses to a single
  `public-web` egress class — any `domains` you add are validated then ignored, so
  do not rely on them to scope it. Omit `egressSummary` entirely when the server
  has no external egress. Use public-web ONLY for genuinely dynamic public
  destinations (a CDN or a runtime-resolved host), not to dodge a finite host list.
- `credentialSchema` is `{ required, authType, keys: [{name, label?, kind?,
  semanticType?, description?}] }` or `null`/omitted for a keyless server. Only
  `keys[].name` is load-bearing (it must equal the env var the code reads or the
  `authHeaders` `${VAR}`). `authType` is free-form catalog metadata, not a
  validated enum, do not rely on a fixed vocabulary.
- `tools[]` is catalog metadata for discovery. Keep it in sync with the tools the
  server actually advertises (`mcp-smoke.sh`'s `tools/list` is the check). A
  server may also expose MCP resources and prompts, not only tools; the platform
  advertises whatever the server returns at runtime, `tools[]` is just the
  catalog listing.
- Do not author `transport: sse`. It exists in the CRD but is not a supported
  authoring path in the collection.

## 5. Test locally before publishing

Two levels, both runnable without a cluster:

1. Run the server and drive the MCP handshake against it. The bundled
   `scripts/mcp-smoke.sh` does `initialize` -> `tools/list` -> optional
   `tools/call` over StreamableHTTP and prints each response. Point it at a local
   `npm run dev` (`http://localhost:3000/mcp`), a running container, or any live
   remote endpoint:

   ```bash
   cd mcp-example && npm install && npm run dev &     # or: docker run -p 3000:3000 <image>
   ../scripts/mcp-smoke.sh http://localhost:3000/mcp
   # call a tool:
   ../scripts/mcp-smoke.sh http://localhost:3000/mcp get_thing '{"id":"42"}'
   ```

   A pass proves: reachable, `initialize` handshake succeeds, the tool is
   advertised, the tool runs, and the response shape is correct. For a remote
   wrapper, run it against the real `remoteEndpoints[0].url` to confirm the
   endpoint is live and the tool names in `registry.json` match reality.

2. Offline packaging check: the publisher's dry run prints the exact catalog
   payload and validates identity/egress/credential shape with no docker or
   network. See `publish-evenfire-plugin` for `publish.sh --dry-run <dir>`.

## 6. How the platform deploys it

`McpServer.spec.managed` defaults to `true` (immutable once set), so HCC owns the
runtime. By transport:

- `streamableHttp`: HCC runs the image directly as an `mcp-server` container,
  readiness = tcpSocket probe on the http port.
- `stdio`: HCC injects an init container that copies `/app` and `/mcp-app` into a
  shared `mcp-bin` emptyDir, plus a `stdio-bridge` sidecar fed
  `STDIO_COMMAND`/`STDIO_ARGS` and probed at `httpGet /health`.
- remote: HCC runs an nginx `egress-proxy` container that fronts
  `remoteEndpoints[0].url` (with `authHeaders` templated in); no vendor image.

An MCP server reads its `envSecret` from the namespace it runs in
(`mcp-server`). If a sibling in `sandbox-recipes` reads the same Secret, create
it in both namespaces (see `create-evenfire-plugin` section 4.2).

## 7. Attach it to a chat agent (Context)

The chat Host reads a `Context` (default `context1`); its `spec.mcpServers` is
the allowlist of McpServer names the agent may use. Add your server by appending
to that array. Do NOT `kubectl patch` the Context: some Contexts are WRC-owned
and a patch reverts, and control-api's update is a full replace. Read, append,
write back through control-api:

```bash
# 1. resolve the McpServer name (recipe-owned servers carry a workload label)
M=$(kubectl --context=<ctx> -n mcp-server get mcpserver \
      -l clerum.io/workload=<your-workload-id> -o jsonpath='{.items[0].metadata.name}')

# 2. append to context1.spec.mcpServers via the admin API (session cookie), then PUT the whole resource
#    control-api PUT /api/v1/admin/contexts/:name replaces the resource; a stale
#    resourceVersion returns 409 {"error":"conflict","reason":"resource_changed"}, so re-GET and retry.
```

Once the McpServer Deployment exists and is Ready, `mcp-host` picks up the
Context change automatically: it polls the Context mapper every 30s
(`CLERUM_CONTEXT_MAPPER_POLL_INTERVAL`, default 30000 ms) and reconciles the
connected fleet, so a pod restart is NOT required (it only forces the pickup to
happen immediately). Confirm it connected by grepping the mcp-host log for
`[McpManager] Added server: <name>` (a following `Removed server` means the
Context change reverted, usually a WRC-owned Context). The agent then sees the
tools as `<serverName>__<toolName>`.

## 8. Gotchas

- Directory name != `registry.json` `.name` != image repo segment -> rejected
  with a 422 at publish and install (and `publish.sh` fails locally first). The
  `package.json` name is a red herring.
- Binding `127.0.0.1` (not `0.0.0.0`) -> the tcpSocket probe never passes.
- Treating `/health` as the readiness gate for a local HTTP server -> the real
  gate is the TCP listener; a 200 on `/health` is neither necessary nor
  sufficient.
- `credentialSchema.keys[].name` not matching the env var the code reads (or the
  `authHeaders` `${VAR}`) -> operator secret is never wired; tools fail auth with
  no build/publish warning.
- Public-web `egressSummary` used to avoid listing a finite host set -> only
  correct for genuinely dynamic public destinations.
- `stdio` without a `command`, or `command` on non-stdio, or `authHeaders` on a
  local server, or `stdio` on a remote server -> all fail fast at publish.
- Expecting the CRD `transport: sse` to be a supported authoring path -> it is
  not; author `streamableHttp` (or `stdio` for reference wrappers).
