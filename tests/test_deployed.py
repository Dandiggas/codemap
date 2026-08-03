import json, os, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock

from lib.deployed import _has_github_remote, fetch_deployed, newest_per_workflow

FIX = Path(__file__).parent / "fixtures" / "gh_runs.json"


def make_git_repo(td):
    root = Path(td)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                     "commit", "-q", "--allow-empty", "-m", "init"], cwd=root, check=True)
    return root


class TestNewestPerWorkflow(unittest.TestCase):
    """Pure function: reduces gh-run-list rows to the newest run per workflow."""

    def test_reduces_to_newest_per_workflow_name(self):
        runs = json.loads(FIX.read_text())
        out = newest_per_workflow(runs)
        by_name = {w["workflow"]: w for w in out}
        # "" workflowName (run 5) is unnamed and gets skipped, not a phantom entry
        self.assertEqual(set(by_name), {"deploy", "ci"})
        # deploy's two runs: 2026-08-02 run wins over 2026-08-01
        self.assertEqual(by_name["deploy"]["sha"], "bbb2222bbb2222bbb2222bbb2222bbb2222bbbb")
        self.assertEqual(by_name["deploy"]["conclusion"], "failure")
        self.assertEqual(by_name["deploy"]["url"], "https://github.com/example/repo/actions/runs/2")
        # ci's two runs: 2026-08-01 completed wins over 2026-07-30 in_progress
        self.assertEqual(by_name["ci"]["sha"], "ccc3333ccc3333ccc3333ccc3333ccc3333cccc")
        self.assertEqual(by_name["ci"]["status"], "completed")

    def test_output_shape_is_normalized(self):
        out = newest_per_workflow([
            {"workflowName": "solo", "status": "completed", "conclusion": "success",
             "headSha": "f00d", "updatedAt": "2026-08-01T00:00:00Z", "url": "https://x/1"},
        ])
        self.assertEqual(out, [{"workflow": "solo", "status": "completed",
                                 "conclusion": "success", "sha": "f00d",
                                 "at": "2026-08-01T00:00:00Z", "url": "https://x/1"}])

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(newest_per_workflow([]), [])

    def test_runs_missing_workflow_name_are_skipped(self):
        self.assertEqual(newest_per_workflow([{"status": "completed"}]), [])


class TestFetchDeployedNoGh(unittest.TestCase):
    """No `gh` on PATH: never raises, returns {} with nothing cached."""

    def test_returns_empty_when_gh_absent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.dict(os.environ, {"PATH": "/nonexistent-bin-dir"}):
                self.assertEqual(fetch_deployed(root), {})

    def test_returns_cache_when_gh_absent_and_cache_exists(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache_dir = root / ".codemap"
            cache_dir.mkdir()
            cached = {"workflows": [{"workflow": "deploy", "status": "completed",
                                      "conclusion": "success", "sha": "abc1234",
                                      "at": "2026-08-01T00:00:00Z", "url": "https://x"}],
                       "fetched_at": "2026-08-01T00:05:00Z"}
            (cache_dir / "deployed.json").write_text(json.dumps(cached))
            with mock.patch.dict(os.environ, {"PATH": "/nonexistent-bin-dir"}):
                self.assertEqual(fetch_deployed(root), cached)


class TestFetchDeployedNoRemote(unittest.TestCase):
    """gh present, repo is git but has no origin remote: falls back to cache."""

    def test_no_remote_falls_back_to_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_git_repo(td)
            cache_dir = root / ".codemap"
            cache_dir.mkdir()
            cached = {"workflows": [], "fetched_at": "2026-08-01T00:00:00Z"}
            (cache_dir / "deployed.json").write_text(json.dumps(cached))
            with mock.patch("lib.deployed.shutil.which", return_value="/usr/bin/gh"):
                self.assertEqual(fetch_deployed(root), cached)

    def test_non_git_dir_falls_back_to_empty(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch("lib.deployed.shutil.which", return_value="/usr/bin/gh"):
                self.assertEqual(fetch_deployed(root), {})

    def test_non_origin_github_remote_is_still_a_github_repo(self):
        # the GitHub remote is often not called "origin" (fork with origin on
        # a private host, upstream on GitHub). Checking origin alone made the
        # gate skip a repo `gh run list` would have answered for.
        with tempfile.TemporaryDirectory() as td:
            root = make_git_repo(td)
            subprocess.run(["git", "remote", "add", "internal",
                             "https://git.example.com/team/repo.git"], cwd=root, check=True)
            subprocess.run(["git", "remote", "add", "upstream",
                             "https://github.com/example/repo.git"], cwd=root, check=True)
            self.assertTrue(_has_github_remote(root))

    def test_only_non_github_remotes_is_not_a_github_repo(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_git_repo(td)
            subprocess.run(["git", "remote", "add", "origin",
                             "https://gitlab.com/example/repo.git"], cwd=root, check=True)
            self.assertFalse(_has_github_remote(root))


class TestFetchDeployedGhFailure(unittest.TestCase):
    """gh present, real github remote, but the run-list call itself fails
    (timeout, network, nonzero exit): still falls back to cache, never raises."""

    def _repo_with_github_remote(self, td):
        root = make_git_repo(td)
        subprocess.run(["git", "remote", "add", "origin",
                         "https://github.com/example/repo.git"], cwd=root, check=True)
        return root

    def test_timeout_falls_back_to_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._repo_with_github_remote(td)
            cache_dir = root / ".codemap"
            cache_dir.mkdir()
            cached = {"workflows": [{"workflow": "deploy", "status": "completed",
                                      "conclusion": "success", "sha": "abc1234",
                                      "at": "2026-08-01T00:00:00Z", "url": "https://x"}],
                       "fetched_at": "2026-08-01T00:05:00Z"}
            (cache_dir / "deployed.json").write_text(json.dumps(cached))
            with mock.patch("lib.deployed.shutil.which", return_value="/usr/bin/gh"), \
                 mock.patch("lib.deployed._fetch_runs",
                             side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=10)):
                self.assertEqual(fetch_deployed(root), cached)

    def test_nonzero_exit_falls_back_to_empty_without_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._repo_with_github_remote(td)
            with mock.patch("lib.deployed.shutil.which", return_value="/usr/bin/gh"), \
                 mock.patch("lib.deployed._fetch_runs", side_effect=RuntimeError("gh run list failed")):
                self.assertEqual(fetch_deployed(root), {})

    def test_bad_json_falls_back_to_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._repo_with_github_remote(td)
            cache_dir = root / ".codemap"
            cache_dir.mkdir()
            cached = {"workflows": [], "fetched_at": "2026-08-01T00:00:00Z"}
            (cache_dir / "deployed.json").write_text(json.dumps(cached))
            with mock.patch("lib.deployed.shutil.which", return_value="/usr/bin/gh"), \
                 mock.patch("lib.deployed._fetch_runs", side_effect=json.JSONDecodeError("bad", "x", 0)):
                self.assertEqual(fetch_deployed(root), cached)


class TestFetchDeployedSuccess(unittest.TestCase):
    """gh present, real remote, run-list succeeds: writes + returns the cache."""

    def test_success_writes_and_returns_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_git_repo(td)
            subprocess.run(["git", "remote", "add", "origin",
                             "https://github.com/example/repo.git"], cwd=root, check=True)
            runs = json.loads(FIX.read_text())
            with mock.patch("lib.deployed.shutil.which", return_value="/usr/bin/gh"), \
                 mock.patch("lib.deployed._fetch_runs", return_value=runs):
                result = fetch_deployed(root)
            self.assertIn("workflows", result)
            self.assertIn("fetched_at", result)
            by_name = {w["workflow"]: w for w in result["workflows"]}
            self.assertEqual(by_name["deploy"]["conclusion"], "failure")
            cache_path = root / ".codemap" / "deployed.json"
            self.assertTrue(cache_path.exists())
            self.assertEqual(json.loads(cache_path.read_text()), result)





class TestRepoToplevelGuard(unittest.TestCase):
    def test_nested_folder_inside_github_repo_gets_no_deployed(self):
        # A scanned folder nested inside an unrelated repo with a github
        # remote must NOT inherit that repo's CI attribution (git walks up).
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            outer = Path(td)
            subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
            subprocess.run(["git", "remote", "add", "origin",
                            "https://github.com/example/outer.git"], cwd=outer, check=True)
            nested = outer / "some" / "scanned" / "folder"
            nested.mkdir(parents=True)
            (nested / "x.py").write_text("a = 1")
            self.assertEqual(fetch_deployed(nested), {})

    def test_nested_folder_never_serves_a_stale_cache(self):
        # A misattributed cache written before the guard existed must not be
        # served either: nested roots always get {}.
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            outer = Path(td)
            subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
            subprocess.run(["git", "remote", "add", "origin",
                            "https://github.com/example/outer.git"], cwd=outer, check=True)
            nested = outer / "scanned"
            nested.mkdir()
            stale = nested / ".codemap"
            stale.mkdir()
            (stale / "deployed.json").write_text(
                '{"workflows": [{"workflow": "stale", "sha": "beef"}], "fetched_at": "old"}')
            self.assertEqual(fetch_deployed(nested), {})

    def test_repo_toplevel_itself_passes_the_guard(self):
        from lib.deployed import _is_repo_toplevel
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            self.assertTrue(_is_repo_toplevel(root))
            sub = root / "sub"
            sub.mkdir()
            self.assertFalse(_is_repo_toplevel(sub))


if __name__ == "__main__":
    unittest.main()
