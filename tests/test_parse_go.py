import unittest
from pathlib import Path
from lib.parse_go import parse

SRC = (Path(__file__).parent / "fixtures" / "toyrepo" / "main.go").read_text()

class TestParseGo(unittest.TestCase):
    def test_symbols(self):
        r = parse(SRC)
        by_name = {s["name"]: s for s in r["symbols"]}
        self.assertEqual(by_name["Server"]["kind"], "class")
        self.assertEqual(by_name["Handle"]["kind"], "method")
        self.assertEqual(by_name["Handle"]["parent"], "Server")
        self.assertEqual(by_name["main"]["kind"], "fn")

    def test_imports(self):
        r = parse(SRC)
        self.assertIn("net/http", r["imports"])

    def test_import_block(self):
        r = parse('package x\nimport (\n\t"fmt"\n\t"os/exec"\n)\n')
        self.assertEqual(r["imports"], ["fmt", "os/exec"])

if __name__ == "__main__":
    unittest.main()
