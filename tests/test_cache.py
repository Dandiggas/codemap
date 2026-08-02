import unittest, tempfile, json, shutil
from pathlib import Path
from lib.scan import scan
from lib.cache import load, save, uncached_files

FIX = Path(__file__).parent / "fixtures" / "toyrepo"

class TestCache(unittest.TestCase):
    def test_all_uncached_on_first_run(self):
        with tempfile.TemporaryDirectory() as td:
            # copy fixture so .codemap doesn't pollute the real one
            root = Path(td) / "repo"; shutil.copytree(FIX, root, ignore=shutil.ignore_patterns(".codemap"))
            self.assertFalse((root / ".codemap").exists())
            tree = scan(root)
            todo = uncached_files(root, tree)
            paths = {t["path"] for t in todo}
            self.assertIn("app/core/orders.py", paths)

    def test_cached_file_skipped_until_changed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"; shutil.copytree(FIX, root, ignore=shutil.ignore_patterns(".codemap"))
            tree = scan(root)
            todo = uncached_files(root, tree)
            c = load(root)
            for t in todo:
                c["files"][t["path"]] = {"sha1": t["sha1"], "caption": "x"}
            save(root, c)
            self.assertEqual(uncached_files(root, scan(root)), [])
            (root / "app" / "core" / "orders.py").write_text("def changed(): pass\n")
            stale = {t["path"] for t in uncached_files(root, scan(root))}
            self.assertEqual(stale, {"app/core/orders.py"})

    def test_load_never_aliases_empty(self):
        from lib.cache import EMPTY
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".codemap").mkdir()
            (root / ".codemap" / "cache.json").write_text('{"files": {}}')  # partial: no arrows key
            c = load(root)
            c["arrows"]["a->b"] = "polluted"
            self.assertEqual(EMPTY["arrows"], {}, "EMPTY was mutated via aliased sub-dict")

if __name__ == "__main__":
    unittest.main()
