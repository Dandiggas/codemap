import re
from pathlib import Path
from lib.scan import EXCLUDES

URL_RE = re.compile(r'https?://([a-zA-Z0-9.-]+\.[a-z]{2,})')
ENV_RE = re.compile(r'''(?:os\.environ(?:\.get)?\(|getenv\(|process\.env\.|os\.Getenv\()["']?([A-Z][A-Z0-9_]{2,})''')
JOB_RE = re.compile(r'^  (\w[\w-]*):\s*$', re.M)
LOCALHOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "example.com", "www.w3.org", "github.com", "schemas.docker.com"}

def scan_infra(root: Path) -> dict:
    root = Path(root)
    pipeline, boundaries, externals, env_vars = [], [], [], set()

    workflows = sorted(root.glob(".github/workflows/*.yml")) + sorted(root.glob(".github/workflows/*.yaml"))
    if workflows:
        pipeline.append({"title": "git push", "sub": "GitHub", "warn": False})
        for wf in workflows:
            text = wf.read_text(errors="replace")
            in_jobs = text.split("\njobs:", 1)
            job_names = JOB_RE.findall(in_jobs[1]) if len(in_jobs) == 2 else []
            for job in job_names:
                pipeline.append({"title": f"ci: {job}", "sub": wf.name, "warn": False})
    else:
        pipeline.append({"title": "git (local)", "sub": "no CI detected", "warn": False})
        pipeline.append({"title": "⚠ manual", "sub": "no CI, no automated deploy", "warn": True})

    dockerfiles = list(root.glob("Dockerfile*")) + list(root.glob("**/Dockerfile"))
    dockerfiles = [d for d in dockerfiles if not any(part in EXCLUDES or (part.startswith(".") and part not in {".github"}) for part in d.relative_to(root).parts[:-1])][:5]
    containerized = bool(dockerfiles)
    if containerized:
        pipeline.append({"title": "container build", "sub": dockerfiles[0].name, "warn": False})

    compose = [p for n in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
               for p in [root / n] if p.exists()]
    if compose:
        boundaries.append({"title": "compose services", "sub": compose[0].name, "warn": False})

    # externals + env vars from code files (bounded sweep)
    exts = {".py", ".ts", ".tsx", ".js", ".go", ".yml", ".yaml", ".tf"}
    seen_hosts = {}
    for p in root.rglob("*"):
        rel_parts = p.relative_to(root).parts[:-1]  # All parts except filename
        if any(part in EXCLUDES or (part.startswith(".") and part not in {".github"}) for part in rel_parts):
            continue
        if p.is_file() and (p.suffix in exts or p.name == "Dockerfile" or p.name.endswith(".env.example")):
            try:
                text = p.read_text(errors="replace")
            except OSError:
                continue
            for m in URL_RE.finditer(text):
                host = m.group(1)
                if host not in LOCALHOSTS and host not in seen_hosts:
                    seen_hosts[host] = str(p.relative_to(root))
            env_vars.update(ENV_RE.findall(text))
    externals = [{"name": h, "evidence": src} for h, src in sorted(seen_hosts.items())]

    boundaries.append({"title": f"{len(externals)} external hosts",
                       "sub": ", ".join(e["name"] for e in externals[:4]) or "none detected",
                       "warn": False})
    if env_vars:
        boundaries.append({"title": f"{len(env_vars)} env vars",
                           "sub": ", ".join(sorted(env_vars)[:6]), "warn": False})
    if (root / ".env").exists():
        boundaries.append({"title": "⚠ .env on disk", "sub": "plaintext secrets file present", "warn": True})

    return {"pipeline": pipeline, "containerized": containerized,
            "externals": externals, "boundaries": boundaries,
            "env_vars": sorted(env_vars)}
