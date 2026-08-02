import hashlib, json
from pathlib import Path

EMPTY = {"files": {}, "arrows": {}, "lenses": {}, "overview": {}}

def _cache_path(root):
    return Path(root) / ".codemap" / "cache.json"

def load(root) -> dict:
    base = json.loads(json.dumps(EMPTY))  # deep copy, never alias EMPTY
    p = _cache_path(root)
    if p.exists():
        try:
            base.update(json.loads(p.read_text()))
        except json.JSONDecodeError:
            pass
    return base

def save(root, cache_dict):
    p = _cache_path(root)
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(cache_dict, indent=1))

def uncached_files(root, tree) -> list:
    cache_dict = load(root)
    out = []
    def walk(node):
        if node["kind"] == "file" and node["lang"]:
            try:
                data = (Path(root) / node["path"]).read_bytes()
            except OSError:
                data = None
            if data is not None:
                sha1 = hashlib.sha1(data).hexdigest()
                entry = cache_dict["files"].get(node["path"])
                if not entry or entry.get("sha1") != sha1:
                    out.append({"path": node["path"], "sha1": sha1})
        for c in node["children"]:
            walk(c)
    walk(tree)
    return out
