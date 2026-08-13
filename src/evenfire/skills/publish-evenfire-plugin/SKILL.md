---
name: publish-evenfire-plugin
description: >-
  Publish an Evenfire recipe (plugin) or an MCP-server connector to the org
  registry, keep it private to the org, install it on a cluster, and manage its
  versions. Covers minting an efrk_ org key, pushing multi-arch images to the org
  image host, the exact POST /api/v1/entries payload (entryType recipe vs
  mcp-server), the mcpServer/credentialSchema/egressSummary blocks, the
  imageRef-equals-entry-name rule, private visibility, installing via control-api,
  pull-secret behavior, granting users, and version retirement. Use whenever the
  task is to publish, release, version, retire, or org-install a plugin/connector.
---

# Publishing to the Evenfire org registry

> **Verified against Evenfire `dev` at commit `f9e8d0487`.**
> CRD API `clerum.io/v1alpha1`, `clerum-crds` chart `0.8.0`. Field names, enums,
> endpoints, and commands below match that revision.

Publishing has two halves that must both be true at install time:

1. The container image(s) are pushed to a registry the installing cluster can
   pull (a public registry for a public plugin, or `registry.evenfire.ai/<org>`
   for an org-private one).
2. The catalog entry (the recipe YAML or the connector metadata) is POSTed to the
   registry so every cluster in the org can discover and install it.

The recipe YAML is the published artifact. Images and Secrets are NOT inside it:
Kubernetes pulls images at install time, and the installing operator provisions
Secrets out of band from your `spec.description`.

Author recipes with `create-evenfire-plugin` and MCP servers with
`create-evenfire-mcp-server` before publishing.

## 1. Two publish surfaces

| Surface | Auth | Who | Name scoping |
|---|---|---|---|
| control-api `POST /api/v1/admin/registry/entries` | admin control-ui session | an operator in the browser or an admin script | server auto-applies the `@<org>/` scope |
| raw `POST https://registry.evenfire.ai/api/v1/entries` | `Authorization: Bearer efrk_...` | CI, scripts, an agent | YOU must supply `@<org>/<name>` |

Both forward the same JSON body. The Control UI publish form defaults to
`entryType: mcp-server` but supports both — a segmented control switches it to
`recipe` (open it with `?type=recipe`), which publishes the recipe YAML. Use the
raw `efrk_` API for anything scripted.

## 2. Mint an efrk_ org key (one time)

An `efrk_` key is minted by an admin via control-api
`POST /api/v1/admin/registry/keys` (owner-gated: it requires an active admin
session and can only mint for that admin's own org). An agent holding only an
`efrk_` bearer CANNOT mint a key. Scopes: `registry:read`, `registry:publish`,
`registry:update`, `registry:delete` (default selection: read + publish +
update). A `registry:publish` key doubles as the image push credential.

Resolve your org scope (and validate the key) with `GET /api/v1/whoami`, which is
org-agnostic, so scripts do not hardcode the org:

```bash
curl -sS -H "Authorization: Bearer $REGISTRY_TOKEN" \
  https://registry.evenfire.ai/api/v1/whoami | jq -r '.orgName'
```

Use the returned org as the `@<org>/` prefix on every entry name. Keep the key
out of chat, logs, and committed files, provision it to a runtime via an env var
(`REGISTRY_TOKEN`), never inline. If a key is ever exposed, rotate it.

## 3. Push the image(s) first

An entry whose image is not yet pushed installs into `ImagePullBackOff`. Build
multi-arch and push before publishing:

```bash
docker login registry.evenfire.ai -u _ -p "$REGISTRY_TOKEN"   # publish key is the push cred
docker buildx create --use --name multiarch                   # one-time
docker buildx build --platform linux/amd64,linux/arm64 \
  --label org.opencontainers.image.source=https://github.com/<org>/<repo> \
  --label org.opencontainers.image.licenses=Apache-2.0 \
  -t registry.evenfire.ai/<org>/<name>:1.0.0 --push .
```

Verify each image before publishing:

- Public (for a public plugin): `docker logout && docker pull <image>` succeeds
  from a clean shell (a successful push does NOT prove the repo is public).
- Multi-arch: `docker buildx imagetools inspect <image>` lists `linux/amd64` and
  `linux/arm64`.
- Non-root: `docker run --rm <image> id` shows uid != 0.
- Listens on the declared port and its health path returns 2xx.
- No baked-in secrets (`docker history <image>`), OCI source + license labels present.

A recipe plugin with several images (for example `ui` + `api` + `mcp`) pushes
each image; the recipe YAML references them by their pinned tags. Push all of
them before publishing the entry.

**imageRef repo must equal the scoped entry name.** This rule applies to an
mcp-server CONNECTOR entry's `mcpServer.imageRef`, not to the individual workload
images inside a recipe. For an evenfire-hosted local connector, the image repo
path `<org>/<name>` must equal the entry's scoped name `@<org>/<name>` (minus the
`@`). A mismatch is a hard 422 at BOTH publish and install (`cross-org pull would
be denied`, which surfaces as a silent ImagePullBackOff). Entry `@acme/db`
requires image `registry.evenfire.ai/acme/db:<tag>`.

## 4. The publish payload

`POST {registry}/api/v1/entries`, `Content-Type: application/json`, `Authorization: Bearer efrk_...`

Required fields: `name`, `version`, `entryType`, `description`, `author`,
`origin`, `category`, `contentCreatorTag`, `configCreatorTag`. Plus `recipe` (the
recipe document as a YAML/JSON string, <= 100 KB) for a recipe entry, or an
`mcpServer` block for a connector. Optional: `tags`, `visibility`.

- `entryType`: `recipe | mcp-server`. There is **no** `workflow` entryType. A
  workflow recipe is published as `entryType: recipe`; the registry derives the
  read-only subtype `recipe_type: workflow | only-workloads` itself.
- `name`: `@<org>/<name>`. A bare name is `400 scope_required`. `@clerum` and
  `@evenfire` are curator-only. The `<name>` segment MUST equal the recipe's
  `metadata.name` (mismatch is `400 INVALID_INPUT`). control-api's admin path
  auto-scopes; the raw path does not, so add `@<org>/` yourself.
- `visibility`: `public | private`. `private` surfaces the entry only to the
  org's own clusters/members and hides it from the default catalog. To read it
  back over the API you must pass `?visibility=all`.
- `origin`: `human-authored | agent-generated | community`.
- `contentCreatorTag` / `configCreatorTag`: `community` (or `1st-party` for a
  curator-published first-party entry).

Recipe entry (JSON body):

```json
{
  "name": "@<org>/my-plugin",
  "version": "1.0.0",
  "entryType": "recipe",
  "description": "One-sentence summary, then the Secret prerequisites.",
  "author": "<org>",
  "origin": "human-authored",
  "category": "productivity",
  "contentCreatorTag": "community",
  "configCreatorTag": "community",
  "visibility": "private",
  "tags": ["tasks"],
  "recipe": "<the recipe YAML as a string>"
}
```

MCP-server connector entry, `mcpServer` block instead of `recipe`:

- Local: `{ "serverMode": "local", "imageRef": "registry.evenfire.ai/<org>/<name>:<tag>", "transport": "streamableHttp", "port": 3000, "egressSummary": {...}, "credentialSchema": {...} }`
- Remote: `{ "serverMode": "remote", "remoteEndpoints": [{ "url": "https://mcp.example.com/mcp" }], "credentialSchema": {...} }`

The remote target field is `remoteEndpoints` (an array of `{url}`), NOT
`remoteBaseUrl` or a scalar `remoteEndpoint`. `egressSummary` is
`{ domains: string[], ports: number[], wideCidr: boolean }` (max 20 domain x port
bindings; `wideCidr: true` is the public-web signal). `credentialSchema` is
`{ required, authType, keys: [{name, label, kind, semanticType?, description?}] }`;
only `keys[].name` is load-bearing.

Response: `201` created, `409` the name+version already exists (a published
name+version is immutable, bump the version), other = failure.

The bundled `scripts/publish-entry.sh` assembles this payload from flags/files
and supports `--dry-run` to print the exact JSON without pushing.

## 5. Install and verify

Install through the Control UI Marketplace (recommended) or the control-api admin
endpoints:

- MCP-server connector: `POST /api/v1/admin/registry/install`. control-api
  attaches `spec.imagePullSecrets: [{name: evenfire-registry-pull}]` (by name) for
  a local-mode entry whose image is on the org registry. The
  `evenfire-registry-pull` Secret itself is provisioned by the platform, not
  created by the install call.
- Recipe: `POST /api/v1/admin/registry/install-recipe`. This does NOT write
  `imagePullSecrets` into the recipe spec, and nothing auto-attaches one to recipe
  workloads (WRC only propagates an `imagePullSecrets` already present in the
  recipe). A private image in a recipe therefore gets no pull secret, so either use
  publicly pullable images or have the operator pre-create a recipe-owned Secret
  and set `workloads[].imagePullSecrets`.
- Direct recipe create (CI, bypasses the marketplace): `POST /api/v1/admin/recipes`.

Verify a private entry published (it is hidden from the default catalog):

```bash
curl -s -H "Authorization: Bearer $REGISTRY_TOKEN" \
  "https://registry.evenfire.ai/api/v1/entries?q=<name>&visibility=all" | jq '.entries[].name'
```

After install, confirm the recipe reaches `.status.phase: active` and real pods
are Running (see `run-debug-evenfire`), then grant users on the recipe's Members
tab, the entry is invisible to end users until granted.

## 6. Version lifecycle

- Same `name`+`version` re-publish returns `409`; a published version is
  permanent. Bump the version (and the image tag) to ship a change.
- Edit an existing version's metadata in place (description, tags, visibility,
  and for a connector `mcpServer.egressSummary`): `PUT` on the version, raw path
  `PUT .../entries/@<org>%2F<name>/versions/<version>`.
- Remove a version (soft delete): `DELETE .../entries/@<org>%2F<name>/versions/<version>`.
- There is **no** `PATCH {status}` endpoint. The entry `status`
  (`published | deprecated | removed`) is read-only display; you cannot set it
  from the client. To retire a version, DELETE it.

Versioning discipline for images: patch bump for a rebuild with no schema change,
minor for a new optional workload/oauthClient/broadened egress, major for a
removed workload, changed port, or a newly required Secret. Never re-push an
unchanged tag with new bits; installed clusters will not re-pull.

## 7. Gotchas

- `entryType: "workflow"` -> not a value. Use `recipe` (the workflow-ness is a
  derived read-side subtype).
- `PATCH /entries/<name> {status: deprecated}` -> that endpoint does not exist.
  Use `DELETE .../versions/<version>`.
- Bare `name`, or a `@<org>/<name>` whose `<name>` != `metadata.name` -> `400`.
- Image repo path != scoped entry name -> `422` at publish and install.
- Expecting the Control UI publish form to only do connectors -> it defaults to
  `mcp-server` but also publishes recipes (switch the segmented control, or open
  it with `?type=recipe`).
- Expecting a `private` entry in the default catalog -> query with
  `?visibility=all`.
- Recipe with a private third-party image expecting an auto pull secret -> only
  mcp-server installs auto-attach one; prefer public images for recipes.
- The efrk_ key in chat/logs/commits -> keep it in env only; rotate if exposed.
