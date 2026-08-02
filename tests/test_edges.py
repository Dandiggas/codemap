import unittest
from pathlib import Path
from lib.scan import scan
from lib.edges import build

FIX = Path(__file__).parent / "fixtures" / "toyrepo"

class TestEdges(unittest.TestCase):
    def setUp(self):
        self.tree = scan(FIX)
        self.flat = build(self.tree, FIX)

    def _node(self, *names):
        node = self.tree
        for n in names:
            node = next(c for c in node["children"] if c["name"] == n)
        return node

    def test_file_symbols_attached(self):
        orders = self._node("app", "core", "orders.py")
        self.assertIn("approve", {s["name"] for s in orders["symbols"]})

    def test_sibling_file_edge(self):
        core = self._node("app", "core")
        pairs = {(e["source"], e["target"]) for e in core["edges"]}
        self.assertIn(("orders.py", "store.py"), pairs)

    def test_aggregated_dir_edge(self):
        app = self._node("app")
        pairs = {(e["source"], e["target"]) for e in app["edges"]}
        self.assertIn(("core", "adapters"), pairs)  # orders.py -> mailer.py, lifted

    def test_ts_relative_import(self):
        web = self._node("web")
        pairs = {(e["source"], e["target"]) for e in web["edges"]}
        self.assertIn(("index.ts", "client.ts"), pairs)

    def test_no_self_edges(self):
        for node in [self._node("app"), self._node("app", "core")]:
            for e in node["edges"]:
                self.assertNotEqual(e["source"], e["target"])

    def test_python_relative_import_resolution(self):
        from lib.edges import _resolve
        by_path = {"app/core/store.py": {}, "app/adapters/mailer.py": {}}
        self.assertEqual(_resolve(".store", "app/core/orders.py", by_path), "app/core/store.py")
        self.assertEqual(_resolve("..adapters.mailer", "app/core/orders.py", by_path), "app/adapters/mailer.py")

    def test_ts_resolution_cwd_independent(self):
        from lib.edges import _resolve
        by_path = {"web/client.ts": {}}
        self.assertEqual(_resolve("./client", "web/index.ts", by_path), "web/client.ts")

if __name__ == "__main__":
    unittest.main()
