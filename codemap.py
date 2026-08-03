#!/usr/bin/env python3
import argparse, json, shutil, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from lib.scan import scan
from lib.edges import build
from lib.infra import scan_infra
from lib.iac import scan_iac
from lib.cache import load, uncached_files
from lib.render import render, write
from lib.hook import write_sidecar, install

def detect_editor():
    if shutil.which("cursor"):
        return "cursor"
    return "vscode"

def main():
    ap = argparse.ArgumentParser(
        prog="codemap.py",
        description="Deterministic three-altitude architecture map (Overview / Code / System) rendered as one self-contained HTML file.",
    )
    ap.add_argument("cmd", choices=["scan", "render", "freshness", "hook"])
    ap.add_argument("root", type=Path)
    ap.add_argument("--editor", default=None, choices=["vscode", "cursor"],
                     help="editor scheme for file-open links (default: auto-detect, falls back to vscode)")
    ap.add_argument("--no-gh", action="store_true",
                     help="render: skip the GitHub Actions deployed-state fetch (the only "
                          "network call codemap can make), leaving the deployed chip dormant")
    args = ap.parse_args()
    root = args.root.resolve()

    if args.cmd == "scan":
        tree = scan(root)
        flat = build(tree, root)
        print(json.dumps({"tree_summary": _summary(tree),
                          "file_edges": flat["file_edges"],
                          "infra": scan_infra(root),
                          "iac": scan_iac(root),
                          "uncached": uncached_files(root, tree)}))
    elif args.cmd == "render":
        tree = scan(root)
        build(tree, root)
        labels = load(root)
        html = render(tree, scan_infra(root), labels, root,
                      args.editor or detect_editor(), fetch_gh=not args.no_gh)
        path = write(html, root)
        try:
            write_sidecar(root)
        except RuntimeError as e:
            # non-git repo or no commits yet: layer-1 (timestamp-only) staleness
            # is still fine, this just skips the layer-2 git-HEAD sidecar
            print(f"note: freshness sidecar skipped: {e}", file=sys.stderr)
        print(path)
    elif args.cmd == "freshness":
        try:
            print(write_sidecar(root))
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.cmd == "hook":
        try:
            print("\n".join(install(root)))
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)

def _summary(tree, depth=0):
    if depth >= 2:
        return {"name": tree["name"], "files": "…"}
    return {"name": tree["name"], "kind": tree["kind"],
            "children": [_summary(c, depth + 1) for c in tree["children"]]}

if __name__ == "__main__":
    main()
