import subprocess
from pathlib import Path

MARKER = "# codemap-freshness"

def write_sidecar(root: Path) -> Path:
    root = Path(root)
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                       capture_output=True, text=True)
    sha = r.stdout.strip()
    if r.returncode != 0 or not sha:
        raise RuntimeError(f"cannot determine git HEAD for {root}: {r.stderr.strip() or 'no commits?'}")
    out_dir = root / ".codemap"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / "codemap-freshness.js"
    out.write_text(f'window.CODEMAP_HEAD = "{sha}";\n')
    return out

def install(root: Path) -> list:
    root = Path(root)
    hooks_dir = root / ".git" / "hooks"
    if not hooks_dir.is_dir():
        raise RuntimeError(f"{root} is not a git repository")

    lib_parent = str(Path(__file__).parent.parent)
    root_resolved = str(Path(root).resolve())
    line = f'{MARKER}\npython3 -c "import sys; sys.path.insert(0, \'{lib_parent}\'); from lib.hook import write_sidecar; write_sidecar(\'{root_resolved}\')" || true\n'

    touched = []
    for name in ("post-commit", "post-merge"):
        hook = hooks_dir / name
        if hook.exists():
            content = hook.read_text()
            if MARKER not in content:
                hook.write_text(content.rstrip("\n") + "\n" + line)
        else:
            hook.write_text("#!/bin/sh\n" + line)
        hook.chmod(0o755)
        touched.append(str(hook))
    return touched
