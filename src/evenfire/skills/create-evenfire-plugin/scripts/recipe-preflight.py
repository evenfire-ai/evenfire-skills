#!/usr/bin/env python3
"""Check an Evenfire WorkflowRecipe for problems that CRD admission cannot see.

The CRD schema and CEL rules are checked by `kubectl-validate` or
`kubectl apply --dry-run=server`; run one of those too. This script mirrors the
checks that run later (the cluster admission policy, Control API at install,
and the recipe controller on reconcile) plus pitfalls that fail silently, as of
evenfire-ai/evenfire dev 0b26101eb.

Usage:   python3 recipe-preflight.py recipe.yaml [more.yaml ...]
Needs:   PyYAML (python3 -m pip install pyyaml)
Exit:    0 no errors (warnings allowed), 1 errors found, 2 unreadable input.
"""

from __future__ import annotations

import re
import sys
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("recipe-preflight: PyYAML is required (python3 -m pip install pyyaml)\n")
    sys.exit(2)

RFC1123_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
TEMPLATE = re.compile(r"\{\{([^{}]*)\}\}")
INCLUDE_WHEN = re.compile(r"^\{\{\s*inputs\.(\w+)\s*\}\}$")
CLUSTER_LOCAL_HOST = re.compile(
    r"([a-z0-9](?:[-a-z0-9]*[a-z0-9])?\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?\.svc\.cluster\.local\.?)(?::([0-9]{1,5}))?",
    re.I,
)
HOST_PORT_TEMPLATE = re.compile(
    r"\{\{\s*([a-z][a-z0-9-]*)\s*:\s*host\s*\}\}:(?:\{\{\s*([a-z][a-z0-9-]*)\s*:\s*port\s*\}\}|([0-9]{1,5}))"
)
DNS_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
SENSITIVE_ENV_NAME = re.compile(r"(PASSWORD|TOKEN|SECRET|API_KEY|CREDENTIAL|PRIVATE_KEY)", re.I)
SENSITIVE_INPUT_NAME = re.compile(
    r"(password|passwd|pwd|token|secret|credential|api[_-]?key|apikey|private[_-]?key|privatekey)", re.I
)
JWT_LIKE = re.compile(r"^[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}$")
SECRET_LIKE = re.compile(r"^(sk-[A-Za-z0-9_-]{8,}|pat[A-Za-z0-9_-]{8,})$")
URL_WITH_PASSWORD = re.compile(r"^[a-z][a-z0-9+.-]*://[^/\s:@]+:([^@\s]+)@", re.I)
INPUT_TEMPLATE_ONLY = re.compile(r"^\{\{\s*inputs\.[A-Za-z0-9_.-]+\s*\}\}$")
INPUT_REF = re.compile(r"\{\{\s*inputs\.([A-Za-z0-9_.-]+)\s*\}\}")
BLOCKED_TEMPLATE_KEYS = {
    "__proto__", "constructor", "prototype", "__defineGetter__", "__defineSetter__",
    "__lookupGetter__", "__lookupSetter__",
}
ALLOWED_CAPABILITIES = {"CHOWN", "FOWNER", "DAC_OVERRIDE", "NET_BIND_SERVICE"}
OFFLINE_SCOPE = {"salesforce": "refresh_token", "microsoft-graph": "offline_access"}
MAX_RUN_DURATION_SECONDS = 86400
RESERVED_SECRET = "evenfire-registry-pull"
TEAM_LABEL = "clerum.io/workflow-team-id"
NON_PUBLIC_SUFFIXES = (".localhost", ".local", ".internal", ".svc", ".cluster.local")


class Report:
    def __init__(self, source: str) -> None:
        self.source = source
        self.items: list[tuple[str, str, str]] = []

    def error(self, where: str, message: str) -> None:
        self.items.append(("ERROR", where, message))

    def warn(self, where: str, message: str) -> None:
        self.items.append(("WARN", where, message))

    def info(self, where: str, message: str) -> None:
        self.items.append(("INFO", where, message))

    @property
    def errors(self) -> int:
        return sum(1 for level, _, _ in self.items if level == "ERROR")


def as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def namespace_of(workload: dict, ui_id: str | None) -> str:
    if workload.get("transport"):
        return "mcp-server"
    if ui_id and workload.get("id") == ui_id:
        return "sandbox-ui"
    return "sandbox-recipes"


def string_fields(workload: dict) -> list[tuple[str, str]]:
    """The fields WRC resolves templates in: env[].value, command[], args[]."""
    fields: list[tuple[str, str]] = []
    for i, env in enumerate(as_list(workload.get("env"))):
        env = as_dict(env)
        if isinstance(env.get("value"), str):
            fields.append((f"env[{env.get('name', i)}].value", env["value"]))
    for key in ("command", "args"):
        for i, value in enumerate(as_list(workload.get(key))):
            if isinstance(value, str):
                fields.append((f"{key}[{i}]", value))
    return fields


def resolved_input_names(spec: dict) -> set[str]:
    names = set(as_dict(spec.get("inputs")).keys())
    for key, definition in as_dict(as_dict(spec.get("inputContract")).get("properties")).items():
        if isinstance(definition, dict) and "default" in definition:
            names.add(key)
    profile = as_dict(as_dict(spec.get("profiles")).get(spec.get("activeProfile") or ""))
    names.update(profile.keys())
    names.update(as_dict(c).get("name") for c in as_list(spec.get("computed")) if as_dict(c).get("name"))
    return names


def check_metadata(doc: dict, spec: dict, r: Report) -> None:
    meta = as_dict(doc.get("metadata"))
    name = meta.get("name")
    if not isinstance(name, str) or not RFC1123_LABEL.match(name):
        r.error("metadata.name", "must be a lowercase RFC 1123 label, at most 63 characters (Control API)")
    ns = meta.get("namespace")
    if ns is not None and ns != "sandbox-recipes":
        r.error("metadata.namespace", "WorkflowRecipes must live in sandbox-recipes (admission policy); leave it out")
    if meta.get("ownerReferences"):
        r.error("metadata.ownerReferences", "only the recipe controller may set ownerReferences (admission policy)")
    extra_labels = [k for k in as_dict(meta.get("labels")) if k != TEAM_LABEL]
    if extra_labels or meta.get("annotations"):
        r.info("metadata", "a registry install keeps none of your labels or annotations")


def check_workflow_fields(doc: dict, spec: dict, r: Report) -> None:
    steps = as_list(spec.get("steps"))
    triggers = as_dict(spec.get("triggers"))
    workloads = [as_dict(w) for w in as_list(spec.get("workloads"))]
    if not workloads and not steps:
        r.error("spec", "a recipe needs at least one workload or step")
    if not steps:
        return
    if "onDemand" not in triggers and "schedule" not in triggers:
        r.error("spec.triggers", "a recipe with steps must declare triggers.onDemand or triggers.schedule (admission policy, Control API)")
    if spec.get("webhooks"):
        r.error("spec.webhooks", "recipes with steps never get the webhook gateway; these URLs will not work")
    long_running = [w.get("id") for w in workloads if w.get("type") in ("deployment", "statefulset", "daemonset")]
    if long_running and not spec.get("pluginWorkloadSdk"):
        r.warn("spec.steps", "once active, WRC no longer re-applies workloads of a recipe with steps; image or env changes to "
               f"{', '.join(map(str, long_running))} will not roll out")
    if any(w.get("includeWhen") for w in workloads):
        r.warn("spec.workloads[].includeWhen", "ignored on recipes with steps; every workload is deployed")
    if any(as_dict(s).get("run") for s in steps):
        r.info("spec.steps[].run", "snippet steps need the operator's snippet runtime (WRC_ENABLE_SNIPPET_RUNTIME)")
    if "schedule" in triggers or spec.get("scheduling"):
        team = as_dict(as_dict(doc.get("metadata")).get("labels")).get(TEAM_LABEL)
        if not team:
            r.error(f"metadata.labels.{TEAM_LABEL}", "scheduled recipes need the owning team's UUID here; a registry install drops it")
        elif not UUID.match(str(team)):
            r.error(f"metadata.labels.{TEAM_LABEL}", "must be a team UUID")
    retention = spec.get("runRetention")
    if isinstance(retention, dict):
        limit = retention.get("maxRunDurationSeconds")
        if limit is None or (isinstance(limit, int) and limit > MAX_RUN_DURATION_SECONDS):
            r.error("spec.runRetention.maxRunDurationSeconds",
                    f"set it to {MAX_RUN_DURATION_SECONDS} or less; the CRD default (604800) exceeds the controller's ceiling")


def check_secret_name(name: Any, where: str, r: Report) -> None:
    if isinstance(name, str) and (name.startswith("wf-") or name == RESERVED_SECRET):
        r.error(where, f'"{name}" is reserved for the platform (Control API rejects wf-* and {RESERVED_SECRET})')


def check_egress(w: dict, where: str, ns: str, ids: set[str], ui_id: str | None, r: Report) -> None:
    bindings = as_list(w.get("egressBindings"))
    if not bindings:
        return
    if ui_id and w.get("id") == ui_id:
        r.warn(where, "egressBindings on the UI workload are ignored; use spec.ui.egress")
        return
    if len(bindings) > 20:
        r.error(where, "at most 20 egressBindings")
    for i, b in enumerate(bindings):
        b = as_dict(b)
        at = f"{where}[{i}]"
        if "cidr" in b:
            r.error(at, "cidr is not allowed; use exact-host dns entries")
        klass = b.get("egressClass", "exact-host")
        if klass == "public-web":
            if not w.get("transport"):
                r.error(at, "public-web is only supported on MCP transport workloads; list each host as exact-host")
            if any(k in b for k in ("dns", "port", "protocol")):
                r.error(at, "public-web entries must not declare dns, port or protocol")
            continue
        if klass != "exact-host":
            r.error(at, "egressClass must be exact-host or public-web")
            continue
        port = b.get("port")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            r.error(at, "port is required (1-65535)")
        dns = b.get("dns")
        if not isinstance(dns, str) or not dns.strip():
            r.error(at, "dns is required")
            continue
        if dns != dns.lower() or "/" in dns or ":" in dns or dns == "*" or dns.startswith("*."):
            r.error(at, "dns must be a lowercase host name, without wildcard, port, path or scheme")
            continue
        if dns.rstrip(".").endswith(".svc.cluster.local"):
            parts = dns.rstrip(".")[: -len(".svc.cluster.local")].split(".")
            if w.get("transport"):
                r.error(at, "cluster-local egressBindings are only supported on non-MCP workloads")
            elif len(parts) != 2 or parts[0] not in ids:
                r.error(at, "a cluster-local dns must be <workload id>.<namespace>.svc.cluster.local of a workload in this recipe")
            elif parts[1] != ns:
                r.error(at, f"the target namespace must be the source workload's own namespace ({ns})")
            continue
        if re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3}){3}", dns):
            r.error(at, "IP addresses are not accepted; use the service's DNS name (Control API, WRC)")
        elif ("." not in dns or dns in ("localhost", "metadata.goog", "kubernetes.default")
                or dns.endswith(NON_PUBLIC_SUFFIXES)):
            r.error(at, "dns must be a public DNS host name")
        elif not all(DNS_LABEL.match(label) for label in dns.split(".")):
            r.error(at, "dns must be a valid DNS host name")
        else:
            r.info(at, f"{dns} must have public IPv4 (A) records when WRC reconciles, or the recipe fails")


def check_env_secrets(w: dict, where: str, r: Report) -> None:
    for i, env in enumerate(as_list(w.get("env"))):
        env = as_dict(env)
        name = str(env.get("name", ""))
        value = env.get("value")
        if value in (None, ""):
            continue
        text = value.strip() if isinstance(value, str) else str(value)
        url_password = URL_WITH_PASSWORD.match(text)
        if (JWT_LIKE.match(text) or SECRET_LIKE.match(text) or "-----BEGIN " in text
                or (url_password and not INPUT_TEMPLATE_ONLY.match(url_password.group(1)))):
            r.error(f"{where}.env[{name or i}]", "the value looks like a secret; move it to envSecret (workflowInlineSecretEnv)")
            continue
        sensitive_input = any(SENSITIVE_INPUT_NAME.search(m) for m in INPUT_REF.findall(text))
        if SENSITIVE_ENV_NAME.search(name) and not sensitive_input:
            r.error(f"{where}.env[{name}]", "Control API rejects env names containing PASSWORD, TOKEN, SECRET, API_KEY, "
                    "CREDENTIAL or PRIVATE_KEY with a literal value; use envSecret or rename (workflowInlineSecretEnv)")


def check_templates(spec: dict, workloads: list[dict], ui_id: str | None, r: Report) -> None:
    by_id = {w.get("id"): w for w in workloads}
    with_port = {wid for wid, w in by_id.items() if w.get("port")}
    inputs = resolved_input_names(spec)
    computed = {as_dict(c).get("name") for c in as_list(spec.get("computed"))}
    resource_keys = {
        as_dict(res).get("id"): set(as_dict(as_dict(res).get("data")).keys())
        for res in as_list(spec.get("resources"))
        if as_dict(res).get("type") in ("secret", "configmap")
    }
    for idx, w in enumerate(workloads):
        source_is_ui = bool(ui_id and w.get("id") == ui_id)
        source_is_stdio = as_dict(w.get("transport")).get("type") == "stdio"
        for field, value in string_fields(w):
            at = f"spec.workloads[{idx}].{field}"
            for body in TEMPLATE.findall(value):
                ref = body.strip()
                parts = re.split(r"[.:]", ref)
                if any(p in BLOCKED_TEMPLATE_KEYS for p in parts):
                    r.error(at, f'template injection blocked: "{ref}"')
                    continue
                if ref.startswith("inputs."):
                    if ref[len("inputs."):] not in inputs:
                        r.error(at, f'unresolved template reference "{ref}" (no input, default, profile or computed value)')
                    continue
                if ref.startswith("computed."):
                    if ref[len("computed."):] not in computed:
                        r.error(at, f'unresolved template reference "{ref}"')
                    continue
                if ":" in ref:
                    target, key = (s.strip() for s in ref.split(":", 1))
                    if key in ("host", "port") and target in with_port:
                        target_w = by_id[target]
                        if key == "host" and not source_is_ui:
                            if ui_id and target == ui_id:
                                r.error(at, f'references the UI workload "{target}"; only rpc-proxy may call the UI')
                            if source_is_stdio or as_dict(target_w.get("transport")).get("type") == "stdio":
                                r.error(at, "stdio MCP workloads cannot reference or be referenced by siblings (OwnershipConflict)")
                        continue
                    if key in resource_keys.get(target, set()):
                        continue
                r.error(at, f'unresolved template reference "{ref}": use inputs.X, computed.X, '
                        "<workload with a port>:host|port, or <resource id>:KEY from its data")
            if not source_is_ui:
                for match in HOST_PORT_TEMPLATE.finditer(value):
                    host_id, port_id, literal = match.group(1), match.group(2), match.group(3)
                    target_w = by_id.get(host_id)
                    if not target_w or not target_w.get("port"):
                        continue
                    port = by_id.get(port_id, {}).get("port") if port_id else int(literal)
                    if port is not None and port != target_w.get("port"):
                        r.error(at, f'"{host_id}" listens on {target_w.get("port")}, not {port} (InvalidInternalDependency)')
                for match in CLUSTER_LOCAL_HOST.finditer(TEMPLATE.sub("", value)):
                    if match.group(1).rstrip(".").lower().endswith(".sandbox-recipes.svc.cluster.local"):
                        r.error(at, f'hardcoded host "{match.group(1)}": Service names are generated; use {{{{<id>:host}}}}')


def check_workloads(spec: dict, r: Report) -> None:
    workloads = [as_dict(w) for w in as_list(spec.get("workloads"))]
    ui_id = as_dict(spec.get("ui")).get("workloadRef")
    ids = [w.get("id") for w in workloads]
    if len(ids) != len(set(ids)):
        r.error("spec.workloads", "workload ids must be unique")
    id_set = set(ids)
    clients = {as_dict(c).get("id"): as_dict(c) for c in as_list(spec.get("oauthClients"))}
    resources = {as_dict(x).get("id"): as_dict(x) for x in as_list(spec.get("resources"))}
    pvc_ids = {rid for rid, res in resources.items() if res.get("type") == "pvc"}
    isolation = as_dict(spec.get("security")).get("isolationLevel", "minimal")
    for idx, w in enumerate(workloads):
        where = f"spec.workloads[{idx}]"
        wid = w.get("id")
        ns = namespace_of(w, ui_id)
        transport = as_dict(w.get("transport"))
        if transport and not w.get("port"):
            r.error(where, "a workload with transport needs a port")
        if transport.get("type") == "stdio" and w.get("type") != "deployment":
            r.error(where, "stdio transport is only supported on deployment workloads")
        sec = as_dict(w.get("security"))
        for key in ("runAsUser", "runAsGroup", "fsGroup"):
            if key in sec and (not isinstance(sec[key], int) or sec[key] < 1):
                r.error(f"{where}.security.{key}", "must be an integer of at least 1 (root is rejected)")
        for cap in as_list(sec.get("addCapabilities")):
            if cap not in ALLOWED_CAPABILITIES:
                r.error(f"{where}.security.addCapabilities", f"{cap} is not allowed (only {', '.join(sorted(ALLOWED_CAPABILITIES))})")
        if sec.get("prepareVolumeOwnership"):
            if not sec.get("runAsUser"):
                r.error(f"{where}.security", "prepareVolumeOwnership requires runAsUser")
            if not any(not as_dict(m).get("readOnly") for m in as_list(w.get("volumeMounts"))):
                r.error(f"{where}.security", "prepareVolumeOwnership requires a writable volumeMount")
        refs = as_list(w.get("oauthClientRefs"))
        if refs and (transport or wid == ui_id):
            r.error(f"{where}.oauthClientRefs", "not allowed on MCP or UI workloads")
        for ref in refs:
            if ref not in clients:
                r.error(f"{where}.oauthClientRefs", f'"{ref}" is not a spec.oauthClients[].id')
            elif clients[ref].get("backgroundAccess") is not True:
                r.error(f"{where}.oauthClientRefs", f'"{ref}" must declare backgroundAccess: true')
        include_when = w.get("includeWhen")
        if include_when is not None and not INCLUDE_WHEN.match(str(include_when)):
            r.error(f"{where}.includeWhen", "must be exactly {{inputs.KEY}}; anything else always excludes the workload")
        health = as_dict(w.get("healthCheck"))
        if health.get("type") == "http" and not health.get("path"):
            r.warn(f"{where}.healthCheck", "http checks default to path /health; set the path you serve")
        if "imagePullPolicy" in w:
            r.warn(f"{where}.imagePullPolicy", "not honored; containers use IfNotPresent, so publish a new tag per build")
        image = str(w.get("image", ""))
        last = image.rsplit("/", 1)[-1]
        if "@" not in image and (":" not in last or last.endswith(":latest")):
            r.warn(f"{where}.image", "use an immutable tag; a reused tag is never pulled again (IfNotPresent)")
        for name in as_list(w.get("imagePullSecrets")):
            check_secret_name(name, f"{where}.imagePullSecrets", r)
        env_secret = as_dict(w.get("envSecret"))
        check_secret_name(env_secret.get("name"), f"{where}.envSecret.name", r)
        if env_secret.get("name") in resources:
            r.warn(f"{where}.envSecret.name", "names a spec.resources id; envSecret takes a literal Secret name, and resources get generated names")
        if wid == ui_id and env_secret:
            r.info(f"{where}.envSecret", "the UI reads this Secret from sandbox-ui; keep the browser-facing pod credential-free when you can")
        for m in as_list(w.get("volumeMounts")):
            mname = as_dict(m).get("name")
            if mname in resources and mname not in pvc_ids:
                r.warn(f"{where}.volumeMounts", f'"{mname}" is a {resources[mname].get("type")} resource; it mounts as an empty directory')
        if isolation in ("standard", "strict") and not as_list(w.get("volumeMounts")):
            r.info(where, f"isolationLevel {isolation} makes the root filesystem read-only; mount an emptyDir where the process writes")
        check_egress(w, f"{where}.egressBindings", ns, id_set, ui_id, r)
        check_env_secrets(w, where, r)
    check_templates(spec, workloads, ui_id, r)


def check_ui(spec: dict, r: Report) -> None:
    ui = as_dict(spec.get("ui"))
    if not ui:
        return
    workloads = {as_dict(w).get("id"): as_dict(w) for w in as_list(spec.get("workloads"))}
    target = workloads.get(ui.get("workloadRef"))
    if not target:
        r.error("spec.ui.workloadRef", "must name a workload")
        return
    if target.get("type") != "deployment" or target.get("transport") or target.get("replicas", 1) != 1:
        r.error("spec.ui.workloadRef", "the UI workload must be a deployment with 1 replica and no transport")
    if target.get("port") != ui.get("port"):
        r.error("spec.ui.port", "must equal the UI workload's port (the Desktop cannot open the app otherwise)")
    if ui.get("port") != 8080:
        r.warn("spec.ui.port", "rpc-proxy allows only 8080 by default; other ports fail with 502 port_not_allowed")
    internal = {as_dict(e).get("workloadRef"): as_dict(e).get("port") for e in as_list(as_dict(ui.get("egress")).get("internal"))}
    for ref, port in internal.items():
        tw = workloads.get(ref)
        if not tw:
            r.error("spec.ui.egress.internal", f'"{ref}" is not a workload')
        elif tw.get("transport"):
            r.error("spec.ui.egress.internal", f'"{ref}" is an MCP workload; the UI cannot call it')
        elif tw.get("port") and port != tw.get("port"):
            r.warn("spec.ui.egress.internal", f'"{ref}" listens on {tw.get("port")}; port {port} opens nothing it serves')
    for _, value in string_fields(target):
        for body in TEMPLATE.findall(value):
            ref = body.strip()
            if ref.endswith(":host") and ref[:-5].strip() in workloads and ref[:-5].strip() not in internal:
                r.warn("spec.ui.egress.internal", f'the UI references "{ref[:-5].strip()}" but has no egress.internal entry for it; calls will hang')


def check_bindings(spec: dict, r: Report) -> None:
    workloads = {as_dict(w).get("id"): as_dict(w) for w in as_list(spec.get("workloads"))}
    for i, b in enumerate(as_list(spec.get("bindings"))):
        b = as_dict(b)
        at = f"spec.bindings[{i}]"
        src, dst = workloads.get(b.get("from")), workloads.get(b.get("to"))
        if src is None or dst is None:
            r.error(at, "from and to must name workloads")
            continue
        if not isinstance(b.get("port"), int) or not 1 <= b["port"] <= 65535:
            r.error(at, "port must be 1-65535")
        if bool(src.get("transport")) == bool(dst.get("transport")):
            r.error(at, "a binding must connect exactly one MCP transport workload to one non-transport workload")
        elif dst.get("port") and b.get("port") != dst.get("port"):
            r.warn(at, f'"{b.get("to")}" listens on {dst.get("port")}, not {b.get("port")}')


def check_sdk(spec: dict, r: Report) -> None:
    sdk = spec.get("pluginWorkloadSdk")
    if sdk is None:
        return
    sdk = as_dict(sdk)
    steps = as_list(spec.get("steps"))
    workloads = [as_dict(w) for w in as_list(spec.get("workloads"))]
    ids = {w.get("id") for w in workloads}
    ui_id = as_dict(spec.get("ui")).get("workloadRef")
    where = "spec.pluginWorkloadSdk"
    r.info(where, "needs the SDK enabled on the cluster and an operator grant per family; on a cluster without the SDK a "
           "recipe without steps deploys nothing")
    if not steps:
        if not workloads:
            r.error(where, "a recipe without steps needs at least one workload")
        for key in ("triggers", "scheduling", "coordinatorImage"):
            if key in spec:
                r.error(f"spec.{key}", "not allowed with pluginWorkloadSdk on a recipe without steps")
    prompt, notify = sdk.get("promptBridge"), sdk.get("clientNotifications")
    if prompt is None and notify is None:
        r.error(where, "declare promptBridge, clientNotifications, or both")
    if notify is not None:
        types = as_list(as_dict(notify).get("allowedEventTypes"))
        if not types:
            r.error(f"{where}.clientNotifications.allowedEventTypes", "list at least one event type")
        if any("*" in str(t) for t in types + as_list(as_dict(notify).get("allowedTargetRefs"))):
            r.error(f"{where}.clientNotifications", "wildcards are not allowed")
    agent = as_dict(spec.get("agent"))
    step_agent = any(as_dict(as_dict(s).get("agent")).get("provider") and as_dict(as_dict(s).get("agent")).get("model") for s in steps)
    if prompt is not None:
        if any("*" in str(m) for m in as_list(as_dict(prompt).get("allowedModels"))):
            r.error(f"{where}.promptBridge.allowedModels", "wildcards are not allowed")
        if not (agent.get("provider") and agent.get("model")) and not step_agent:
            r.error("spec.agent", "promptBridge requires spec.agent (or a step agent) with provider and model")
    elif spec.get("agent") and not steps:
        r.error("spec.agent", "not allowed on a recipe without steps unless it declares promptBridge (R1)")
    callers = as_list(sdk.get("allowedCallers"))
    for c in callers:
        if c not in ids:
            if steps:
                r.warn(f"{where}.allowedCallers", f'"{c}" is not a workload (silently dropped on recipes with steps)')
            else:
                r.error(f"{where}.allowedCallers", f'"{c}" is not a workload (PS4)')
    eligible = {w.get("id") for w in workloads if not w.get("transport") and w.get("id") != ui_id}
    for c in callers:
        if c in ids and c not in eligible:
            r.warn(f"{where}.allowedCallers", f'"{c}" is a UI or MCP workload and never receives an SDK token')
    if not (eligible & set(callers) if callers else eligible):
        r.warn(f"{where}.allowedCallers", "no backend workload is eligible to call the SDK")


def check_webhooks_oauth(spec: dict, r: Report) -> None:
    workloads = {as_dict(w).get("id"): as_dict(w) for w in as_list(spec.get("workloads"))}
    for i, wh in enumerate(as_list(spec.get("webhooks"))):
        wh = as_dict(wh)
        at = f"spec.webhooks[{i}]"
        handler = workloads.get(wh.get("workloadRef"))
        if handler is None:
            r.error(at, "workloadRef must name a workload (W2)")
        elif handler.get("type") != "deployment" or handler.get("transport"):
            r.error(at, "the handler must be a deployment without transport (W2)")
        elif not handler.get("port"):
            r.warn(at, "the handler has no port; the gateway forwards to 8080")
        verification = as_dict(wh.get("verification"))
        for ref_key in ("secretRef",):
            check_secret_name(as_dict(verification.get(ref_key)).get("name"), f"{at}.verification.{ref_key}", r)
        check_secret_name(as_dict(as_dict(verification.get("setupHandshake")).get("secretRef")).get("name"),
                          f"{at}.verification.setupHandshake.secretRef", r)
    for i, c in enumerate(as_list(spec.get("oauthClients"))):
        c = as_dict(c)
        needed = OFFLINE_SCOPE.get(str(c.get("provider")))
        if c.get("backgroundAccess") is True and needed and needed not in as_list(c.get("scopes")):
            r.error(f"spec.oauthClients[{i}].scopes", f'backgroundAccess needs the "{needed}" scope for {c.get("provider")}')
        for ref_key in ("clientIdRef", "clientSecretRef"):
            check_secret_name(as_dict(c.get(ref_key)).get("name"), f"spec.oauthClients[{i}].{ref_key}", r)


def check_misc(spec: dict, r: Report) -> None:
    if spec.get("dependencies"):
        r.warn("spec.dependencies", "accepted by the CRD but not acted on by the controller")
    for name, profile in as_dict(spec.get("profiles")).items():
        if any(isinstance(v, (dict, list)) for v in as_dict(profile).values()):
            r.warn(f"spec.profiles.{name}", "profiles only override input values; nested spec overrides are not applied")
    for i, res in enumerate(as_list(spec.get("resources"))):
        res = as_dict(res)
        if res.get("type") in ("secret", "configmap"):
            r.info(f"spec.resources[{i}]", "workloads cannot reference this by id (it gets a generated name); use it for PVCs only")


def check_document(doc: dict, source: str) -> Report:
    r = Report(source)
    spec = as_dict(doc.get("spec"))
    check_metadata(doc, spec, r)
    check_workflow_fields(doc, spec, r)
    check_workloads(spec, r)
    check_ui(spec, r)
    check_bindings(spec, r)
    check_sdk(spec, r)
    check_webhooks_oauth(spec, r)
    check_misc(spec, r)
    return r


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write(__doc__ or "")
        return 2
    total_errors = 0
    for path in argv:
        try:
            with open(path, encoding="utf-8") as handle:
                docs = [d for d in yaml.safe_load_all(handle) if isinstance(d, dict)]
        except (OSError, yaml.YAMLError) as exc:
            sys.stderr.write(f"{path}: cannot read: {exc}\n")
            return 2
        recipes = [d for d in docs if d.get("kind") == "WorkflowRecipe"]
        if not recipes:
            sys.stderr.write(f"{path}: no WorkflowRecipe found\n")
            return 2
        for doc in recipes:
            name = as_dict(doc.get("metadata")).get("name", "?")
            report = check_document(doc, path)
            print(f"{path} ({name}): {report.errors} error(s)")
            for level, where, message in report.items:
                print(f"  {level:<5} {where}: {message}")
            total_errors += report.errors
    return 1 if total_errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
