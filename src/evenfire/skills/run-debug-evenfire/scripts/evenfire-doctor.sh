#!/usr/bin/env bash
# Read-only health sweep of an Evenfire cluster. Never mutates anything.
# Usage: evenfire-doctor.sh <kube-context> [recipe-base-name]
set -euo pipefail

CTX="${1:-}"
RECIPE="${2:-}"
TIMEOUT="${KUBECTL_TIMEOUT:-15s}"

if [ -z "$CTX" ]; then
  echo "usage: evenfire-doctor.sh <kube-context> [recipe-base-name]" >&2
  exit 2
fi

# Only sweep known Evenfire clusters. Refuse anything else even for reads.
case "$CTX" in
  clerum-test|clerum-codex-*|clerum-detached-*|\
  gke_eventfire-491421_us-central1-a_clerum|\
  gke_eventfire-491421_us-central1-a_clerum-dev|\
  gke_eventfire-491421_us-central1-a_evenfire-hub) ;;
  *) echo "refusing: '$CTX' is not an allowed Evenfire context" >&2; exit 3 ;;
esac

K=(kubectl --context="$CTX" --request-timeout="$TIMEOUT")

echo "== context =="
"${K[@]}" config current-context >/dev/null 2>&1 || true
echo "$CTX"

echo
echo "== platform namespaces =="
for ns in control-plane mcp-host mcp-server sandbox-recipes sandbox-ui channels; do
  if "${K[@]}" get ns "$ns" -o name >/dev/null 2>&1; then
    echo "  ok   $ns"
  else
    echo "  MISS $ns"
  fi
done

echo
echo "== control-plane deployments =="
"${K[@]}" get deploy -n control-plane \
  -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,WANT:.spec.replicas' \
  --no-headers 2>/dev/null | while read -r name ready want; do
  ready="${ready:-0}"
  if [ "$ready" = "$want" ]; then echo "  ok   $name ($ready/$want)"; else echo "  !!   $name ($ready/$want)"; fi
done

if [ -n "$RECIPE" ]; then
  echo
  echo "== recipe: $RECIPE =="
  # The recipe object is not labeled with its base name; a registry install is
  # named recipe-<entry-slug>-v<ver>-<hash> and a hand-apply keeps metadata.name.
  # Match the base name as a hyphen-delimited token in the generated name.
  NAME="$("${K[@]}" get workflowrecipes -n sandbox-recipes -o name 2>/dev/null \
          | sed 's|.*/||' | grep -E "(^|-)${RECIPE}(-|$)" | head -1 || true)"
  if [ -z "$NAME" ]; then
    echo "  no recipe matching base name '$RECIPE' in sandbox-recipes"
  else
    PHASE="$("${K[@]}" get workflowrecipe "$NAME" -n sandbox-recipes \
              -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    echo "  name  $NAME"
    echo "  phase ${PHASE:-<none>}   (healthy = active)"
    echo "  conditions:"
    "${K[@]}" get workflowrecipe "$NAME" -n sandbox-recipes \
      -o jsonpath='{range .status.conditions[*]}    {.type}={.status} {.reason}{"\n"}{end}' 2>/dev/null || true
    echo "  non-Running pods:"
    FOUND=0
    for ns in sandbox-recipes sandbox-ui mcp-server; do
      while read -r pod phase; do
        [ -z "$pod" ] && continue
        if [ "$phase" != "Running" ] && [ "$phase" != "Succeeded" ]; then
          echo "    $ns/$pod: $phase"; FOUND=1
        fi
      done < <("${K[@]}" get pods -n "$ns" -l "clerum.io/recipe=$NAME" \
                 -o custom-columns='NAME:.metadata.name,PHASE:.status.phase' --no-headers 2>/dev/null || true)
    done
    [ "$FOUND" = 0 ] && echo "    (none: all pods Running/Succeeded)"
  fi
fi

echo
echo "done."
