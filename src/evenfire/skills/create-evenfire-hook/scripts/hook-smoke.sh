#!/usr/bin/env bash
# Drive the mcp-host /v1 hook contract against a running hook server and print
# each response. No cluster needed — point it at a local `node server.js` (or a
# running container) on the port your hook listens on.
#
# It POSTs a realistic body for a lifecycle point to {base}{path}/v1/{point} and
# prints the HTTP status + JSON. `all` drives every LLM-lane point in sequence.
#
# Usage:
#   hook-smoke.sh <base-url> [point] [path-prefix]
#     point : pre_call | moderate | post_call | on_error | pre_tool_use | post_tool_use | all
#             (default: pre_call)
#     path-prefix : spec.path if not "/" (e.g. /a/chatllm) — default empty
# Examples:
#   hook-smoke.sh http://localhost:8080
#   hook-smoke.sh http://localhost:8080 all
#   hook-smoke.sh http://localhost:8080 post_tool_use /a/chatllm
set -euo pipefail

BASE="${1:-}"
POINT="${2:-pre_call}"
PREFIX="${3:-}"

if [ -z "$BASE" ]; then
  echo "usage: hook-smoke.sh <base-url> [point] [path-prefix]" >&2
  exit 2
fi
command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 2; }
BASE="${BASE%/}"          # trim trailing slash
PREFIX="${PREFIX%/}"      # trim trailing slash

# Realistic body per point, matching the /v1 contract (see references/v1-contract.md).
body_for() {
  case "$1" in
    pre_call|moderate)
      jq -cn '{
        messages: [
          { role:"user", content:"78 words of prose wrapped around a paste, ending in a question." },
          { role:"assistant", content:[
            { type:"text", text:"the block form" },
            { type:"tool_use", id:"toolu_01", name:"search", input:{ q:"orders" } } ] },
          { role:"user", content:[
            { type:"tool_result", tool_use_id:"toolu_01", content:"id,amount\n1,10\n2,20\n" } ] }
        ],
        tools: [ { name:"search", description:"search", input_schema:{} } ],
        max_tokens: 4096, temperature: 0.2
      }' ;;
    post_call)
      jq -cn '{
        response: { content:"the answer", tool_calls:[ { id:"toolu_02", name:"get_issue", arguments:{ number:7 } } ], finish_reason:"tool_use" },
        usage: { input_tokens: 1200, output_tokens: 40 }
      }' ;;
    on_error)
      jq -cn '{
        request: { messages:[ { role:"user", content:"do the thing" } ], tools:[] },
        error: { code:"overloaded", message:"provider busy", retryable:true }
      }' ;;
    pre_tool_use)
      jq -cn '{ tool: { provenance:"mcp", server:"github", name:"get_issue" }, arguments:{ number:7, repo:"acme/app" } }' ;;
    post_tool_use)
      jq -cn '{ tool: { provenance:"mcp", server:"github", name:"get_issue" }, arguments:{ number:7 }, result:{ content:"issue body", is_error:false } }' ;;
    *) echo "unknown point: $1" >&2; return 2 ;;
  esac
}

drive() {
  local point="$1"
  local url="${BASE}${PREFIX}/v1/${point}"
  local body; body="$(body_for "$point")" || return 2
  echo "== POST ${PREFIX}/v1/${point} =="
  local out status
  out="$(curl -sS -o /dev/null -w '%{http_code}' -H 'content-type: application/json' -X POST "$url" --data "$body" || echo 000)"
  status="$out"
  # Re-run to capture the body (kept separate so a non-2xx status is still visible).
  local resp; resp="$(curl -sS -H 'content-type: application/json' -X POST "$url" --data "$body" || true)"
  echo "HTTP $status"
  if printf '%s' "$resp" | jq . >/dev/null 2>&1; then
    printf '%s' "$resp" | jq .
  else
    printf '%s\n' "$resp"
  fi
  # Contract sanity: pre_call must answer an {action} object on every path.
  if [ "$point" = "pre_call" ]; then
    printf '%s' "$resp" | jq -e 'has("action")' >/dev/null 2>&1 \
      || echo "WARN: pre_call response has no 'action' — {} is not a valid pre_call action" >&2
  fi
  echo
}

echo "== GET ${PREFIX}/healthz =="
curl -sS "${BASE}${PREFIX}/healthz" || true
echo; echo

if [ "$POINT" = "all" ]; then
  for p in pre_call moderate post_call on_error; do drive "$p"; done
else
  drive "$POINT"
fi

echo "ok."
