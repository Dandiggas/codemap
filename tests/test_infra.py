import unittest, tempfile
from pathlib import Path
from lib.infra import scan_infra

FIX = Path(__file__).parent / "fixtures" / "toyrepo"

class TestInfra(unittest.TestCase):
    def test_pipeline_from_ci(self):
        r = scan_infra(FIX)
        titles = [s["title"] for s in r["pipeline"]]
        self.assertIn("ci: test", titles)
        self.assertIn("ci: deploy", titles)
        self.assertTrue(r["containerized"])  # Dockerfile present

    def test_externals_detected(self):
        r = scan_infra(FIX)
        names = {e["name"] for e in r["externals"]}
        self.assertIn("api.sendgrid.com", names)

    def test_no_ci_warns(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "x.py").write_text("print(1)")
            r = scan_infra(Path(td))
            warns = [s for s in r["pipeline"] if s["warn"]]
            self.assertTrue(any("no CI" in s["sub"] for s in warns))
            self.assertFalse(r["containerized"])

    def test_repo_under_excluded_dirname_still_scanned(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "build" / "myrepo"
            root.mkdir(parents=True)
            (root / "api.py").write_text('import requests\nrequests.get("https://api.stripe.com/v1/charges")\n')
            r = scan_infra(root)
            names = {e["name"] for e in r["externals"]}
            self.assertIn("api.stripe.com", names)

    def test_env_example_file_scanned(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".env.example").write_text("PAYMENTS_URL=https://api.stripe.com/v1\nSTRIPE_KEY=\n")
            r = scan_infra(root)
            names = {e["name"] for e in r["externals"]}
            self.assertIn("api.stripe.com", names)

    def test_build_artifact_dirs_excluded_from_sweep(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            nxt = root / ".next" / "static"
            nxt.mkdir(parents=True)
            (nxt / "chunk.js").write_text('fetch("https://bugs.webkit.org/x")\n')
            (root / "app.py").write_text('import requests\nrequests.get("https://api.stripe.com/v1")\n')
            r = scan_infra(root)
            names = {e["name"] for e in r["externals"]}
            self.assertIn("api.stripe.com", names)
            self.assertNotIn("bugs.webkit.org", names)

if __name__ == "__main__":
    unittest.main()
