import json, subprocess
from datetime import datetime, timezone
from pathlib import Path
from lib.layout import place

TEMPLATE = Path(__file__).parent.parent / "template.html"
SNIPPET_LINES = 25


def render(tree, infra, labels, root: Path, editor_scheme: str) -> str:
    root = Path(root).resolve()
    _bake(tree, root, labels or {})
    meta = {
        "generated_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": _git(root, "rev-parse", "--short", "HEAD"),
        "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD"),
        "repo_abs": str(root),
        "editor_scheme": editor_scheme,
    }
    data = {"tree": tree, "infra": infra, "labels": labels, "meta": meta}
    # guard: a repo file's baked snippet (or any string) could contain "</script>",
    # "<!--", or "<script" sequences that would prematurely terminate or otherwise
    # confuse the inline <script> block (an HTML parser watches for "</script"
    # case-insensitively and also honors "<!--" inside script content per the
    # HTML spec's "script data" state). Escaping every "<" to its JSON/JS-safe
    # unicode form kills all of these in one move: JSON structural characters
    # never include "<", and "<" parses identically to "<" inside a JS
    # string literal, so the payload remains valid JSON and valid JS.
    payload = json.dumps(data).replace("<", "\\u003c")
    return TEMPLATE.read_text().replace("__CODEMAP_DATA__", payload)


def _bake(node, root, labels):
    """Attach layout to every dir/group level, arrow labels to every dir-level edge,
    code snippets + captions to every symbol, and file captions to every file."""
    if node["kind"] in ("dir", "repo", "group"):
        _collapse_zero_code_children(node)
        node["layout"] = place(node["children"], node.get("edges", []))
        _label_edges(node, labels)
        for c in node["children"]:
            _bake(c, root, labels)
    elif node["kind"] == "file":
        file_label = labels.get("files", {}).get(node["path"], {})
        caption = file_label.get("caption")
        if caption:
            node["caption"] = caption
        symbol_captions = file_label.get("symbol_captions", {})
        if node.get("symbols"):
            try:
                lines = (root / node["path"]).read_text(errors="replace").splitlines()
            except OSError:
                lines = []
            for s in node["symbols"]:
                start = max(0, s["line"] - 1)
                s["snippet"] = "\n".join(lines[start:start + SNIPPET_LINES])
                sym_caption = symbol_captions.get(s["name"])
                if sym_caption:
                    s["caption"] = sym_caption


GROUP_NAME = "data & docs"
GROUP_EXEMPT = {".github"}  # infra tab (System) depends on .github staying visible at its own level


def _is_zero_code(node):
    """True if this node (a file) or its whole subtree (a dir) contains no code files."""
    if node["kind"] == "file":
        return node.get("lang") is None
    return all(_is_zero_code(c) for c in node.get("children", []))


def _collapse_zero_code_children(node):
    """Fold 2+ zero-code children (db files, docs, lockfiles, renders) into one
    synthetic 'data & docs' group box, so a level isn't a junk drawer of
    zero-signal boxes sitting next to the real architecture.

    Rules: only collapse when there's at least one code-bearing sibling (an
    all-docs level stays flat rather than collapsing into nothing); never
    collapse .github (the System tab depends on it staying visible at this
    level); and never collapse a child an aggregated edge still references
    (edges.py only aggregates real import edges, so a zero-code child should
    never be an edge endpoint, but if one somehow is, trust the edge and
    leave that child uncollapsed rather than orphan the edge).
    """
    children = node.get("children")
    if not children:
        return
    has_code_sibling = any(not _is_zero_code(c) for c in children)
    if not has_code_sibling:
        return
    edge_names = set()
    for e in node.get("edges") or []:
        edge_names.add(e.get("source"))
        edge_names.add(e.get("target"))
    keep, collapsible = [], []
    for c in children:
        if c["name"] in GROUP_EXEMPT or c["name"] in edge_names or not _is_zero_code(c):
            keep.append(c)
        else:
            collapsible.append(c)
    if len(collapsible) < 2:
        return
    group = {
        "name": GROUP_NAME, "kind": "group", "path": node.get("path", ""), "lang": None,
        "size": sum(c.get("size", 0) for c in collapsible),
        "children": collapsible, "collapsed_count": len(collapsible),
    }
    node["children"] = keep + [group]


def _leaf_paths(node, out=None):
    """Every file's repo-relative path under this node (itself, if it's a file)."""
    if out is None:
        out = set()
    if node["kind"] == "file":
        out.add(node["path"])
    else:
        for c in node.get("children", []):
            _leaf_paths(c, out)
    return out


def _label_edges(node, labels):
    """Fill in edge['label'] for this node's dir-level edges from labels['arrows'].

    node['edges'] entries are {"source": <child name>, "target": <child name>, ...}
    (edges.py aggregates file-level import edges up to sibling child names at
    every level). An arrow key is "<src file path>→<dst file path>"; a dir-level
    edge gets labeled by the first (sorted, for determinism) underlying file
    edge whose src falls under the source child's subtree and dst under the
    target child's subtree.
    """
    edges = node.get("edges")
    arrows = labels.get("arrows")
    if not edges or not arrows:
        return
    child_paths = {c["name"]: _leaf_paths(c) for c in node["children"]}
    for e in edges:
        src_paths = child_paths.get(e["source"], set())
        dst_paths = child_paths.get(e["target"], set())
        if not src_paths or not dst_paths:
            continue
        for key in sorted(arrows.keys()):
            if "→" not in key:
                continue
            src, dst = key.split("→", 1)
            if src in src_paths and dst in dst_paths:
                e["label"] = arrows[key]
                break


def _git(root, *args):
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except OSError:
        return None


def write(html: str, root: Path) -> Path:
    out_dir = Path(root) / ".codemap"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / "codemap.html"
    out.write_text(html)
    return out
