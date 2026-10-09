#!/usr/bin/env bash
# Assemble and POST an Evenfire registry entry (recipe or mcp-server connector).
# --dry-run prints the exact JSON payload and exits without any network call.
#
# Auth: export REGISTRY_TOKEN=efrk_...   (a registry:publish org key)
# Usage:
#   publish-entry.sh --org acme --name my-plugin --version 1.0.0 \
#     --type recipe --category productivity --description "..." \
#     --recipe-file recipe/my-plugin.yaml --visibility private [--dry-run]
#
#   publish-entry.sh --org acme --name mcp-example --version 1.0.0 \
#     --type mcp-server --category data --description "..." \
#     --mcp-file mcpServer.json [--dry-run]
set -euo pipefail

REGISTRY_URL="${REGISTRY_URL:-https://registry.evenfire.ai}"
ORG="" NAME="" VERSION="" TYPE="" CATEGORY="" DESCRIPTION=""
AUTHOR="" ORIGIN="human-authored" VISIBILITY="private" TAGS="[]"
RECIPE_FILE="" MCP_FILE="" DRY_RUN=0

die() { echo "error: $*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --org) ORG="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --type) TYPE="$2"; shift 2 ;;
    --category) CATEGORY="$2"; shift 2 ;;
    --description) DESCRIPTION="$2"; shift 2 ;;
    --author) AUTHOR="$2"; shift 2 ;;
    --origin) ORIGIN="$2"; shift 2 ;;
    --visibility) VISIBILITY="$2"; shift 2 ;;
    --tags) TAGS="$2"; shift 2 ;;               # JSON array, e.g. '["a","b"]'
    --recipe-file) RECIPE_FILE="$2"; shift 2 ;;
    --mcp-file) MCP_FILE="$2"; shift 2 ;;       # JSON file holding the mcpServer block
    --registry-url) REGISTRY_URL="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown flag: $1" ;;
  esac
done

command -v jq >/dev/null 2>&1 || die "jq is required"
[ -n "$ORG" ] || die "--org is required"
[ -n "$NAME" ] || die "--name is required"
[ -n "$VERSION" ] || die "--version is required"
[ -n "$CATEGORY" ] || die "--category is required"
[ -n "$DESCRIPTION" ] || die "--description is required"
[ -n "$AUTHOR" ] || AUTHOR="$ORG"

case "$TYPE" in
  recipe)
    [ -n "$RECIPE_FILE" ] || die "--recipe-file is required for --type recipe"
    [ -f "$RECIPE_FILE" ] || die "recipe file not found: $RECIPE_FILE"
    ;;
  mcp-server)
    [ -n "$MCP_FILE" ] || die "--mcp-file (mcpServer block) is required for --type mcp-server"
    [ -f "$MCP_FILE" ] || die "mcp file not found: $MCP_FILE"
    jq -e . "$MCP_FILE" >/dev/null 2>&1 || die "mcp file is not valid JSON: $MCP_FILE"
    ;;
  *) die "--type must be 'recipe' or 'mcp-server' (there is no 'workflow' type)" ;;
esac
case "$VISIBILITY" in public|private) ;; *) die "--visibility must be public or private" ;; esac
case "$ORIGIN" in human-authored|agent-generated|community) ;; *) die "--origin must be human-authored|agent-generated|community" ;; esac

PAYLOAD=$(jq -n \
  --arg name "@${ORG}/${NAME}" \
  --arg version "$VERSION" \
  --arg entryType "$TYPE" \
  --arg description "$DESCRIPTION" \
  --arg author "$AUTHOR" \
  --arg origin "$ORIGIN" \
  --arg category "$CATEGORY" \
  --arg visibility "$VISIBILITY" \
  --argjson tags "$TAGS" \
  '{
     name: $name, version: $version, entryType: $entryType,
     description: $description, author: $author, origin: $origin,
     category: $category, contentCreatorTag: "community",
     configCreatorTag: "community", visibility: $visibility, tags: $tags
   }')

if [ "$TYPE" = "recipe" ]; then
  PAYLOAD=$(jq --rawfile recipe "$RECIPE_FILE" '. + { recipe: $recipe }' <<<"$PAYLOAD")
else
  PAYLOAD=$(jq --slurpfile mcp "$MCP_FILE" '. + { mcpServer: $mcp[0] }' <<<"$PAYLOAD")
fi

if [ "$DRY_RUN" = 1 ]; then
  echo "$PAYLOAD" | jq .
  echo "# dry run: would POST to ${REGISTRY_URL}/api/v1/entries" >&2
  exit 0
fi

[ -n "${REGISTRY_TOKEN:-}" ] || die "REGISTRY_TOKEN (efrk_ publish key) must be set"

HTTP=$(curl -sS -o /tmp/publish-entry-resp.$$ -w '%{http_code}' \
  -X POST "${REGISTRY_URL}/api/v1/entries" \
  -H "Authorization: Bearer ${REGISTRY_TOKEN}" \
  -H 'content-type: application/json' \
  --data "$PAYLOAD")
echo "HTTP $HTTP"
jq . /tmp/publish-entry-resp.$$ 2>/dev/null || cat /tmp/publish-entry-resp.$$
rm -f /tmp/publish-entry-resp.$$
case "$HTTP" in
  201) echo "published ${ORG}/${NAME}@${VERSION}" ;;
  409) echo "conflict: ${NAME}@${VERSION} already exists; bump the version" >&2; exit 1 ;;
  *) echo "publish failed (HTTP $HTTP)" >&2; exit 1 ;;
esac
