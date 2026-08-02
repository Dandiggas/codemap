#!/usr/bin/env python3
"""codemap eval — run: python3 evals/codemap_eval.py. Exit 0 = pass."""
import json, shutil, subprocess, sys, tempfile
from pathlib import Path

SKILL = Path(__file__).parent.parent
FIX = SKILL / "tests" / "fixtures" / "toyrepo"
FAILURES = []

def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)

def run(cmd, cwd=None):
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)

with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "repo"
    shutil.copytree(FIX, root)

    # HAPPY: render produces a map with all three tabs' data baked in
    r = run([sys.executable, str(SKILL / "codemap.py"), "render", str(root)])
    out = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    check("render exits 0", r.returncode == 0, r.stderr[-300:])
    check("html exists", bool(out) and out.exists())
    html = out.read_text() if out and out.exists() else ""
    check("code boxes baked", '"orders.py"' in html)
    check("system data baked", "ci: deploy" in html)
    check("overview externals baked", "api.sendgrid.com" in html)
    check("staleness stamp baked", '"generated_iso"' in html)
    check("self-contained", "https://cdn" not in html and "http://cdn" not in html)

    # SAD: empty dir → exits nonzero OR renders an honest empty map, never a traceback-free lie
    with tempfile.TemporaryDirectory() as empty:
        r2 = run([sys.executable, str(SKILL / "codemap.py"), "render", empty])
        check("empty repo doesn't crash", r2.returncode == 0 or "Error" in r2.stderr)

    # SAD: non-git repo → render still works (layer-1 staleness), hook cmd fails loudly
    r3 = run([sys.executable, str(SKILL / "codemap.py"), "hook", str(root)])
    check("hook on non-git fails loudly", r3.returncode != 0)

    # SAD / MUST-NOT: render must not modify tracked repo files (only .codemap/)
    before = {p: p.stat().st_mtime for p in root.rglob("*.py")}
    run([sys.executable, str(SKILL / "codemap.py"), "render", str(root)])
    after = {p: p.stat().st_mtime for p in root.rglob("*.py")}
    check("source files untouched", before == after)

    # SAD: freshness on non-git → clean one-line error, no traceback
    with tempfile.TemporaryDirectory() as ng:
        r4 = run([sys.executable, str(SKILL / "codemap.py"), "freshness", ng])
        check("freshness non-git exits nonzero", r4.returncode != 0)
        check("freshness non-git no traceback", "Traceback" not in r4.stderr)

print("\n" + ("EVAL PASS" if not FAILURES else f"EVAL FAIL: {FAILURES}"))
sys.exit(0 if not FAILURES else 1)
