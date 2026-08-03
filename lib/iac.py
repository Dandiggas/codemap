import json
import re
from pathlib import Path, PurePosixPath
from lib.scan import EXCLUDES

SKIP_DIRS = EXCLUDES | {".terraform"}
CFN_SUFFIXES = (".yaml", ".yml", ".json")

def empty_model() -> dict:
    return {"resources": [], "roles": [], "connections": [], "deployed": {}}

# ---------------------------------------------------------------------------
# group_of: the human-readable "source group" an id belongs to, used by the
# System tab to tell environments/stacks apart (a multi-env Terraform repo
# renders envs/prod and envs/dev on one canvas; without a group, nothing
# distinguishes their boxes -- see TestTerraformMultiEnvIds above for the
# id-uniqueness half of this reviewer repro).
#
#   tf   ids ("tf:<dirpath>::...")       -> the module directory ("envs/prod",
#                                            "." becomes "terraform" since a
#                                            bare "." reads as nothing in a chip)
#   cfn/sls/compose/k8s/cdk ids          -> the source file path, extension
#   ("<kind>:<relpath>::...")               stripped ("template.yaml" ->
#                                            "template", "k8s/app.yaml" ->
#                                            "k8s/app")
#
# Every parser's id already carries this qualifier (see the id-scheme note
# above parse_terraform_files, and the matching notes at the top of
# lib/iac_cfn.py / lib/iac_k8s.py / lib/iac_cdk.py); group_of only ever reads
# it back out, it never has to re-derive anything the parsers didn't already
# encode positionally.
# ---------------------------------------------------------------------------

def group_of(id: str) -> str:
    """Human-readable source group for a resource/role id, or "" if id isn't
    one of this module's qualified ids (defensive: never raises on odd input,
    since a bad group must not take a render down)."""
    if not id or "::" not in id:
        return ""
    prefix, _, _bare = id.partition("::")
    kind, sep, qualifier = prefix.partition(":")
    if not sep or not qualifier:
        return ""
    if kind == "tf":
        return "terraform" if qualifier == "." else qualifier
    return str(PurePosixPath(qualifier).with_suffix(""))

def scan_iac(root) -> dict:
    """Scan a repo root for IaC sources and return the normalized model.
    Dispatches to the Terraform parser, the CFN/SAM/serverless.yml parser
    (lib/iac_cfn.py), the docker-compose/k8s parser (lib/iac_k8s.py), and
    the best-effort CDK TS/Python source parser (lib/iac_cdk.py). Ids are
    prefixed per source kind so results from different parsers never
    collide when merged here -- see the id-scheme note at the top of
    lib/iac_cfn.py (compose/k8s ids are documented at the top of
    lib/iac_k8s.py, cdk ids at the top of lib/iac_cdk.py, Terraform ids
    just above parse_terraform_files below)."""
    from lib import iac_cfn, iac_k8s, iac_cdk  # deferred: all import from this module
    root = Path(root)
    model = empty_model()
    tf_files = sorted(
        p for p in root.rglob("*.tf")
        if not any(part in SKIP_DIRS for part in p.relative_to(root).parts[:-1])
    )
    # Terraform is parsed a DIRECTORY at a time, not a file at a time: a
    # directory is Terraform's module boundary, so sibling .tf files share one
    # address space (see the id-scheme note below parse_terraform_files).
    tf_by_dir = {}
    for f in tf_files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        rel = f.relative_to(root)
        tf_by_dir.setdefault(str(rel.parent), []).append((str(rel), text))
    for dirpath in sorted(tf_by_dir):
        partial = parse_terraform_files(tf_by_dir[dirpath])
        model["resources"].extend(partial["resources"])
        model["roles"].extend(partial["roles"])
        model["connections"].extend(partial["connections"])

    cfn_files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in CFN_SUFFIXES
        and not any(part in SKIP_DIRS for part in p.relative_to(root).parts[:-1])
    )
    for f in cfn_files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        rel = str(f.relative_to(root))
        # A parse failure here must not block the compose/k8s dispatch below:
        # a multi-doc k8s file parsed whole (ignoring its "---" boundaries)
        # is a garbled but harmless merge (see lib/iac_k8s.parse_doc's
        # docstring), and even a hard parse error on the whole file should
        # not stop iac_k8s from re-parsing it doc-by-doc, where one bad
        # document doesn't take down its siblings.
        try:
            parsed = json.loads(text) if f.suffix.lower() == ".json" else parse_yaml_lite(text)
        except Exception:
            parsed = None
        if isinstance(parsed, dict) and iac_cfn.is_serverless_doc(parsed):
            partial = iac_cfn.parse_serverless_doc(parsed, text, rel)
        elif isinstance(parsed, dict) and iac_cfn.is_cfn_doc(parsed):
            partial = iac_cfn.parse_cfn_doc(parsed, text, rel)
        elif f.suffix.lower() != ".json":
            partial = iac_k8s.parse_doc(f.name, rel, text, parsed if isinstance(parsed, dict) else {})
            if partial is None:
                continue
        else:
            continue
        model["resources"].extend(partial["resources"])
        model["roles"].extend(partial["roles"])
        model["connections"].extend(partial["connections"])

    cdk_files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in (".ts", ".py")
        and not any(part in SKIP_DIRS for part in p.relative_to(root).parts[:-1])
    )
    for f in cdk_files:
        try:
            if f.stat().st_size > iac_cdk.MAX_BYTES:
                continue
            text = f.read_text(errors="replace")
        except OSError:
            continue
        rel = str(f.relative_to(root))
        if f.suffix.lower() == ".ts" and iac_cdk.is_cdk_ts(text):
            partial = iac_cdk.parse_cdk_ts(text, rel)
        elif f.suffix.lower() == ".py" and iac_cdk.is_cdk_py(text):
            partial = iac_cdk.parse_cdk_py(text, rel)
        else:
            continue
        model["resources"].extend(partial["resources"])
        model["roles"].extend(partial["roles"])
        model["connections"].extend(partial["connections"])

    # group is a scan_iac-level concern, not a per-parser one: every parser's
    # own unit tests (parse_terraform_text, parse_cdk_ts, ...) keep asserting
    # bare model dicts, and only scan_iac's merged output is what the
    # template ever renders.
    for r in model["resources"]:
        r["group"] = group_of(r["id"])
    for r in model["roles"]:
        r["group"] = group_of(r["id"])
    return model

# ---------------------------------------------------------------------------
# Terraform (.tf) parser
# ---------------------------------------------------------------------------
# Not a full HCL parser: regex block-heads + balanced-delimiter scanning,
# good enough for the resource/role/policy/reference shapes real stacks use.

RESOURCE_RE = re.compile(r'^resource\s+"([A-Za-z0-9_]+)"\s+"([A-Za-z0-9_-]+)"\s*\{', re.M)
ROLE_ATTR_RE = re.compile(r'\brole\s*=\s*([^\n]+)')
ATTACH_ATTR_RE = re.compile(r'\b(role|role_arn|execution_role_arn|execution_role)\s*=\s*([^\n]+)')
STATEMENT_RE = re.compile(r'"?Statement"?\s*[:=]\s*\[')
ACTION_RE = re.compile(r'"?Action"?\s*[:=]\s*(\[[^\]]*\]|"[^"]*")', re.S)
# Unlike ACTION_RE, this does not special-case a `[...]` list value: a
# list-valued Resource (Resource = [a.arn, b.arn]) stops at the first comma,
# so only the first element is ever resolved. Known limitation, not fixed here.
RESOURCE_ATTR_RE = re.compile(r'"?Resource"?\s*[:=]\s*([^\n,]+)')
REF_RE = re.compile(r'(aws_[A-Za-z0-9_]+)\.([A-Za-z0-9_-]+)\.[A-Za-z0-9_]+')
POLICY_ARN_ATTR_RE = re.compile(r'\bpolicy_arn\s*=\s*([^\n]+)')
HEREDOC_RE = re.compile(r'<<-?\s*(\w+)\s*\n')
BRACE_RE = re.compile(r'\{')

# Resource types that represent IAM permission plumbing rather than an
# infrastructure box; they feed roles/grants, not the resources list.
POLICY_DOC_TYPES = {"aws_iam_role_policy", "aws_iam_policy", "aws_iam_role_policy_attachment"}
# Types skipped as *sources* of attachment/reference scanning (their bodies
# are policy documents / role definitions, not wiring to other resources).
IAM_BLOCK_TYPES = POLICY_DOC_TYPES | {"aws_iam_role"}
# Types skipped as reference *targets* for the generic "references" mechanism
# (handled instead via the "via role" mechanism).
IAM_TARGET_TYPES = IAM_BLOCK_TYPES

def parse_terraform_text(text: str, filename: str) -> dict:
    """One .tf file parsed as a module directory of its own. Thin wrapper over
    parse_terraform_files (the real entry point) for single-file callers."""
    return parse_terraform_files([(filename, text)])

# ---------------------------------------------------------------------------
# Terraform id scheme: "tf:<dirpath>::<type>.<name>", dirpath repo-relative
# ("." at the repo root) -- e.g. "tf:.::aws_s3_bucket.logs",
# "tf:envs/prod::aws_iam_role.app".
#
# A bare "<type>.<name>" is NOT unique across a repo: a multi-env layout
# (envs/prod/main.tf + envs/dev/main.tf) legitimately declares the same names
# in both, and unqualified ids collide -- the template indexes boxes by id, so
# a collision silently overwrites one box with the other, stacks them at one
# set of coordinates, and shows only the last-parsed file's grants for a role
# both environments call "app".
#
# The qualifier is the DIRECTORY, not the file, because a directory is
# Terraform's module boundary: main.tf and iam.tf in one folder are one module
# and address each other freely, so file-qualifying would break every
# legitimate cross-file reference. That's also why this function takes a whole
# directory's files at once rather than one file at a time.
#
# Only ids carry the prefix. Role display names stay bare ("app"), the same as
# every other parser here.
# ---------------------------------------------------------------------------

def parse_terraform_files(files) -> dict:
    """Parse one Terraform module directory.

    `files` is [(repo-relative path, text), ...] for the .tf files of a SINGLE
    directory (scan_iac groups them); every block shares that directory's id
    space while keeping its own source_file and line.
    """
    if not files:
        return empty_model()
    dirpath = _tf_dir(files[0][0])

    blocks = []
    seen_ids = set()
    for filename, text in files:
        for m in RESOURCE_RE.finditer(text):
            rtype, name = m.group(1), m.group(2)
            block_id = _tf_id(dirpath, f"{rtype}.{name}")
            if block_id in seen_ids:
                # Duplicate address inside one module is invalid Terraform
                # (`terraform validate` rejects it). Keep the first and move
                # on rather than emitting a colliding id downstream.
                continue
            seen_ids.add(block_id)
            open_pos = m.end() - 1
            close_pos = _match_pair(text, open_pos, "{", "}")
            blocks.append({
                "id": block_id, "rtype": rtype, "name": name,
                "file": filename, "text": text,
                "line": _line_at(text, m.start()),
                "body_start": open_pos + 1, "body_end": close_pos,
            })

    resources = [
        {"id": b["id"], "rtype": b["rtype"], "name": b["name"],
         "source_file": b["file"], "line": b["line"], "attrs": {}}
        for b in blocks if b["rtype"] not in POLICY_DOC_TYPES
    ]
    resources_by_id = {r["id"]: r for r in resources}

    roles_by_id = {
        b["id"]: {"id": b["id"], "name": b["name"], "grants": [],
                   "attached_to": [], "source_file": b["file"], "line": b["line"]}
        for b in blocks if b["rtype"] == "aws_iam_role"
    }

    # aws_iam_role_policy / aws_iam_policy -> grants on the role they target
    stmt_records = {}  # role_id -> [{"actions", "on", "line", "file"}, ...]
    for b in blocks:
        if b["rtype"] not in ("aws_iam_role_policy", "aws_iam_policy"):
            continue
        text = b["text"]
        role_m = ROLE_ATTR_RE.search(text, b["body_start"], b["body_end"])
        if not role_m:
            continue
        role_id = _tf_id(dirpath, _resolve_ref(role_m.group(1)))
        if role_id not in roles_by_id:
            continue
        obj = _find_policy_object(text, b["body_start"], b["body_end"])
        if not obj:
            continue
        statements = _extract_statements(text, obj[0], obj[1], dirpath)
        for st in statements:
            st["file"] = b["file"]
            roles_by_id[role_id]["grants"].append({"actions": st["actions"], "on": st["on"]})
        stmt_records.setdefault(role_id, []).extend(statements)

    # aws_iam_role_policy_attachment -> managed-policy grant on the role
    # (the most common real-world grant shape; without this a role attached
    # only via a managed policy shows empty grants, indistinguishable from
    # having no permissions at all)
    for b in blocks:
        if b["rtype"] != "aws_iam_role_policy_attachment":
            continue
        text = b["text"]
        role_m = ROLE_ATTR_RE.search(text, b["body_start"], b["body_end"])
        if not role_m:
            continue
        role_id = _tf_id(dirpath, _resolve_ref(role_m.group(1)))
        if role_id not in roles_by_id:
            continue
        arn_m = POLICY_ARN_ATTR_RE.search(text, b["body_start"], b["body_end"])
        if not arn_m:
            continue
        label = _managed_policy_label(arn_m.group(1))
        if label:
            roles_by_id[role_id]["grants"].append({"actions": ["managed"], "on": label})

    # role attachments (role=, role_arn=, execution_role[_arn]=) on non-IAM resources
    attachments = []  # (resource_id, role_id, line)
    for b in blocks:
        if b["rtype"] in IAM_BLOCK_TYPES:
            continue
        text = b["text"]
        for am in ATTACH_ATTR_RE.finditer(text, b["body_start"], b["body_end"]):
            role_id = _tf_id(dirpath, _resolve_ref(am.group(2)))
            if role_id not in roles_by_id:
                continue
            if b["id"] not in roles_by_id[role_id]["attached_to"]:
                roles_by_id[role_id]["attached_to"].append(b["id"])
            attachments.append((b["id"], role_id, _line_at(text, am.start())))

    connections = []
    for res_id, role_id, _attach_line in attachments:
        role = roles_by_id[role_id]
        for st in stmt_records.get(role_id, []):
            if st["on"] not in resources_by_id:
                continue
            connections.append({
                "src": res_id, "dst": st["on"], "mechanism": "via role",
                "role": role["name"], "evidence": f"{st['file']}:{st['line']}",
            })

    # direct inter-resource references (${type.name.attr} or bare type.name.attr)
    for b in blocks:
        if b["rtype"] in IAM_BLOCK_TYPES:
            continue
        text = b["text"]
        for rm in REF_RE.finditer(text, b["body_start"], b["body_end"]):
            if rm.group(1) in IAM_TARGET_TYPES:
                continue
            target_id = _tf_id(dirpath, f"{rm.group(1)}.{rm.group(2)}")
            if target_id == b["id"] or target_id not in resources_by_id:
                continue
            connections.append({
                "src": b["id"], "dst": target_id, "mechanism": "references",
                "role": None, "evidence": f"{b['file']}:{_line_at(text, rm.start())}",
            })

    resources.sort(key=lambda r: (r["source_file"], r["line"]))
    roles = sorted(roles_by_id.values(), key=lambda r: (r["source_file"], r["line"]))
    connections.sort(key=lambda c: (c["src"], c["dst"], c["mechanism"]))
    return {"resources": resources, "roles": roles, "connections": connections, "deployed": {}}

def _tf_dir(filename):
    """Repo-relative directory of a .tf file, "." at the repo root."""
    return str(PurePosixPath(filename).parent)

def _tf_id(dirpath, bare):
    """Qualify a bare Terraform address ("<type>.<name>") with its module
    directory. None in -> None out, so an unresolved reference stays
    unresolved instead of becoming a plausible-looking id."""
    return f"tf:{dirpath}::{bare}" if bare else None

def _line_at(text, offset):
    return text.count("\n", 0, offset) + 1

def _resolve_ref(expr):
    """The BARE "<type>.<name>" a reference expression points at, or None.
    Callers qualify it with _tf_id where an id is wanted; _managed_policy_label
    deliberately wants the bare form."""
    m = REF_RE.search(expr)
    return f"{m.group(1)}.{m.group(2)}" if m else None

def _managed_policy_label(raw):
    """policy_arn value -> "managed:<name>". Handles a literal ARN string
    (AWS or customer managed) directly; a data-source/resource reference
    (e.g. data.aws_iam_policy.x.arn) falls back best-effort to that
    reference's local name, since the real ARN isn't known statically."""
    raw = raw.strip().rstrip(",").strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        arn = raw[1:-1]
        return f"managed:{arn.rsplit('/', 1)[-1]}" if arn else None
    ref = _resolve_ref(raw)
    return f"managed:{ref.split('.', 1)[1]}" if ref else None

def _match_pair(text, open_pos, open_ch, close_ch):
    """Index of the delimiter matching text[open_pos], skipping quoted strings
    and heredoc bodies (a heredoc's content is opaque to depth counting,
    exactly like a quoted string — an unbalanced brace inside a user_data
    heredoc must not leak into the enclosing block's depth)."""
    depth = 0
    i = open_pos
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                i += 1
        elif c == "<" and text[i:i + 2] == "<<":
            hm = HEREDOC_RE.match(text, i)
            if hm:
                marker = hm.group(1)
                end_re = re.compile(r"^[ \t]*" + re.escape(marker) + r"[ \t]*$", re.M)
                em = end_re.search(text, hm.end())
                i = em.end() if em else n
                continue
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n - 1  # unterminated: best-effort

def _find_policy_object(text, start, end):
    """Locate a policy document object within [start, end): jsonencode({...})
    or a heredoc whose body is (or contains) a JSON-ish {...} object.
    Returns (obj_start, obj_end) or None."""
    idx = text.find("jsonencode(", start, end)
    if idx != -1:
        open_paren = idx + len("jsonencode(") - 1
        close_paren = _match_pair(text, open_paren, "(", ")")
        obj_start = text.find("{", open_paren, close_paren)
        if obj_start != -1:
            return obj_start, _match_pair(text, obj_start, "{", "}") + 1

    hm = HEREDOC_RE.search(text, start, end)
    if hm:
        marker = hm.group(1)
        content_start = hm.end()
        end_re = re.compile(r"^[ \t]*" + re.escape(marker) + r"[ \t]*$", re.M)
        em = end_re.search(text, content_start, end)
        content_end = em.start() if em else end
        obj_start = text.find("{", content_start, content_end)
        if obj_start != -1:
            return obj_start, _match_pair(text, obj_start, "{", "}") + 1
    return None

def _extract_statements(text, obj_start, obj_end, dirpath):
    sm = STATEMENT_RE.search(text, obj_start, obj_end)
    if not sm:
        return []
    list_open = sm.end() - 1
    list_close = _match_pair(text, list_open, "[", "]")
    statements = []
    pos = list_open + 1
    while True:
        om = BRACE_RE.search(text, pos, list_close)
        if not om:
            break
        obj_s = om.start()
        obj_e = _match_pair(text, obj_s, "{", "}")
        statements.append(_parse_statement(text, obj_s, obj_e, dirpath))
        pos = obj_e + 1
    return statements

def _parse_statement(text, s, e, dirpath):
    seg = text[s:e]
    actions = []
    am = ACTION_RE.search(seg)
    if am:
        val = am.group(1)
        if val.startswith("["):
            actions = re.findall(r'"([^"]*)"', val)
        else:
            actions = [val.strip('"')]
    on = None
    rm = RESOURCE_ATTR_RE.search(seg)
    if rm:
        raw = rm.group(1).strip().rstrip(",").strip()
        # an in-repo reference becomes a module-qualified id; anything else
        # (a literal ARN, a wildcard) stays the literal string it is
        on = _tf_id(dirpath, _resolve_ref(raw)) or raw.strip('"')
    return {"actions": actions, "on": on, "line": _line_at(text, s)}

# ---------------------------------------------------------------------------
# YAML-lite reader: an indentation-tree subset (mappings, lists, scalars,
# block scalars) adequate for CloudFormation/k8s/compose documents. No
# anchors/aliases, no flow collections ([a, b] / {a: b}) — those fall back
# to being read as plain scalar strings.
# ---------------------------------------------------------------------------

_BLOCK_STYLES = ("|-", "|+", "|", ">-", ">+", ">")
# The key-terminating colon is the first one followed by whitespace or
# end-of-line (real YAML semantics) -- NOT just the first colon character.
# A lazy `.*?` for the key (rather than excluding ":" outright) is required
# so keys with an embedded colon, like CloudFormation's long-form intrinsic
# names ("Fn::Sub", "Fn::GetAtt"), parse as keys instead of being silently
# dropped (a real bug this fixes, not just a style choice).
_MAP_ENTRY_RE = re.compile(r'^(?:"[^"]*"|\'[^\']*\'|[^#\s].*?)\s*:(\s|$)')

def parse_yaml_lite(text: str) -> dict:
    lines = text.split("\n")
    cur = _Cursor(lines)
    if cur.peek() is None:
        return {}
    return _parse_value_at(cur, _indent(cur.peek()))

class _Cursor:
    def __init__(self, lines):
        self.lines = lines
        self.i = 0

    def peek(self):
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            stripped = raw.strip()
            if stripped == "" or stripped.startswith("#") or stripped == "---":
                self.i += 1
                continue
            return raw
        return None

    def take(self):
        raw = self.peek()
        self.i += 1
        return raw

def _indent(line):
    return len(line) - len(line.lstrip(" "))

def _parse_value_at(cur, indent):
    line = cur.peek()
    if line is None or _indent(line) < indent:
        return None
    if line.strip().startswith("- "):
        return _parse_list(cur, indent)
    if line.strip() == "-":
        return _parse_list(cur, indent)
    return _parse_map(cur, indent)

def _parse_map(cur, indent, first_pair=None):
    result = {}
    if first_pair:
        result[first_pair[0]] = first_pair[1]
    while True:
        line = cur.peek()
        if line is None or _indent(line) != indent:
            break
        stripped = line.strip()
        if stripped.startswith("-"):
            break
        if not _MAP_ENTRY_RE.match(stripped):
            break
        cur.take()
        key, rest = _split_key_value(stripped)
        result[key] = _parse_after_colon(cur, indent, rest)
    return result

def _parse_list(cur, indent):
    items = []
    while True:
        line = cur.peek()
        if line is None or _indent(line) != indent or not line.strip().startswith("-"):
            break
        cur.take()
        after_dash = line.strip()[1:]
        content = after_dash.lstrip(" ")
        item_indent = indent + (len(after_dash) - len(content)) + 1
        if content == "":
            nxt = cur.peek()
            if nxt is not None and _indent(nxt) > indent:
                items.append(_parse_value_at(cur, _indent(nxt)))
            else:
                items.append(None)
        elif _MAP_ENTRY_RE.match(content):
            key, rest = _split_key_value(content)
            val = _parse_after_colon(cur, item_indent, rest)
            items.append(_parse_map(cur, item_indent, first_pair=(key, val)))
        else:
            items.append(_parse_scalar(content))
    return items

# Same key/colon boundary as _MAP_ENTRY_RE (kept in sync deliberately: a
# line only reaches _split_key_value after _MAP_ENTRY_RE already matched it,
# so both must agree on where the key ends).
_KEY_VALUE_SPLIT_RE = re.compile(r'^("[^"]*"|\'[^\']*\'|[^#\s].*?)\s*:(?:[ \t]+(.*)|$)')

def _split_key_value(stripped):
    m = _KEY_VALUE_SPLIT_RE.match(stripped)
    if not m:
        # defensive fallback; _MAP_ENTRY_RE should already guarantee a match
        key, _, rest = stripped.partition(":")
        return _unquote(key.strip()), rest.strip()
    return _unquote(m.group(1).strip()), (m.group(2) or "").strip()

def _parse_after_colon(cur, indent, rest):
    if rest == "":
        nxt = cur.peek()
        if nxt is not None and _indent(nxt) > indent:
            return _parse_value_at(cur, _indent(nxt))
        return None
    if rest in _BLOCK_STYLES:
        return _parse_block_scalar(cur, indent, rest)
    return _parse_scalar(rest)

def _parse_block_scalar(cur, key_indent, style):
    raw_lines = []
    base_indent = None
    while cur.i < len(cur.lines):
        raw = cur.lines[cur.i]
        if raw.strip() == "":
            raw_lines.append("")
            cur.i += 1
            continue
        li = _indent(raw)
        if li <= key_indent:
            break
        if base_indent is None:
            base_indent = li
        raw_lines.append(raw[min(base_indent, len(raw)):])
        cur.i += 1
    while raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    if style.startswith("|"):
        return "\n".join(raw_lines)
    return " ".join(l for l in raw_lines if l != "")

def _unquote(s):
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1].replace("''", "'")
    return s

def _parse_scalar(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1].replace("''", "'")
    m = re.search(r'\s+#', s)
    if m:
        s = s[:m.start()].strip()
    if s in ("null", "~", "Null", "NULL", ""):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if re.fullmatch(r'-?\d+', s):
        return int(s)
    if re.fullmatch(r'-?\d+\.\d+', s):
        return float(s)
    return s
