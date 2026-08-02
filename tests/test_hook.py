import unittest, subprocess, tempfile
from pathlib import Path
from lib.hook import write_sidecar, install

def make_git_repo(td):
    root = Path(td)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "init"], cwd=root, check=True)
    return root

class TestHook(unittest.TestCase):
    def test_sidecar_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_git_repo(td)
            path = write_sidecar(root)
            text = path.read_text()
            self.assertRegex(text, r'window\.CODEMAP_HEAD = "[0-9a-f]{7,}";')

    def test_install_appends_not_clobbers(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_git_repo(td)
            existing = root / ".git" / "hooks" / "post-commit"
            existing.write_text("#!/bin/sh\necho keepme\n")
            existing.chmod(0o755)
            touched = install(root)
            content = existing.read_text()
            self.assertIn("keepme", content)          # old hook preserved
            self.assertIn("write_sidecar", content)   # ours appended
            self.assertTrue(any("post-merge" in t for t in touched))

    def test_commit_updates_sidecar(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_git_repo(td)
            install(root)
            before = write_sidecar(root).read_text()
            subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                            "commit", "-q", "--allow-empty", "-m", "two"], cwd=root, check=True)
            after = (root / ".codemap" / "codemap-freshness.js").read_text()
            self.assertNotEqual(before, after)

    def test_non_git_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError):
                install(Path(td))

    def test_no_commit_repo_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            with self.assertRaises(RuntimeError):
                write_sidecar(root)

if __name__ == "__main__":
    unittest.main()
