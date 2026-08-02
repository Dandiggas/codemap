import tempfile, unittest
from pathlib import Path
from lib.scan import scan

FIX = Path(__file__).parent / "fixtures" / "toyrepo"

class TestScan(unittest.TestCase):
    def test_tree_shape(self):
        tree = scan(FIX)
        self.assertEqual(tree["kind"], "repo")
        names = {c["name"] for c in tree["children"]}
        self.assertIn("app", names)
        self.assertIn("web", names)
        self.assertIn("main.go", names)
        self.assertNotIn("node_modules", names)
        self.assertNotIn("__pycache__", names)

    def test_lang_tagging(self):
        tree = scan(FIX)
        app = next(c for c in tree["children"] if c["name"] == "app")
        core = next(c for c in app["children"] if c["name"] == "core")
        orders = next(c for c in core["children"] if c["name"] == "orders.py")
        self.assertEqual(orders["lang"], "python")
        self.assertEqual(orders["kind"], "file")
        self.assertGreater(orders["size"], 0)
        self.assertEqual(orders["path"], "app/core/orders.py")

    def test_broken_symlink_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "real.py").write_text("x = 1\n")
            (root / "dead").symlink_to(root / "nonexistent")
            tree = scan(root)  # must not raise
            names = {c["name"] for c in tree["children"]}
            self.assertIn("real.py", names)
            self.assertNotIn("dead", names)

    def test_cyclic_symlink_does_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "real.py").write_text("x = 1\n")
            (root / "loop").symlink_to(root)
            tree = scan(root)  # must not raise / infinite-recurse
            names = {c["name"] for c in tree["children"]}
            self.assertIn("real.py", names)
            self.assertNotIn("loop", names)

if __name__ == "__main__":
    unittest.main()
