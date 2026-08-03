import re
from pathlib import Path
from lib.iac import parse_yaml_lite, _BLOCK_STYLES

# ---------------------------------------------------------------------------
# docker-compose + Kubernetes manifest parser.
#
# ID scheme (per the system-view-v2 plan, task 3), following the file-
# qualified convention set by lib/iac_cfn.py:
#   compose service       : "compose:<relative_file_path>::<service name>"
#   k8s object (a box)     : "k8s:<relative_file_path>::<kind>/<metadata.name>"
#   k8s ServiceAccount as a role identity: "k8s:<relative_file_path>::sa/<name>"
#     -- distinct from the ServiceAccount's own resource id above; a
#     ServiceAccount is both a box (like aws_iam_role in Terraform) and the
#     identity RBAC grants attach to.
#
# Divergence from the Terraform/CFN "via role" convention, documented here
# because it is easy to miss: a k8s Role/ClusterRole rule's "on" names an
# API group + resource kind (e.g. "apps/deployments"), which is never a box
# in this repo's resource graph -- "via role" can't point a connection at a
# specific granted target the way it does for AWS. What *is* still a box is
# the ServiceAccount itself, so a workload's "runs as" connection targets
# the ServiceAccount's own resource id (dst = "k8s:<file>::ServiceAccount/
# <name>", same cross-parser contract every other connection follows: dst
# is always a resource id) with role=<sa display name> (bare name, matching
# the TF/CFN convention of role["name"] -- not an id). The grants
# themselves still live on the separate role entry keyed
# "k8s:<file>::sa/<name>", reachable by that bare name.
# ---------------------------------------------------------------------------

COMPOSE_NAME_RE = re.compile(r'(?i)^(docker-compose|compose)([.\-_][\w.-]*)?\.ya?ml$')

K8S_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job"}
K8S_CONFIG_KINDS = {"ConfigMap", "Secret"}
K8S_RBAC_ROLE_KINDS = {"Role", "ClusterRole"}
K8S_RBAC_BINDING_KINDS = {"RoleBinding", "ClusterRoleBinding"}
K8S_ALL_KINDS = (K8S_WORKLOAD_KINDS | K8S_CONFIG_KINDS | K8S_RBAC_ROLE_KINDS
                  | K8S_RBAC_BINDING_KINDS | {"Service", "ServiceAccount", "Ingress"})

# Host extraction for the "network dns" env-value scan: after "scheme://",
# skip an optional "userinfo@" segment (RFC 3986 -- "user:pass@host") before
# capturing the host, so a URL like "http://db:secret@realhost/x" resolves
# to "realhost", not the userinfo "db" (which would otherwise collide with
# an unrelated service literally named "db").
URL_HOST_RE = re.compile(r'[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^/@\s]*@)?([^:/\s]+)')


def parse_doc(filename: str, rel: str, text: str, parsed_whole: dict) -> dict | None:
    """Entry point dispatched from lib.iac.scan_iac for one *.yaml/*.yml
    file. `parsed_whole` is a best-effort single-pass parse_yaml_lite() of
    the entire raw file, used only for the compose "services:" check --
    for a multi-doc k8s file that parse is a garbled merge of every
    document's top-level keys (parse_yaml_lite has no notion of "---"
    document boundaries), harmless here since it will never carry a
    top-level "services" key. Returns None if nothing recognized."""
    if is_compose_doc(filename, parsed_whole):
        return parse_compose_doc(parsed_whole, text, rel)
    docs = _collect_k8s_docs(text)
    if not docs:
        return None
    return _build_k8s_model(docs, rel)


def is_compose_doc(filename: str, parsed) -> bool:
    if not COMPOSE_NAME_RE.match(Path(filename).name):
        return False
    return isinstance(parsed, dict) and isinstance(parsed.get("services"), dict) and bool(parsed["services"])


# ---------------------------------------------------------------------------
# raw-text helpers (same convention as lib/iac_cfn.py's _find_key_line /
# _key_block: parse_yaml_lite discards source positions, so line numbers are
# recovered by re-scanning the raw text). Bounded by an explicit [start, end)
# so a repeated key name elsewhere in the file/doc can't be mistaken for the
# one inside the block being searched.
# ---------------------------------------------------------------------------

def _block(text, key, start=0, end=None):
    """Find `key:` at start-of-line (any indent) within text[start:end).
    Returns (line, indent, content_start, content_end) -- content_end is
    where this key's block dedents back to <= its own indent, or `end`.
    Returns None if not found."""
    end = len(text) if end is None else end
    pat = re.compile(r'^([ \t]*)"?' + re.escape(key) + r'"?\s*:', re.M)
    m = pat.search(text, start, end)
    if not m:
        return None
    indent = len(m.group(1))
    line = text.count("\n", 0, m.start()) + 1
    content_start = m.end()
    pos = content_start
    block_end = end
    for l in text[content_start:end].split("\n"):
        stripped = l.strip()
        if stripped and not stripped.startswith("#"):
            li = len(l) - len(l.lstrip(" \t"))
            if li <= indent:
                block_end = pos
                break
        pos += len(l) + 1
    return line, indent, content_start, block_end


def _first_line(text, key, start=0, end=None):
    b = _block(text, key, start, end)
    return b[0] if b else None


# A block-scalar-opening line: "<key>: |" / "<key>: >-" etc, or the same
# indicator on a bare list item ("- |"). Matched at end-of-line only (mirrors
# parse_yaml_lite's own _BLOCK_STYLES set) so a plain scalar that merely
# contains one of these tokens mid-value doesn't falsely open a block.
_BLOCK_SCALAR_OPEN_RE = re.compile(
    r'(?::|-)[ \t]*(' + '|'.join(re.escape(s) for s in _BLOCK_STYLES) + r')[ \t]*$'
)


def split_yaml_docs(text):
    """Split raw YAML text into per-document (chunk_text, start_line)
    pairs on lines that are '---' (a YAML document separator) and nothing
    else on that line, at column 0 only (leading whitespace disqualifies a
    line; only trailing whitespace/\\r is stripped before the comparison).
    start_line is the 1-based line number of the chunk's first line in the
    original file, so line numbers recovered from the chunk can be mapped
    back to real file:line evidence. parse_yaml_lite has no notion of
    multi-doc streams, so any k8s file with more than one manifest must be
    split before being handed to it -- this is that split.

    Separator detection is suspended while inside a block scalar (a "|" or
    ">"-style value): its content is opaque text that can legitimately
    contain an indented "---" (e.g. a markdown rule inside a ConfigMap's
    data field), and column-0-only already rules out most false positives,
    but a block scalar based at zero indentation would still collide
    without this -- so block-scalar state is tracked explicitly rather than
    relied on indentation alone."""
    lines = text.split("\n")
    docs = []
    cur = []
    cur_start = 1
    in_block = False
    block_indent = 0
    for i, line in enumerate(lines, start=1):
        if in_block:
            if line.strip() != "":
                li = len(line) - len(line.lstrip(" \t"))
                if li <= block_indent:
                    in_block = False
                else:
                    cur.append(line)
                    continue
            else:
                cur.append(line)
                continue
        if line.rstrip() == "---":
            if cur:
                docs.append(("\n".join(cur), cur_start))
            cur = []
            cur_start = i + 1
            continue
        cur.append(line)
        if _BLOCK_SCALAR_OPEN_RE.search(line):
            in_block = True
            block_indent = len(line) - len(line.lstrip(" \t"))
    if cur:
        docs.append(("\n".join(cur), cur_start))
    return docs


def _deep_find_all(node, key):
    if isinstance(node, dict):
        if key in node:
            yield node[key]
        for v in node.values():
            yield from _deep_find_all(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _deep_find_all(v, key)


def _deep_find_first(node, key):
    for v in _deep_find_all(node, key):
        return v
    return None


def _mount_refs(parsed):
    """(kind, name) pairs for every ConfigMap/Secret a workload doc
    references via envFrom or a volume, found by a best-effort deep scan
    for the marker keys k8s uses for each shape (configMapRef/secretRef in
    envFrom and volumes[].configMap, configMap/secret in volumes[]) rather
    than an exact envFrom/volumes path -- this also catches CronJob's more
    deeply nested jobTemplate.spec.template.spec shape for free."""
    refs = []
    for ref in _deep_find_all(parsed, "configMapRef"):
        if isinstance(ref, dict) and isinstance(ref.get("name"), str):
            refs.append(("ConfigMap", ref["name"]))
    for ref in _deep_find_all(parsed, "secretRef"):
        if isinstance(ref, dict) and isinstance(ref.get("name"), str):
            refs.append(("Secret", ref["name"]))
    for ref in _deep_find_all(parsed, "configMap"):
        if isinstance(ref, dict) and isinstance(ref.get("name"), str):
            refs.append(("ConfigMap", ref["name"]))
    for ref in _deep_find_all(parsed, "secret"):
        if isinstance(ref, dict) and isinstance(ref.get("secretName"), str):
            refs.append(("Secret", ref["secretName"]))
    return refs


def _ingress_backend_names(parsed):
    names = set()
    spec = parsed.get("spec") or {}
    legacy = spec.get("backend")
    if isinstance(legacy, dict) and isinstance(legacy.get("serviceName"), str):
        names.add(legacy["serviceName"])
    for rule in spec.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        http = rule.get("http") or {}
        for path in http.get("paths") or []:
            if not isinstance(path, dict):
                continue
            backend = path.get("backend") or {}
            svc = backend.get("service")
            if isinstance(svc, dict) and isinstance(svc.get("name"), str):
                names.add(svc["name"])
            elif isinstance(backend.get("serviceName"), str):
                names.add(backend["serviceName"])
    return names


def _meta_name_line(chunk, offset):
    mb = _block(chunk, "metadata")
    if mb:
        nb = _block(chunk, "name", mb[2], mb[3])
        if nb:
            return nb[0] + offset
    return 1 + offset


# ---------------------------------------------------------------------------
# compose
# ---------------------------------------------------------------------------

def parse_compose_doc(parsed: dict, text: str, filename: str) -> dict:
    id_prefix = f"compose:{filename}::"
    services = parsed.get("services") or {}
    named_volumes = parsed.get("volumes") if isinstance(parsed.get("volumes"), dict) else {}

    services_blk = _block(text, "services")
    svc_bounds = {}
    resources = []
    for name, body in services.items():
        if not isinstance(body, dict):
            body = {}
        blk = _block(text, name, services_blk[2], services_blk[3]) if services_blk else None
        line = blk[0] if blk else 1
        svc_bounds[name] = (blk[2], blk[3]) if blk else (0, len(text))
        resources.append({
            "id": id_prefix + name, "rtype": "compose service", "name": name,
            "source_file": filename, "line": line, "attrs": {},
        })

    connections = []

    # depends_on -> "depends on" (supports both the short list form and the
    # long {name: {condition: ...}} form -- only the names matter here)
    for name, body in services.items():
        if not isinstance(body, dict):
            continue
        dep = body.get("depends_on")
        dep_names = list(dep.keys()) if isinstance(dep, dict) else (dep if isinstance(dep, list) else [])
        if not dep_names:
            continue
        s, e = svc_bounds.get(name, (0, len(text)))
        dline = _first_line(text, "depends_on", s, e) or 1
        for dn in dep_names:
            if dn in services and dn != name:
                connections.append({
                    "src": id_prefix + name, "dst": id_prefix + dn,
                    "mechanism": "depends on", "role": None,
                    "evidence": f"{filename}:{dline}",
                })

    # links -> "links"
    for name, body in services.items():
        if not isinstance(body, dict) or not isinstance(body.get("links"), list):
            continue
        s, e = svc_bounds.get(name, (0, len(text)))
        lline = _first_line(text, "links", s, e) or 1
        for link in body["links"]:
            target = link.split(":", 1)[0] if isinstance(link, str) else None
            if target and target in services and target != name:
                connections.append({
                    "src": id_prefix + name, "dst": id_prefix + target,
                    "mechanism": "links", "role": None,
                    "evidence": f"{filename}:{lline}",
                })

    # shared named volumes -> "shared volume", evidence at the later
    # (second-and-onward, in service declaration order) referencing service
    vol_refs = {}
    for name, body in services.items():
        if not isinstance(body, dict) or not isinstance(body.get("volumes"), list):
            continue
        s, e = svc_bounds.get(name, (0, len(text)))
        for v in body["volumes"]:
            if not isinstance(v, str) or ":" not in v:
                continue
            vol_name = v.split(":", 1)[0]
            if vol_name not in named_volumes:
                continue
            idx = text.find(v, s, e)
            vline = (text.count("\n", 0, idx) + 1) if idx != -1 else (_first_line(text, "volumes", s, e) or 1)
            vol_refs.setdefault(vol_name, []).append((name, vline))
    for vol_name, refs in vol_refs.items():
        if len(refs) < 2:
            continue
        first_service = refs[0][0]
        for svc_name, vline in refs[1:]:
            connections.append({
                "src": id_prefix + first_service, "dst": id_prefix + svc_name,
                "mechanism": "shared volume", "role": None,
                "evidence": f"{filename}:{vline}",
            })

    # environment values referencing another service by exact hostname in a
    # URL -> "network dns" (best-effort: <scheme>://<host>[:port] scan)
    for name, body in services.items():
        if not isinstance(body, dict):
            continue
        env = body.get("environment")
        pairs = []
        if isinstance(env, dict):
            pairs = [(k, v) for k, v in env.items() if isinstance(v, str)]
        elif isinstance(env, list):
            for item in env:
                if isinstance(item, str) and "=" in item:
                    k, v = item.split("=", 1)
                    pairs.append((k, v))
        s, e = svc_bounds.get(name, (0, len(text)))
        for key, val in pairs:
            m = URL_HOST_RE.search(val)
            if not m:
                continue
            host = m.group(1)
            if host in services and host != name:
                idx = text.find(val, s, e)
                eline = (text.count("\n", 0, idx) + 1) if idx != -1 else (_first_line(text, key, s, e) or 1)
                connections.append({
                    "src": id_prefix + name, "dst": id_prefix + host,
                    "mechanism": "network dns", "role": None,
                    "evidence": f"{filename}:{eline}",
                })

    resources.sort(key=lambda r: r["line"])
    connections.sort(key=lambda c: (c["src"], c["dst"], c["mechanism"]))
    return {"resources": resources, "roles": [], "connections": connections, "deployed": {}}


# ---------------------------------------------------------------------------
# kubernetes
# ---------------------------------------------------------------------------

def _collect_k8s_docs(text):
    docs = []
    for chunk_text, start_line in split_yaml_docs(text):
        if not chunk_text.strip():
            continue
        try:
            parsed = parse_yaml_lite(chunk_text)
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        kind = parsed.get("kind")
        if kind not in K8S_ALL_KINDS:
            continue
        meta = parsed.get("metadata")
        name = meta.get("name") if isinstance(meta, dict) else None
        if not name:
            continue
        docs.append({"kind": kind, "name": name, "parsed": parsed,
                      "chunk": chunk_text, "offset": start_line - 1})
    return docs


def _build_k8s_model(docs, filename):
    id_prefix = f"k8s:{filename}::"
    resources = []
    roles_by_sa = {}
    sa_resource_ids = {}   # sa name -> its own "ServiceAccount/<name>" resource id
    role_rules = {}       # (kind, name) -> [{"actions":.., "on":..}, ...]
    workloads = []
    services = []
    ingress_docs = []
    configs = {}           # (kind, name) -> resource id
    bindings = []

    for d in docs:
        kind, name, parsed, chunk, offset = d["kind"], d["name"], d["parsed"], d["chunk"], d["offset"]
        name_line = _meta_name_line(chunk, offset)

        if kind in K8S_WORKLOAD_KINDS:
            rid = id_prefix + f"{kind}/{name}"
            resources.append({"id": rid, "rtype": kind, "name": name,
                               "source_file": filename, "line": name_line, "attrs": {}})
            sa_name = _deep_find_first(parsed, "serviceAccountName")
            sa_line = _first_line(chunk, "serviceAccountName") if sa_name else None
            pod_labels = (((parsed.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels")
            if not isinstance(pod_labels, dict):
                meta_labels = (parsed.get("metadata") or {}).get("labels")
                pod_labels = meta_labels if isinstance(meta_labels, dict) else {}
            workloads.append({"id": rid, "sa_name": sa_name,
                               "sa_line": (sa_line + offset) if sa_line else None,
                               "pod_labels": pod_labels, "parsed": parsed,
                               "chunk": chunk, "offset": offset, "name_line": name_line})

        elif kind == "Service":
            rid = id_prefix + f"{kind}/{name}"
            resources.append({"id": rid, "rtype": kind, "name": name,
                               "source_file": filename, "line": name_line, "attrs": {}})
            selector = (parsed.get("spec") or {}).get("selector")
            sel_line = _first_line(chunk, "selector") if isinstance(selector, dict) and selector else None
            services.append({"id": rid, "selector": selector or {},
                              "sel_line": (sel_line + offset) if sel_line else name_line})

        elif kind == "Ingress":
            rid = id_prefix + f"{kind}/{name}"
            resources.append({"id": rid, "rtype": kind, "name": name,
                               "source_file": filename, "line": name_line, "attrs": {}})
            ingress_docs.append({"id": rid, "backend_names": _ingress_backend_names(parsed),
                                  "line": name_line})

        elif kind in K8S_CONFIG_KINDS:
            rid = id_prefix + f"{kind}/{name}"
            resources.append({"id": rid, "rtype": kind, "name": name,
                               "source_file": filename, "line": name_line, "attrs": {}})
            configs[(kind, name)] = rid

        elif kind == "ServiceAccount":
            rid = id_prefix + f"{kind}/{name}"
            role_id = id_prefix + f"sa/{name}"
            resources.append({"id": rid, "rtype": kind, "name": name,
                               "source_file": filename, "line": name_line, "attrs": {}})
            roles_by_sa[name] = {"id": role_id, "name": name, "grants": [],
                                  "attached_to": [], "source_file": filename, "line": name_line}
            sa_resource_ids[name] = rid

        elif kind in K8S_RBAC_ROLE_KINDS:
            grants = []
            for rule in parsed.get("rules") or []:
                if not isinstance(rule, dict):
                    continue
                api_groups = rule.get("apiGroups") or [""]
                res_list = rule.get("resources") or [""]
                verbs = rule.get("verbs") or []
                for ag in api_groups:
                    for rsrc in res_list:
                        grants.append({"actions": list(verbs), "on": f"{ag}/{rsrc}"})
            role_rules[(kind, name)] = grants

        elif kind in K8S_RBAC_BINDING_KINDS:
            subjects = parsed.get("subjects") or []
            role_ref = parsed.get("roleRef") or {}
            sa_names = [s.get("name") for s in subjects
                        if isinstance(s, dict) and s.get("kind") == "ServiceAccount" and s.get("name")]
            if sa_names and isinstance(role_ref.get("name"), str):
                bindings.append({"sa_names": sa_names,
                                  "role_ref": (role_ref.get("kind"), role_ref["name"])})

    connections = []

    # RoleBinding/ClusterRoleBinding -> attach the referenced Role/ClusterRole's
    # grants onto each subject ServiceAccount's role identity
    for b in bindings:
        rules = role_rules.get(b["role_ref"], [])
        for sa_name in b["sa_names"]:
            role = roles_by_sa.get(sa_name)
            if not role:
                continue
            for g in rules:
                if g not in role["grants"]:
                    role["grants"].append(g)

    # workloads -> attached_to + "runs as" connection to the SA's own
    # resource box (see module docstring: a k8s grant targets an API group,
    # not a repo box, so unlike "via role" there's no specific granted
    # target to point at -- dst falls back to the ServiceAccount itself,
    # keeping the same "dst is always a resource id" contract every other
    # connection follows; role is the bare SA name, matching TF/CFN)
    for w in workloads:
        if not w["sa_name"]:
            continue
        role = roles_by_sa.get(w["sa_name"])
        sa_rid = sa_resource_ids.get(w["sa_name"])
        if not role or not sa_rid:
            continue
        if w["id"] not in role["attached_to"]:
            role["attached_to"].append(w["id"])
        line = w["sa_line"] or w["name_line"]
        connections.append({
            "src": w["id"], "dst": sa_rid, "mechanism": "runs as",
            "role": role["name"], "evidence": f"{filename}:{line}",
        })

    # Service selector vs workload pod labels -> "selects"
    for svc in services:
        if not svc["selector"]:
            continue
        for w in workloads:
            labels = w["pod_labels"] or {}
            if all(labels.get(k) == v for k, v in svc["selector"].items()):
                connections.append({
                    "src": svc["id"], "dst": w["id"], "mechanism": "selects",
                    "role": None, "evidence": f"{filename}:{svc['sel_line']}",
                })

    # Ingress backend service refs -> "routes to"
    services_by_name = {sid["id"].rsplit("/", 1)[-1]: sid["id"] for sid in services}
    for ing in ingress_docs:
        for bn in ing["backend_names"]:
            target = services_by_name.get(bn)
            if target:
                connections.append({
                    "src": ing["id"], "dst": target, "mechanism": "routes to",
                    "role": None, "evidence": f"{filename}:{ing['line']}",
                })

    # ConfigMap/Secret referenced via envFrom/volumes -> "mounts"
    for w in workloads:
        for kind, name in _mount_refs(w["parsed"]):
            target = configs.get((kind, name))
            if not target:
                continue
            m = re.search(r'\b' + re.escape(name) + r'\b', w["chunk"])
            line = (w["chunk"].count("\n", 0, m.start()) + 1 + w["offset"]) if m else w["name_line"]
            connections.append({
                "src": w["id"], "dst": target, "mechanism": "mounts",
                "role": None, "evidence": f"{filename}:{line}",
            })

    resources.sort(key=lambda r: r["line"])
    roles = sorted(roles_by_sa.values(), key=lambda r: r["line"])
    connections.sort(key=lambda c: (c["src"], c["dst"], c["mechanism"]))
    return {"resources": resources, "roles": roles, "connections": connections, "deployed": {}}
