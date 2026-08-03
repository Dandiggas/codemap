import re
from lib.iac import parse_yaml_lite, _line_at, empty_model

# ---------------------------------------------------------------------------
# CloudFormation / SAM / serverless.yml parser.
#
# ID scheme (documented per the system-view-v2 plan, task 2): Terraform ids
# stay "<resource_type>.<name>" (unchanged). To merge cleanly with those, and
# to stay unique across arbitrarily many template files in one repo, every id
# here is qualified by its source file:
#   CFN/SAM resource or role : "cfn:<relative_file_path>::<LogicalId>"
#   serverless.yml function  : "sls:<relative_file_path>::<function name>"
#   serverless implicit role : "sls:<relative_file_path>::provider"
#   SAM implicit function role: "cfn:<relative_file_path>::<LogicalId>Role (implicit)"
# Two templates that both define a logical id "Table" therefore never collide.
# ---------------------------------------------------------------------------

CFN_ROLE_TYPES = {"AWS::IAM::Role"}
SAM_FUNCTION_TYPE = "AWS::Serverless::Function"
ROLE_ATTR_KEYS = ("Role", "ExecutionRoleArn", "TaskRoleArn")
EXCLUDE_REF_KEYS = set(ROLE_ATTR_KEYS) | {"Events", "Policies"}
EVENT_SOURCE_PROP = {"SQS": "Queue", "DynamoDB": "Stream", "Kinesis": "Stream",
                      "S3": "Bucket", "SNS": "Topic"}

REF_TAG_RE = re.compile(r'^!Ref\s+([A-Za-z0-9_]+)$')
GETATT_TAG_RE = re.compile(r'^!GetAtt\s+([A-Za-z0-9_]+)(?:\.[A-Za-z0-9_.]+)?$')
SUB_TAG_RE = re.compile(r'^!Sub\s+(.+)$')
SUB_VAR_RE = re.compile(r'\$\{([A-Za-z0-9_]+)(?:\.[A-Za-z0-9_]+)?\}')
# Policy statement list-item start, tolerant of YAML dash-list style
# ("- Effect: Allow") and plain-indented style (as used by pretty-printed
# JSON templates, which never have a leading "-").
STATEMENT_START_RE = re.compile(r'^[ \t]*(?:-[ \t]*)?"?Effect"?\s*:', re.M)


def is_cfn_doc(parsed) -> bool:
    """A CFN/SAM template: top-level Resources map whose entries look like
    real resources (each has a Type). Guards against an unrelated file that
    happens to have a "Resources" key."""
    if not isinstance(parsed, dict):
        return False
    resources = parsed.get("Resources")
    if not isinstance(resources, dict) or not resources:
        return False
    return any(isinstance(v, dict) and isinstance(v.get("Type"), str) for v in resources.values())


def is_serverless_doc(parsed) -> bool:
    return (isinstance(parsed, dict) and "service" in parsed
            and isinstance(parsed.get("functions"), dict) and bool(parsed["functions"]))


# ---------------------------------------------------------------------------
# raw-text helpers: parse_yaml_lite discards source positions, so best-effort
# line numbers are recovered by re-scanning the raw text. Not exact for
# minified JSON or repeated key names -- documented limitation, not fixed.
# ---------------------------------------------------------------------------

def _find_key_line(text, key):
    """First occurrence anywhere in text of `key:` (optionally quoted, for
    JSON) as a mapping key -> 1-based line number, defaulting to 1."""
    pat = re.compile(r'^[ \t]*"?' + re.escape(key) + r'"?\s*:', re.M)
    m = pat.search(text)
    return (text.count("\n", 0, m.start()) + 1) if m else 1


def _key_block(text, key, search_from=0):
    """Locate `key:` starting at-or-after char offset search_from.
    Returns (line, start_offset, end_offset) where end_offset is the offset
    of the next line at the same-or-lower indentation (the point this key's
    block dedents), or len(text) if none. Returns None if not found."""
    pat = re.compile(r'^([ \t]*)"?' + re.escape(key) + r'"?\s*:', re.M)
    m = pat.search(text, search_from)
    if not m:
        return None
    indent = len(m.group(1))
    start = m.start()
    end = len(text)
    pos = m.end()
    for line in text[m.end():].split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            li = len(line) - len(line.lstrip(" \t"))
            if li <= indent:
                end = pos
                break
        pos += len(line) + 1
    return text.count("\n", 0, start) + 1, start, end


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _unwrap_quotes(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _extract_ref_names(value):
    """Yield every logical-id NAME referenced by `value`, in source order:
    Ref (dict long-form {"Ref": "X"} or short-form "!Ref X"), Fn::GetAtt
    (dict {"Fn::GetAtt": [...]}/"X.Attr" or short-form "!GetAtt X.Attr"),
    and Fn::Sub ${Var}/${Var.Attr} placeholders (dict Fn::Sub, short-form
    "!Sub ...", or a bare string containing "${...}" with no tag at all).

    A Sub can name more than one variable ("${A.Arn}-${B.Arn}"), so this
    always scans with finditer, never search -- the single source of truth
    for Ref/GetAtt/Sub extraction shared by _resolve_target[s_multi] (single-
    slot property resolution) and _iter_ref_targets (deep multi-ref scan).
    Using one function for both closes the drift that let _resolve_target's
    Sub handling regress to first-match-only while _iter_ref_targets stayed
    exhaustive -- they physically cannot diverge again since there is only
    one code path left to get it wrong in."""
    if isinstance(value, dict):
        if isinstance(value.get("Ref"), str):
            yield value["Ref"]
        if "Fn::GetAtt" in value:
            g = value["Fn::GetAtt"]
            lid = (g[0] if isinstance(g, list) and g
                   else g.split(".", 1)[0] if isinstance(g, str) else None)
            if lid:
                yield lid
        if isinstance(value.get("Fn::Sub"), str):
            for m in SUB_VAR_RE.finditer(value["Fn::Sub"]):
                yield m.group(1)
        return
    if isinstance(value, str):
        s = value.strip()
        m = REF_TAG_RE.match(s) or GETATT_TAG_RE.match(s)
        if m:
            yield m.group(1)
            return
        m = SUB_TAG_RE.match(s)
        body = _unwrap_quotes(m.group(1)) if m else s
        for vm in SUB_VAR_RE.finditer(body):
            yield vm.group(1)


def _literal_fallback(value):
    """value's best-effort external/literal representation, used when none
    of the names _extract_ref_names finds in it are internal to this
    template (an unresolved Ref/GetAtt/Sub, or a plain scalar with no
    intrinsic at all -- e.g. a literal ARN)."""
    if isinstance(value, dict):
        if isinstance(value.get("Fn::Sub"), str):
            return value["Fn::Sub"]
        if isinstance(value.get("Ref"), str):
            return value["Ref"]
        if "Fn::GetAtt" in value:
            g = value["Fn::GetAtt"]
            return g if isinstance(g, str) else ".".join(g) if isinstance(g, list) else None
        return None  # unresolvable intrinsic (Fn::Join etc.) -- best-effort punt
    if isinstance(value, str):
        s = value.strip()
        m = SUB_TAG_RE.match(s)
        return _unwrap_quotes(m.group(1)) if m else s
    return None


def _resolve_targets_multi(value, logical_ids, id_prefix):
    """Every target `value` resolves to, in source order: each name
    _extract_ref_names finds that IS a known logical id resolves to
    id_prefix + name (deduped); if none of them are internal, falls back to
    a single-element list holding the literal representation (so a plain
    ARN, or a Ref/Sub naming only external things, still produces exactly
    one grant/connection endpoint, matching prior single-target behaviour).
    Returns [] only for value is None. This is the multi-target sibling of
    _resolve_target -- used wherever more than one endpoint is meaningful
    (a policy statement's Resource, which a multi-var Sub can name several
    of); _resolve_target stays single-valued for slots where exactly one
    target makes sense (Role:, ExecutionRoleArn:, an event source's Queue:)."""
    if value is None:
        return []
    seen = set()
    targets = []
    for lid in _extract_ref_names(value):
        if lid in logical_ids:
            t = id_prefix + lid
            if t not in seen:
                seen.add(t)
                targets.append(t)
    if targets:
        return targets
    literal = _literal_fallback(value)
    return [literal] if literal is not None else []


def _resolve_target(value, logical_ids, id_prefix):
    """Single-target resolution for single-slot properties (Role:,
    ExecutionRoleArn:, TaskRoleArn:, an event source's Queue:/Bucket:/
    Stream:/Topic:) where exactly one target is meaningful. Takes the first
    of _resolve_targets_multi's results; a multi-var Sub in one of these
    slots is a punt (a role attribute names exactly one role, so "first
    match" is the only sane single-target reading)."""
    targets = _resolve_targets_multi(value, logical_ids, id_prefix)
    return targets[0] if targets else None


def _iter_ref_targets(value, logical_ids):
    """Recursively yield internal logical ids referenced anywhere within a
    nested Properties value (dict/list), for the generic "references" scan."""
    if isinstance(value, dict):
        for lid in _extract_ref_names(value):
            if lid in logical_ids:
                yield lid
        for v in value.values():
            yield from _iter_ref_targets(v, logical_ids)
    elif isinstance(value, list):
        for v in value:
            yield from _iter_ref_targets(v, logical_ids)
    elif isinstance(value, str):
        for lid in _extract_ref_names(value):
            if lid in logical_ids:
                yield lid


def _managed_policy_label(value, logical_ids, id_prefix):
    """policy_arn-shaped value -> "managed:<name>". A literal ARN uses its
    last path segment; a Ref/GetAtt to a customer-managed policy resource
    falls back to that resource's logical id (real ARN unknown statically)."""
    if isinstance(value, str):
        s = value.strip()
        m = REF_TAG_RE.match(s) or GETATT_TAG_RE.match(s)
        if m:
            return f"managed:{m.group(1)}"
        return f"managed:{s.rsplit('/', 1)[-1]}" if s else None
    if isinstance(value, dict):
        if isinstance(value.get("Ref"), str):
            return f"managed:{value['Ref']}"
        if "Fn::GetAtt" in value:
            g = value["Fn::GetAtt"]
            lid = (g[0] if isinstance(g, list) and g
                   else g.split(".", 1)[0] if isinstance(g, str) else None)
            return f"managed:{lid}" if lid else None
    return None


def _collect_role_grants(role, props, text, block_start, block_end, logical_ids, id_prefix):
    """Populate role['grants'] from inline Policies + ManagedPolicyArns.
    Returns [(on, line), ...] in the same order as role['grants'], used by
    the caller to attach file:line evidence to the connections it derives."""
    evidence = []
    # Scope the Effect: scan to the Policies: sub-block only -- the role's
    # own AssumeRolePolicyDocument (trust policy, not a grant) also has
    # Effect: statements earlier in the block and must not shift alignment.
    pol_block = _key_block(text, "Policies", block_start)
    if pol_block and pol_block[1] < block_end:
        pol_start, pol_end = pol_block[1], min(pol_block[2], block_end)
    else:
        pol_start, pol_end = block_start, block_start
    stmt_starts = [m.start() for m in STATEMENT_START_RE.finditer(text, pol_start, pol_end)]
    stmt_i = 0
    for policy in _as_list(props.get("Policies")):
        if not isinstance(policy, dict):
            continue
        doc = policy.get("PolicyDocument")
        if not isinstance(doc, dict):
            continue
        for st in _as_list(doc.get("Statement")):
            if not isinstance(st, dict):
                continue
            line = (_line_at(text, stmt_starts[stmt_i]) if stmt_i < len(stmt_starts)
                    else _line_at(text, block_start))
            stmt_i += 1
            actions = _as_list(st.get("Action"))
            for rv in _as_list(st.get("Resource")):
                # a multi-var Sub ("${A.Arn}-${B.Arn}") names more than one
                # internal resource in a single Resource entry -- one grant
                # (and one connection downstream) per resolved target.
                for on in _resolve_targets_multi(rv, logical_ids, id_prefix):
                    role["grants"].append({"actions": actions, "on": on})
                    evidence.append((on, line))
    for arn in _as_list(props.get("ManagedPolicyArns")):
        label = _managed_policy_label(arn, logical_ids, id_prefix)
        if label:
            role["grants"].append({"actions": ["managed"], "on": label})
            evidence.append((label, _line_at(text, block_start)))
    return evidence


def _collect_sam_policies(role, policies_val, text, block_start, block_end, logical_ids, id_prefix):
    """SAM implicit-role Policies: -- best-effort. A plain string names an
    AWS-managed policy or a SAM policy template (no params, so no target);
    a {TemplateName: {params}} dict is a SAM policy template with params,
    scanned for a Ref/GetAtt to resolve its "on"; a full inline Statement/
    PolicyDocument list is handled the same as a real IAM role's Policies."""
    evidence = []
    line = _line_at(text, block_start)
    for item in _as_list(policies_val):
        if isinstance(item, str):
            role["grants"].append({"actions": [f"sam:{item}"], "on": None})
            evidence.append((None, line))
        elif isinstance(item, dict):
            if "Statement" in item or "PolicyDocument" in item:
                doc = item.get("PolicyDocument", item)
                for st in _as_list(doc.get("Statement")):
                    if not isinstance(st, dict):
                        continue
                    actions = _as_list(st.get("Action"))
                    for rv in _as_list(st.get("Resource")):
                        for on in _resolve_targets_multi(rv, logical_ids, id_prefix):
                            role["grants"].append({"actions": actions, "on": on})
                            evidence.append((on, line))
            else:
                for tname, params in item.items():
                    target = None
                    if isinstance(params, dict):
                        for pv in params.values():
                            t = _resolve_target(pv, logical_ids, id_prefix)
                            if t and t.startswith(id_prefix):
                                target = t
                                break
                    role["grants"].append({"actions": [f"sam:{tname}"], "on": target})
                    evidence.append((target, line))
    return evidence


def _event_source_target(ev_body, logical_ids, id_prefix):
    if not isinstance(ev_body, dict):
        return None
    prop_key = EVENT_SOURCE_PROP.get(ev_body.get("Type"))
    if not prop_key:
        return None
    props = ev_body.get("Properties")
    if not isinstance(props, dict) or prop_key not in props:
        return None
    target = _resolve_target(props[prop_key], logical_ids, id_prefix)
    return target if target and target.startswith(id_prefix) else None


def parse_cfn_doc(parsed: dict, text: str, filename: str) -> dict:
    id_prefix = f"cfn:{filename}::"
    resources_section = parsed.get("Resources")
    if not isinstance(resources_section, dict):
        resources_section = {}
    logical_ids = set(resources_section.keys())

    resources = []
    roles_by_id = {}
    grant_evidence = {}       # role_id -> [(on, line), ...]
    implicit_roles = {}       # role_id -> role dict (SAM Policies: implicit roles)
    implicit_evidence = {}    # role_id -> [(on, line), ...]
    fn_events = []            # (fn_id, target_id, line)
    lines_by_logical = {}

    for logical_id, body in resources_section.items():
        if not isinstance(body, dict):
            continue
        rtype = body.get("Type") or ""
        block = _key_block(text, logical_id)
        line = block[0] if block else _find_key_line(text, logical_id)
        block_start, block_end = (block[1], block[2]) if block else (0, len(text))
        lines_by_logical[logical_id] = line
        props = body.get("Properties")
        if not isinstance(props, dict):
            props = {}

        rid = id_prefix + logical_id
        # An IAM role is a resource box too (mirrors the Terraform parser,
        # where aws_iam_role appears in both `resources` and `roles`) --
        # only its inline policy plumbing is excluded from being a box.
        resources.append({"id": rid, "rtype": rtype, "name": logical_id,
                           "source_file": filename, "line": line, "attrs": {}})

        if rtype in CFN_ROLE_TYPES:
            role = {"id": rid, "name": logical_id, "grants": [],
                    "attached_to": [], "source_file": filename, "line": line}
            grant_evidence[role["id"]] = _collect_role_grants(
                role, props, text, block_start, block_end, logical_ids, id_prefix)
            roles_by_id[role["id"]] = role
            continue

        if rtype == SAM_FUNCTION_TYPE:
            has_explicit_role = any(k in props for k in ROLE_ATTR_KEYS)
            if not has_explicit_role and props.get("Policies"):
                iname = f"{logical_id}Role (implicit)"
                irid = id_prefix + iname
                irole = {"id": irid, "name": iname, "grants": [],
                          "attached_to": [rid], "source_file": filename, "line": line}
                implicit_evidence[irid] = _collect_sam_policies(
                    irole, props["Policies"], text, block_start, block_end, logical_ids, id_prefix)
                implicit_roles[irid] = irole
            events = props.get("Events")
            if isinstance(events, dict):
                for ev_name, ev_body in events.items():
                    target = _event_source_target(ev_body, logical_ids, id_prefix)
                    if target:
                        eblock = _key_block(text, ev_name, block_start)
                        eline = eblock[0] if eblock else line
                        fn_events.append((rid, target, eline))

    resources_by_id = {r["id"]: r for r in resources}

    # role attachments: Role / ExecutionRoleArn / TaskRoleArn on non-role resources
    for logical_id, body in resources_section.items():
        if not isinstance(body, dict):
            continue
        rtype = body.get("Type") or ""
        if rtype in CFN_ROLE_TYPES:
            continue
        props = body.get("Properties")
        if not isinstance(props, dict):
            continue
        block = _key_block(text, logical_id)
        block_start = block[1] if block else 0
        rid = id_prefix + logical_id
        for key in ROLE_ATTR_KEYS:
            if key not in props:
                continue
            target = _resolve_target(props[key], logical_ids, id_prefix)
            if target in roles_by_id and rid not in roles_by_id[target]["attached_to"]:
                roles_by_id[target]["attached_to"].append(rid)

    connections = []
    for role_id, role in roles_by_id.items():
        for res_id in role["attached_to"]:
            for on, gline in grant_evidence.get(role_id, []):
                if on not in resources_by_id:
                    continue
                connections.append({
                    "src": res_id, "dst": on, "mechanism": "via role",
                    "role": role["name"], "evidence": f"{filename}:{gline}",
                })
    for irid, irole in implicit_roles.items():
        fn_id = irole["attached_to"][0]
        for on, gline in implicit_evidence.get(irid, []):
            if on not in resources_by_id:
                continue
            connections.append({
                "src": fn_id, "dst": on, "mechanism": "via role",
                "role": irole["name"], "evidence": f"{filename}:{gline}",
            })
    for fn_id, target, line in fn_events:
        if target in resources_by_id:
            connections.append({
                "src": target, "dst": fn_id, "mechanism": "event source",
                "role": None, "evidence": f"{filename}:{line}",
            })

    # generic Ref/GetAtt/Sub references between non-IAM resources (role
    # attachment and event-source keys already produce their own connections
    # above, so they're excluded here to avoid a redundant duplicate edge).
    for logical_id, body in resources_section.items():
        if not isinstance(body, dict):
            continue
        rtype = body.get("Type") or ""
        if rtype in CFN_ROLE_TYPES:
            continue
        rid = id_prefix + logical_id
        props = body.get("Properties")
        if not isinstance(props, dict):
            continue
        line = lines_by_logical[logical_id]
        seen = set()
        for key, val in props.items():
            if key in EXCLUDE_REF_KEYS:
                continue
            for lid in _iter_ref_targets(val, logical_ids):
                tid = id_prefix + lid
                if tid == rid or tid not in resources_by_id or tid in seen:
                    continue
                seen.add(tid)
                connections.append({
                    "src": rid, "dst": tid, "mechanism": "references",
                    "role": None, "evidence": f"{filename}:{line}",
                })

    resources.sort(key=lambda r: r["line"])
    all_roles = sorted(list(roles_by_id.values()) + list(implicit_roles.values()), key=lambda r: r["line"])
    connections.sort(key=lambda c: (c["src"], c["dst"], c["mechanism"]))
    return {"resources": resources, "roles": all_roles, "connections": connections, "deployed": {}}


def parse_serverless_doc(parsed: dict, text: str, filename: str) -> dict:
    id_prefix = f"sls:{filename}::"
    functions = parsed.get("functions") or {}
    provider = parsed.get("provider") or {}

    # An embedded `resources.Resources` block is plain CFN -- reuse the CFN
    # parser for it directly rather than re-implementing statement/ref
    # handling. Its ids share the same "cfn:<file>::" prefix as a standalone
    # template would use in this file, so they can't collide with the sls: ids.
    embedded_section = None
    res_block = parsed.get("resources")
    if isinstance(res_block, dict) and isinstance(res_block.get("Resources"), dict):
        embedded_section = res_block["Resources"]
    embedded = (parse_cfn_doc({"Resources": embedded_section}, text, filename)
                if embedded_section else empty_model())
    embedded_logical_ids = set(embedded_section.keys()) if embedded_section else set()
    cfn_prefix = f"cfn:{filename}::"

    resources = list(embedded["resources"])
    roles = list(embedded["roles"])
    connections = list(embedded["connections"])
    resources_by_id = {r["id"]: r for r in resources}

    fn_lines = {}
    for name, body in functions.items():
        block = _key_block(text, name)
        line = block[0] if block else _find_key_line(text, name)
        fn_lines[name] = line
        resources.append({"id": id_prefix + name, "rtype": "serverless function",
                           "name": name, "source_file": filename, "line": line, "attrs": {}})

    statements = None
    if isinstance(provider.get("iam"), dict):
        role_block = provider["iam"].get("role")
        if isinstance(role_block, dict):
            statements = role_block.get("statements")
    if statements is None:
        statements = provider.get("iamRoleStatements")

    if statements:
        pblock = _key_block(text, "provider")
        pstart, pend = (pblock[1], pblock[2]) if pblock else (0, len(text))
        role = {"id": id_prefix + "provider", "name": "serverless (provider)", "grants": [],
                "attached_to": sorted(id_prefix + n for n, body in functions.items()
                                       if not (isinstance(body, dict) and "role" in body)),
                "source_file": filename, "line": pblock[0] if pblock else 1}
        for st in _as_list(statements):
            if not isinstance(st, dict):
                continue
            actions = _as_list(st.get("Action"))
            for rv in _as_list(st.get("Resource")):
                for on in _resolve_targets_multi(rv, embedded_logical_ids, cfn_prefix):
                    role["grants"].append({"actions": actions, "on": on})
        roles.append(role)

    # function events -- best-effort, only resolves against the embedded
    # resources.Resources block (serverless.yml has no other notion of an
    # "internal" resource); a literal ARN produces no connection.
    for name, body in functions.items():
        if not isinstance(body, dict):
            continue
        fid = id_prefix + name
        for ev in _as_list(body.get("events")):
            if not isinstance(ev, dict):
                continue
            for etype, edata in ev.items():
                if etype == "s3":
                    val = edata.get("bucket") if isinstance(edata, dict) else edata
                elif etype in ("sqs", "stream", "sns"):
                    val = edata.get("arn") if isinstance(edata, dict) else edata
                else:
                    continue
                target = _resolve_target(val, embedded_logical_ids, cfn_prefix)
                if target and target in resources_by_id:
                    connections.append({
                        "src": target, "dst": fid, "mechanism": "event source",
                        "role": None, "evidence": f"{filename}:{fn_lines[name]}",
                    })

    resources.sort(key=lambda r: r["line"])
    roles.sort(key=lambda r: r["line"])
    connections.sort(key=lambda c: (c["src"], c["dst"], c["mechanism"]))
    return {"resources": resources, "roles": roles, "connections": connections, "deployed": {}}
