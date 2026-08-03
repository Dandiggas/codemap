#!/usr/bin/env python3
"""codemap eval — run: python3 evals/codemap_eval.py. Exit 0 = pass."""
import json, shutil, subprocess, sys, tempfile
from pathlib import Path

SKILL = Path(__file__).parent.parent
FIX = SKILL / "tests" / "fixtures" / "toyrepo"
IAC_FIX = SKILL / "tests" / "fixtures" / "iacrepo"
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

    # SAD: a real git repo that simply has no remote still renders -- lib/
    # deployed.py's `gh` enrichment is best-effort and must never block a
    # render or leave the model half-baked. Rendered in its OWN git-initialised
    # copy rather than reusing the non-git render above, so this exercises the
    # remote-lookup path (git present, zero remotes) instead of silently
    # re-reporting the "not a git repo at all" case.
    with tempfile.TemporaryDirectory() as gtd:
        git_root = Path(gtd) / "repo"
        shutil.copytree(FIX, git_root)
        run(["git", "init", "-q"], cwd=git_root)
        run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "--allow-empty", "-m", "init"], cwd=git_root)
        remotes = run(["git", "remote"], cwd=git_root)
        rg = run([sys.executable, str(SKILL / "codemap.py"), "render", str(git_root)])
        outg = Path(rg.stdout.strip()) if rg.returncode == 0 and rg.stdout.strip() else None
        html_g = outg.read_text() if outg and outg.exists() else ""
        check("git repo with zero remotes: render exits 0",
              rg.returncode == 0 and remotes.stdout.strip() == "", rg.stderr[-300:])
        check("git repo with zero remotes: deployed empty", '"deployed": {}' in html_g)

    # SAD: --no-gh skips the fetch outright, the documented offline opt-out
    rn = run([sys.executable, str(SKILL / "codemap.py"), "render", str(root), "--no-gh"])
    html_n = Path(rn.stdout.strip()).read_text() if rn.returncode == 0 and rn.stdout.strip() else ""
    check("--no-gh exits 0", rn.returncode == 0, rn.stderr[-300:])
    check("--no-gh leaves deployed empty", '"deployed": {}' in html_n)

    # HAPPY: iacrepo fixture renders a role name and its grants onto the page
    with tempfile.TemporaryDirectory() as itd:
        iac_root = Path(itd) / "repo"
        shutil.copytree(IAC_FIX, iac_root)
        ri = run([sys.executable, str(SKILL / "codemap.py"), "render", str(iac_root)])
        outi = Path(ri.stdout.strip()) if ri.returncode == 0 and ri.stdout.strip() else None
        check("iacrepo render exits 0", ri.returncode == 0, ri.stderr[-300:])
        html_iac = outi.read_text() if outi and outi.exists() else ""
        check("iacrepo role name baked", '"lambda_exec"' in html_iac)
        # `"grants": [` (with the space json.dumps puts after the colon) only
        # ever appears in the baked DATA payload, never in the static
        # template's own markup (which spells it class="grants" -- no space,
        # no colon), so this can't pass on template boilerplate alone.
        check("iacrepo grants marker baked", '"grants": [' in html_iac)

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
