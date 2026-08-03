"""GitHub Actions deployed-state enrichment for the System tab.

fetch_deployed(root) is best-effort and must never block a render: if `gh`
isn't on PATH, the repo has no GitHub remote, or the `gh run list` call
itself fails for any reason (auth, network, timeout, nonzero exit, bad
JSON), it falls back to whatever was cached at .codemap/deployed.json on a
previous successful run, or an empty dict if there's nothing cached yet.
Never raises.
"""
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

GH_TIMEOUT = 10
GIT_TIMEOUT = 5
RUN_FIELDS = "workflowName,status,conclusion,headSha,updatedAt,url"


def newest_per_workflow(runs) -> list:
    """Reduce gh-run-list rows to the newest run per workflow name.

    Pure function: no I/O. Input rows are gh's own field names
    (workflowName, status, conclusion, headSha, updatedAt, url); output rows
    are normalized to {"workflow","status","conclusion","sha","at","url"}.
    A row with no workflowName can't be keyed by anything meaningful and is
    skipped rather than becoming a nameless entry. "Newest" compares the
    updatedAt strings directly, which works because gh emits RFC3339
    timestamps (lexicographic order matches chronological order). Output is
    sorted by workflow name for deterministic rendering.
    """
    best = {}
    for run in runs or []:
        name = run.get("workflowName")
        if not name:
            continue
        at = run.get("updatedAt") or ""
        if name not in best or at > best[name]["at"]:
            best[name] = {
                "workflow": name,
                "status": run.get("status", ""),
                "conclusion": run.get("conclusion", ""),
                "sha": run.get("headSha", ""),
                "at": at,
                "url": run.get("url", ""),
            }
    return [best[k] for k in sorted(best)]


def _has_github_remote(root: Path) -> bool:
    """True if ANY configured remote points at github.com.

    Not just `origin`: plenty of real setups name the GitHub remote something
    else (a fork with `origin` on a private host and `upstream` on GitHub, or
    a deploy remote), and `gh run list` resolves the repo from the whole
    remote set, not from origin alone. Checking origin only made this gate
    disagree with the tool it guards.
    """
    r = subprocess.run(
        ["git", "remote", "-v"],
        cwd=str(root), capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )
    return r.returncode == 0 and "github.com" in r.stdout


def _is_repo_toplevel(root: Path) -> bool:
    """True only when root IS the git repository's top level.

    git commands run with cwd=root walk UP the directory tree, so a scanned
    folder nested inside some unrelated repository would inherit that
    repository's remotes and get its CI runs attributed to the map. The map
    is generated FOR root; unless root is itself the repo top level, deployed
    state cannot be honestly attributed, so the fetch is skipped entirely.
    """
    r = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(root), capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return False
    return Path(r.stdout.strip()).resolve() == Path(root).resolve()


def _fetch_runs(root: Path) -> list:
    """Shell out to `gh run list`. Raises on any failure; fetch_deployed is
    the only caller and treats any exception here as cache-fallback."""
    r = subprocess.run(
        ["gh", "run", "list", "--limit", "20", "--json", RUN_FIELDS],
        cwd=str(root), capture_output=True, text=True, timeout=GH_TIMEOUT,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "gh run list exited nonzero")
    return json.loads(r.stdout)


def _cache_path(root: Path) -> Path:
    return Path(root) / ".codemap" / "deployed.json"


def _load_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save_cache(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    except OSError:
        pass  # a write failure here must not surface: caller already has `data`


def fetch_deployed(root) -> dict:
    """Best-effort fetch of the latest GitHub Actions run per workflow.

    Returns {"workflows": [{"workflow","status","conclusion","sha","at","url"}],
    "fetched_at": iso timestamp} on success, or the cached copy from a prior
    successful run (or {} if none exists) on any failure. Caches its result
    to <root>/.codemap/deployed.json on success so offline renders reuse the
    last known state instead of going dark.
    """
    root = Path(root)
    cache_path = _cache_path(root)
    cached = _load_cache(cache_path)
    try:
        if not _is_repo_toplevel(root):
            # Not this root's repo: any cached copy here was misattributed
            # from an enclosing repository. Never serve it.
            return {}
        if not shutil.which("gh"):
            return cached
        if not _has_github_remote(root):
            return cached
        runs = _fetch_runs(root)
        result = {
            "workflows": newest_per_workflow(runs),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _save_cache(cache_path, result)
        return result
    except Exception:
        return cached
