import json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "toyrepo"
IAC_FIX = Path(__file__).parent / "fixtures" / "iacrepo"
CODEMAP = Path(__file__).parent.parent / "codemap.py"


class TestCliScan(unittest.TestCase):
    def test_scan_prints_file_edges(self):
        # AGENTS.md's arrow-key contract depends on `scan` actually emitting
        # file_edges in its printed JSON (edges.build already computes it,
        # the CLI used to discard it). This is the executable proof.
        r = subprocess.run(
            [sys.executable, str(CODEMAP), "scan", str(FIX)],
            capture_output=True, text=True, check=True,
        )
        payload = json.loads(r.stdout)
        self.assertIn("file_edges", payload)
        self.assertIn(
            {"src": "app/core/orders.py", "dst": "app/core/store.py", "kind": "import"},
            payload["file_edges"],
        )

    def test_scan_prints_iac_roles_and_connections(self):
        # AGENTS.md's infra-whys step depends on `scan` exposing iac.roles /
        # iac.connections so a labeling agent can see what to write whys for
        # before ever running `render` (which is where the model used to be
        # baked, with no CLI-visible equivalent beforehand).
        r = subprocess.run(
            [sys.executable, str(CODEMAP), "scan", str(IAC_FIX)],
            capture_output=True, text=True, check=True,
        )
        payload = json.loads(r.stdout)
        self.assertIn("iac", payload)
        role_ids = {role["id"] for role in payload["iac"]["roles"]}
        self.assertIn("tf:.::aws_iam_role.lambda_exec", role_ids)
        self.assertTrue(payload["iac"]["connections"])

    def test_scan_iac_empty_for_repo_without_infrastructure(self):
        r = subprocess.run(
            [sys.executable, str(CODEMAP), "scan", str(FIX)],
            capture_output=True, text=True, check=True,
        )
        payload = json.loads(r.stdout)
        self.assertEqual(payload["iac"]["roles"], [])
        self.assertEqual(payload["iac"]["connections"], [])


class TestCliRenderNoGh(unittest.TestCase):
    def test_no_gh_render_bakes_an_empty_deployed_model(self):
        # `render --no-gh` is the offline guarantee the README points at: no
        # `gh run list`, so the deployed model is empty no matter what a
        # previous fetch cached. Run against a temp copy so the fixture's own
        # .codemap/ (which may hold a real cached deployed.json) is untouched.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            shutil.copytree(IAC_FIX, root, ignore=shutil.ignore_patterns(".codemap"))
            cache_dir = root / ".codemap"
            cache_dir.mkdir()
            (cache_dir / "deployed.json").write_text(json.dumps(
                {"workflows": [{"workflow": "deploy", "status": "completed",
                                 "conclusion": "success", "sha": "abc1234",
                                 "at": "2026-08-01T00:00:00Z", "url": "https://x"}],
                 "fetched_at": "2026-08-01T00:05:00Z"}))
            r = subprocess.run(
                [sys.executable, str(CODEMAP), "render", str(root), "--no-gh"],
                capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            html = Path(r.stdout.strip()).read_text()
            self.assertIn('"deployed": {}', html)
            self.assertNotIn("abc1234", html)


if __name__ == "__main__":
    unittest.main()
