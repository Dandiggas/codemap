import unittest
from pathlib import Path
from lib.parse_py import parse

SRC = (Path(__file__).parent / "fixtures" / "toyrepo" / "app" / "core" / "orders.py").read_text()

class TestParsePy(unittest.TestCase):
    def test_symbols(self):
        r = parse(SRC)
        by_name = {s["name"]: s for s in r["symbols"]}
        self.assertEqual(by_name["OrderBook"]["kind"], "class")
        self.assertEqual(by_name["approve"]["kind"], "fn")
        self.assertEqual(by_name["add"]["kind"], "method")
        self.assertEqual(by_name["add"]["parent"], "OrderBook")
        self.assertEqual(by_name["approve"]["line"], 10)

    def test_imports(self):
        r = parse(SRC)
        self.assertIn("app.core.store", r["imports"])
        self.assertIn("app.adapters.mailer", r["imports"])
        self.assertIn("json", r["imports"])

    def test_calls(self):
        r = parse(SRC)
        callees = {(c["fn"], c["callee"]) for c in r["calls"]}
        self.assertIn(("approve", "store.load"), callees)
        self.assertIn(("approve", "mailer.send"), callees)
        self.assertIn(("add", "_audit"), callees)

    def test_relative_imports_preserved(self):
        r = parse("from .sibling import helper\nfrom . import util\n")
        self.assertIn(".sibling.helper", r["imports"])
        self.assertIn(".sibling", r["imports"])
        self.assertIn(".util", r["imports"])
        self.assertNotIn("sibling", r["imports"])

    def test_nested_function_calls_not_misattributed(self):
        src = "def outer():\n    def inner():\n        hidden()\n    direct()\n"
        r = parse(src)
        callees = {(c["fn"], c["callee"]) for c in r["calls"]}
        self.assertIn(("outer", "direct"), callees)
        self.assertNotIn(("outer", "hidden"), callees)

    def test_nested_call_arguments_still_captured(self):
        r = parse("def f():\n    g(h())\n")
        callees = {(c["fn"], c["callee"]) for c in r["calls"]}
        self.assertIn(("f", "g"), callees)
        self.assertIn(("f", "h"), callees)

if __name__ == "__main__":
    unittest.main()
