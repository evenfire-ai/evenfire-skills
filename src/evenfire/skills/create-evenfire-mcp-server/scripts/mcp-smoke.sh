#!/usr/bin/env bash
# Drive the MCP StreamableHTTP handshake against a server URL and print each
# response: initialize -> tools/list -> optional tools/call. No cluster needed.
# Works against a local `npm run dev`, a running container, or a live remote endpoint.
#
# Usage:
#   mcp-smoke.sh <url> [tool-name] [json-args]
# Examples:
#   mcp-smoke.sh http://localhost:3000/mcp
#   mcp-smoke.sh https://mcp.context7.com/mcp
#   mcp-smoke.sh http://localhost:3000/mcp get_thing '{"id":"42"}'
set -euo pipefail

URL="${1:-}"
TOOL="${2:-}"
ARGS="${3:-{}}"
PROTO="${MCP_PROTOCOL_VERSION:-2025-03-26}"

if [ -z "$URL" ]; then
  echo "usage: mcp-smoke.sh <url> [tool-name] [json-args]" >&2
  exit 2
fi
command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 2; }

HDRS="$(mktemp)"
trap 'rm -f "$HDRS"' EXIT

# StreamableHTTP responses may be application/json or text/event-stream.
# Extract the JSON-RPC object from either form.
extract_json() {
  # StreamableHTTP replies are text/event-stream ('data: {json}') or plain JSON.
  # Slurp once, then emit the JSON either way.
  local body; body="$(cat)"
  if printf '%s\n' "$body" | grep -q '^data: '; then
    printf '%s\n' "$body" | sed -n 's/^data: //p'
  else
    printf '%s\n' "$body"
  fi
}

rpc() {
  # $1 = JSON body, uses $SID if set. Writes response headers to $HDRS.
  local body="$1"
  local -a auth=()
  [ -n "${SID:-}" ] && auth=(-H "mcp-session-id: $SID")
  curl -sS -D "$HDRS" \
    -H 'content-type: application/json' \
    -H 'accept: application/json, text/event-stream' \
    ${auth[@]+"${auth[@]}"} \
    -X POST "$URL" --data "$body"
}

echo "== initialize =="
INIT_BODY=$(jq -cn --arg p "$PROTO" '{
  jsonrpc:"2.0", id:1, method:"initialize",
  params:{ protocolVersion:$p, capabilities:{}, clientInfo:{name:"mcp-smoke", version:"1.0.0"} }
}')
INIT_RAW="$(rpc "$INIT_BODY")"
# A stateful server returns an mcp-session-id header; a stateless one does not.
SID="$(grep -i '^mcp-session-id:' "$HDRS" 2>/dev/null | head -1 | tr -d '\r' | awk '{print $2}' || true)"
echo "$INIT_RAW" | extract_json | jq '{serverInfo:.result.serverInfo, protocolVersion:.result.protocolVersion, error:.error}' 2>/dev/null \
  || { echo "initialize did not return a JSON-RPC result:"; echo "$INIT_RAW"; exit 1; }
[ -n "$SID" ] && echo "session: $SID"

# Notify initialized (best-effort; some servers require it before tools/list).
rpc '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1 || true

echo
echo "== tools/list =="
LIST_RAW="$(rpc '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')"
echo "$LIST_RAW" | extract_json | jq -r '.result.tools[]?.name // empty' 2>/dev/null \
  || { echo "tools/list did not return tools:"; echo "$LIST_RAW"; exit 1; }

if [ -n "$TOOL" ]; then
  echo
  echo "== tools/call: $TOOL =="
  CALL_BODY=$(jq -cn --arg n "$TOOL" --argjson a "$ARGS" '{
    jsonrpc:"2.0", id:3, method:"tools/call", params:{ name:$n, arguments:$a }
  }')
  rpc "$CALL_BODY" | extract_json | jq '{isError:.result.isError, content:.result.content, error:.error}' 2>/dev/null \
    || echo "tools/call returned a non-JSON response"
fi

echo
echo "ok."
