import re
from lib.iac import _line_at, _match_pair

# ---------------------------------------------------------------------------
# AWS CDK best-effort source scanner (TypeScript + Python).
#
# Not a real TS/Python parser -- regex over `new <Construct>(this, "Id", ...)`
# / `<Construct>(self, "Id", ...)` call shapes, good enough to recognize the
# common construct families and the grant/role wiring real stacks use. Every
# resource here carries attrs={"confidence": "best-effort"} so the renderer
# (and anyone reading the model) never mistakes this for the same certainty
# as the Terraform/CFN/compose/k8s parsers.
#
# ID scheme (per the system-view-v2 plan, task 4), following the file-
# qualified convention set by lib/iac_cfn.py and lib/iac_k8s.py:
#   cdk construct        : "cdk:<relative_file_path>::<ConstructId>"
#   cdk implicit role     : "cdk:<relative_file_path>::role/<grantee ConstructId> (implicit)"
#     -- created mechanically by a `.grantX(...)` call; distinct from an
#     explicit `new iam.Role(...)` construct, which keeps its own plain
#     "cdk:<file>::<ConstructId>" id in both `resources` and `roles` (same
#     dual-membership convention as Terraform's aws_iam_role).
# ---------------------------------------------------------------------------

MAX_BYTES = 200_000

CDK_TS_MARKER = "aws-cdk-lib"
CDK_PY_IMPORT_RE = re.compile(r'(?m)^\s*(?:import\s+aws_cdk\b|from\s+aws_cdk\b)')

# Construct class suffix -> resource family. Best-effort and deliberately
# small: only families with an unambiguous, common suffix are recognized: an
# unrecognized suffix is skipped entirely rather than guessed at (never
# invent a resource).
FAMILY_SUFFIXES = {
    "Function": "lambda",
    "Table": "dynamodb",
    "Bucket": "s3",
    "Queue": "sqs",
    "Topic": "sns",
    "Role": "iam role",
    "Cluster": "ecs",
    "FargateService": "ecs",
    "Ec2Service": "ecs",
    "RestApi": "apigateway",
    "HttpApi": "apigateway",
    "Secret": "secretsmanager",
    "LogGroup": "logs",
}

# grantX method name (normalized: lowercased, underscores stripped, so both
# TS grantReadWriteData and Python grant_read_write_data hit the same key)
# -> a short verb used to build the implicit role's synthesized action
# string "<target family>:<verb> (cdk)". An unrecognized grant method still
# produces a connection (it was directly observed in source) but falls back
# to the generic verb "access" rather than inventing a specific one.
GRANT_VERBS = {
    "grantreaddata": "read",
    "grantwritedata": "write",
    "grantreadwritedata": "readwrite",
    "grantread": "read",
    "grantwrite": "write",
    "grantconsumemessages": "consume",
    "grantsendmessages": "send",
    "grantinvoke": "invoke",
    "grantput": "put",
}

# `new <Class.Path>(this, "Id", ...)` with an optional `const x =` / `let x =`
# / `var x =` binding captured as `var`.
TS_CONSTRUCT_RE = re.compile(
    r'(?:(?:const|let|var)\s+(?P<var>[A-Za-z_$][\w$]*)\s*=\s*)?'
    r'new\s+(?P<cls>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)\s*'
    r'\(\s*this\s*,\s*[\'"](?P<cid>[^\'"]+)[\'"]'
)
# `<Class.Path>(self, "Id", ...)` with an optional `x =` binding. Requires a
# dotted class path (module.Construct) so this never matches an ordinary
# method definition like `def __init__(self, ...)`.
PY_CONSTRUCT_RE = re.compile(
    r'(?:(?P<var>[A-Za-z_][\w]*)\s*=\s*)?'
    r'(?P<cls>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+)\s*'
    r'\(\s*self\s*,\s*[\'"](?P<cid>[^\'"]+)[\'"]'
)

# `<target>.grantX(<grantee>)` -- shared shape across TS (camelCase) and
# Python (snake_case): both are `var.grant...(var...)` call syntax.
GRANT_CALL_RE = re.compile(
    r'\b(?P<target>[A-Za-z_$][\w$]*)\.(?P<method>grant[A-Za-z_]+)\(\s*(?P<grantee>[A-Za-z_$][\w$]*)'
)
# `role: <expr>` (TS object literal) / `role=<expr>` (Python kwarg) inside a
# construct's own call body.
ROLE_PROP_RE = re.compile(r'\brole\s*[:=]\s*(?P<val>[A-Za-z_$][\w$.]*)')
# `<role>.addToPolicy(` / `<role>.add_to_policy(`
ADD_POLICY_RE = re.compile(r'\b(?P<role>[A-Za-z_$][\w$]*)\.(?:addToPolicy|add_to_policy)\s*\(')
ACTIONS_LIST_RE = re.compile(r'\bactions\s*[:=]\s*\[([^\]]*)\]')
RESOURCES_LIST_RE = re.compile(r'\bresources\s*[:=]\s*\[([^\]]*)\]')
QUOTED_RE = re.compile(r'[\'"]([^\'"]+)[\'"]')


def is_cdk_ts(text: str) -> bool:
    """Cheap gate: does this .ts file import aws-cdk-lib at all (root or any
    submodule, e.g. 'aws-cdk-lib/aws-lambda')? Run before regex-scanning the
    whole file so an unrelated TypeScript file is never mistaken for a CDK
    stack just because it happens to call `new X(this, "id", ...)`."""
    return CDK_TS_MARKER in text


def is_cdk_py(text: str) -> bool:
    """Cheap gate: does this .py file `import aws_cdk` / `from aws_cdk
    import ...` anywhere at statement level?"""
    return bool(CDK_PY_IMPORT_RE.search(text))


def parse_cdk_ts(text: str, filename: str) -> dict:
    return _parse_cdk(text, filename, TS_CONSTRUCT_RE)


def parse_cdk_py(text: str, filename: str) -> dict:
    return _parse_cdk(text, filename, PY_CONSTRUCT_RE)


def _parse_cdk(text: str, filename: str, construct_re: re.Pattern) -> dict:
    id_prefix = f"cdk:{filename}::"

    blocks = []
    for m in construct_re.finditer(text):
        cls = m.group("cls")
        last = cls.rsplit(".", 1)[-1]
        family = FAMILY_SUFFIXES.get(last)
        if family is None:
            continue  # unrecognized construct family: never invent a resource
        open_idx = text.find("(", m.end("cls"))
        if open_idx == -1:
            continue
        close_idx = _match_pair(text, open_idx, "(", ")")
        blocks.append({
            "id": id_prefix + m.group("cid"), "cid": m.group("cid"),
            "family": family, "var": m.group("var"),
            "line": _line_at(text, m.start()),
            "body_start": open_idx + 1, "body_end": close_idx,
        })

    resources = [
        {"id": b["id"], "rtype": f"cdk {b['family']}", "name": b["cid"],
         "source_file": filename, "line": b["line"], "attrs": {"confidence": "best-effort"}}
        for b in blocks
    ]
    resources_by_id = {r["id"]: r for r in resources}
    var_to_id = {b["var"]: b["id"] for b in blocks if b["var"]}

    roles_by_id = {
        b["id"]: {"id": b["id"], "name": b["cid"], "grants": [], "attached_to": [],
                   "source_file": filename, "line": b["line"]}
        for b in blocks if b["family"] == "iam role"
    }

    # `role:` / `role=` prop on a non-Role construct -> attach that
    # construct to the referenced explicit Role's attached_to.
    for b in blocks:
        if b["family"] == "iam role":
            continue
        rm = ROLE_PROP_RE.search(text, b["body_start"], b["body_end"])
        if not rm:
            continue
        base_var = rm.group("val").split(".", 1)[0]
        role = roles_by_id.get(var_to_id.get(base_var))
        if not role:
            continue
        if b["id"] not in role["attached_to"]:
            role["attached_to"].append(b["id"])

    connections = []
    implicit_roles = {}

    # `<target>.grantX(<grantee>)` -> connection + implicit role on the grantee
    for m in GRANT_CALL_RE.finditer(text):
        target_id = var_to_id.get(m.group("target"))
        grantee_id = var_to_id.get(m.group("grantee"))
        if target_id not in resources_by_id or grantee_id not in resources_by_id:
            continue  # never guess at an unresolved var
        method = m.group("method")
        line = _line_at(text, m.start())
        connections.append({
            "src": grantee_id, "dst": target_id,
            "mechanism": f"cdk grant: {method}", "role": None,
            "evidence": f"{filename}:{line}",
        })

        verb = GRANT_VERBS.get(method.lower().replace("_", ""), "access")
        target_family = resources_by_id[target_id]["rtype"][len("cdk "):]
        grantee_cid = resources_by_id[grantee_id]["name"]
        role_id = f"{id_prefix}role/{grantee_cid} (implicit)"
        irole = implicit_roles.setdefault(role_id, {
            "id": role_id, "name": f"{grantee_cid} (implicit)", "grants": [],
            "attached_to": [], "source_file": filename, "line": line,
        })
        if grantee_id not in irole["attached_to"]:
            irole["attached_to"].append(grantee_id)
        irole["grants"].append({"actions": [f"{target_family}:{verb} (cdk)"], "on": target_id})

    # `<role>.addToPolicy(new iam.PolicyStatement({actions: [...], resources: [...]}))`
    # -> grants on the explicit role + "via role" connections from whatever
    # was attached to it via a `role:` prop, resolving `resources:` entries
    # that reference a construct var (e.g. queue.queueArn) back to that
    # construct's resource id. A literal ARN string can't be resolved to a
    # box, so it's skipped rather than guessed at.
    for m in ADD_POLICY_RE.finditer(text):
        role = roles_by_id.get(var_to_id.get(m.group("role")))
        if not role:
            continue
        open_idx = m.end() - 1
        close_idx = _match_pair(text, open_idx, "(", ")")
        segment = text[open_idx:close_idx + 1]
        line = _line_at(text, m.start())

        actions_m = ACTIONS_LIST_RE.search(segment)
        actions = QUOTED_RE.findall(actions_m.group(1)) if actions_m else []

        targets = []
        resources_m = RESOURCES_LIST_RE.search(segment)
        if resources_m:
            for item in resources_m.group(1).split(","):
                item = item.strip()
                if not item or item[0] in "'\"":
                    continue  # literal ARN string: not resolvable to a box
                base = item.split(".", 1)[0]
                tid = var_to_id.get(base)
                if tid in resources_by_id:
                    targets.append(tid)

        for tid in targets:
            role["grants"].append({"actions": actions, "on": tid})
            for attached in role["attached_to"]:
                connections.append({
                    "src": attached, "dst": tid, "mechanism": "via role",
                    "role": role["name"], "evidence": f"{filename}:{line}",
                })

    resources.sort(key=lambda r: r["line"])
    roles = sorted(list(roles_by_id.values()) + list(implicit_roles.values()), key=lambda r: r["line"])
    connections.sort(key=lambda c: (c["src"], c["dst"], c["mechanism"]))
    return {"resources": resources, "roles": roles, "connections": connections, "deployed": {}}
