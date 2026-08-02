import posixpath
from pathlib import Path
from lib import parse_py, parse_ts, parse_go

PARSERS = {"python": parse_py.parse, "ts": parse_ts.parse, "go": parse_go.parse}

def build(tree: dict, root: Path) -> dict:
    root = Path(root)
    files = _collect_files(tree)
    by_path = {f["path"]: f for f in files}

    # parse every code file
    for f in files:
        parser = PARSERS.get(f["lang"])
        if not parser:
            f["symbols"], f["_imports"] = [], []
            continue
        try:
            parsed = parser((root / f["path"]).read_text(errors="replace"))
        except OSError:
            parsed = {"symbols": [], "imports": [], "calls": []}
        f["symbols"] = parsed["symbols"]
        f["_imports"] = parsed["imports"]
        f["_calls"] = parsed.get("calls", [])

    # resolve imports to repo-internal file paths
    file_edges = []
    for f in files:
        for imp in f.get("_imports", []):
            dst = _resolve(imp, f["path"], by_path)
            if dst and dst != f["path"]:
                file_edges.append({"src": f["path"], "dst": dst, "kind": "import"})

    # aggregate to every dir level: edge between the two direct children
    # of `node` whose subtrees contain src and dst
    _attach_level_edges(tree, file_edges)
    return {"file_edges": file_edges}

def _collect_files(node, out=None):
    if out is None:
        out = []
    if node["kind"] == "file":
        out.append(node)
    for c in node["children"]:
        _collect_files(c, out)
    return out

def _resolve(imp: str, src_path: str, by_path: dict):
    if imp.startswith("."):
        # ts/js relative import always contains a path separator, e.g.
        # "./client" or "../foo/bar" from web/index.ts -> web/client.ts
        if "/" in imp:
            target = posixpath.normpath(str(Path(src_path).parent / imp))
            for suffix in (".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.js"):
                cand = target + suffix
                if cand in by_path:
                    return cand
            return None
        # python leading-dot relative import: ".store" / "..adapters.mailer"
        # resolved relative to the source file's directory. Each extra
        # leading dot beyond the first goes up one directory.
        stripped = imp.lstrip(".")
        level = len(imp) - len(stripped)
        base = Path(src_path).parent
        for _ in range(level - 1):
            base = base.parent
        rel = stripped.replace(".", "/")
        target = posixpath.normpath(str(base / rel)) if rel else posixpath.normpath(str(base))
        cand = target + ".py"
        if cand in by_path:
            return cand
        cand = posixpath.join(target, "__init__.py")
        if cand in by_path:
            return cand
        return None

    # python dotted: app.core.store -> app/core/store.py
    cand = imp.replace(".", "/") + ".py"
    if cand in by_path:
        return cand
    cand = imp.replace(".", "/") + "/__init__.py"
    if cand in by_path:
        return cand
    # src-layout fallback: package root nested under a source dir
    # (src/pli/audio.py imported as pli.audio). Accept a suffix match
    # only when it is unambiguous across the repo.
    for suffix in (imp.replace(".", "/") + ".py", imp.replace(".", "/") + "/__init__.py"):
        matches = [p for p in by_path if p.endswith("/" + suffix)]
        if len(matches) == 1:
            return matches[0]
    return None

def _attach_level_edges(node, file_edges):
    if node["kind"] not in ("dir", "repo"):
        return
    child_of = {}  # rel file path -> direct child name that contains it
    for child in node["children"]:
        for f in _collect_files(dict(child, children=child["children"])):
            child_of[f["path"]] = child["name"]
        if child["kind"] == "file":
            child_of[child["path"]] = child["name"]
    weight, order = {}, []
    for e in file_edges:
        s, d = child_of.get(e["src"]), child_of.get(e["dst"])
        if s and d and s != d:
            key = (s, d)
            if key not in weight:
                weight[key] = 0
                order.append((key, e["kind"]))
            weight[key] += 1
    edges = [
        {"source": s, "target": d, "kind": kind, "label": None, "weight": weight[(s, d)]}
        for (s, d), kind in order
    ]
    node["edges"] = edges
    for child in node["children"]:
        _attach_level_edges(child, file_edges)
