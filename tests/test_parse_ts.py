import unittest
from pathlib import Path
from lib.parse_ts import parse

SRC = (Path(__file__).parent / "fixtures" / "toyrepo" / "web" / "index.ts").read_text()

class TestParseTs(unittest.TestCase):
    def test_symbols(self):
        r = parse(SRC)
        by_name = {s["name"]: s for s in r["symbols"]}
        self.assertEqual(by_name["Panel"]["kind"], "class")
        self.assertEqual(by_name["main"]["kind"], "fn")
        self.assertEqual(by_name["main"]["line"], 7)

    def test_imports(self):
        r = parse(SRC)
        self.assertIn("./client", r["imports"])

    def test_arrow_and_require(self):
        r = parse('const load = async (x) => x\nconst db = require("pg")\n')
        names = {s["name"] for s in r["symbols"]}
        self.assertIn("load", names)
        self.assertIn("pg", r["imports"])

if __name__ == "__main__":
    unittest.main()
