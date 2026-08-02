from pathlib import Path

EXCLUDES = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".pytest_cache", "out", "release", "target", ".idea", ".vscode",
    ".codemap", ".superpowers", "coverage", ".mypy_cache",
}
LANG_BY_EXT = {
    ".py": "python",
    ".ts": "ts", ".tsx": "ts", ".js": "ts", ".jsx": "ts", ".mjs": "ts",
    ".go": "go",
}

def scan(root: Path) -> dict:
    root = Path(root)
    node = _walk(root, root)
    node["kind"] = "repo"
    return node

def _walk(path: Path, root: Path) -> dict:
    rel = str(path.relative_to(root)) if path != root else ""
    if path.is_file():
        return {
            "name": path.name, "kind": "file", "path": rel,
            "lang": LANG_BY_EXT.get(path.suffix.lower()),
            "size": path.stat().st_size, "children": [],
        }
    children = []
    for child in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if child.is_symlink():
            # broken/cyclic symlinks can't be stat'd or would recurse forever
            continue
        if child.name in EXCLUDES or child.name.startswith("."):
            # keep .github: CI configs matter for the System tab
            if child.name != ".github":
                continue
        try:
            children.append(_walk(child, root))
        except OSError:
            # child vanished (race) or otherwise became unreadable mid-walk
            continue
    return {"name": path.name, "kind": "dir", "path": rel, "lang": None,
            "size": sum(c["size"] for c in children), "children": children}
