import unittest, tempfile
from pathlib import Path
from lib.scan import scan
from lib.edges import build
from lib.infra import scan_infra
from lib.render import render, write

FIX = Path(__file__).parent / "fixtures" / "toyrepo"

class TestRender(unittest.TestCase):
    def _html(self):
        tree = scan(FIX)
        build(tree, FIX)
        return render(tree, scan_infra(FIX), {}, FIX, "vscode")

    def test_selfcontained_and_data_baked(self):
        html = self._html()
        self.assertNotIn("__CODEMAP_DATA__", html)
        self.assertIn('"orders.py"', html)
        self.assertIn("api.sendgrid.com", html)          # infra baked
        self.assertIn("codemap-freshness.js", html)      # layer-2 hook point
        self.assertNotIn("http://cdn", html)
        self.assertNotIn("https://cdn", html)

    def test_meta_and_editor(self):
        html = self._html()
        self.assertIn('"editor_scheme": "vscode"', html)
        self.assertIn('"generated_iso"', html)

    def test_snippet_baked(self):
        html = self._html()
        self.assertIn("def approve(order_id):", html)    # peek snippet present

    def test_write_creates_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = write("<html>x</html>", Path(td))
            self.assertTrue(path.exists())
            self.assertEqual(path.name, "codemap.html")
            self.assertEqual(path.parent.name, ".codemap")

    def test_labels_reach_the_html(self):
        tree = scan(FIX)
        build(tree, FIX)
        labels = {
            "files": {"app/core/orders.py": {"sha1": "x", "caption": "Order approval and audit",
                       "symbol_captions": {"approve": "Approves one pending order"}}},
            "arrows": {"app/core/orders.py→app/core/store.py": "orders persisted"},
            "lenses": {"approval flow": ["app/core/orders.py"]},
            "overview": {"summary": "Toy order system.",
                          "externals": {"api.sendgrid.com": {"why": "sends order emails"}}},
        }
        html = render(tree, scan_infra(FIX), labels, FIX, "vscode")
        for needle in ("Order approval and audit", "Approves one pending order",
                       "orders persisted", "approval flow", "sends order emails"):
            self.assertIn(needle, html)

    def test_html_escapes_script_close(self):
        # A repo file whose SOURCE contains </script>, <!--, and a bare <script
        # must not terminate the baked JSON's script block or reach the DOM
        # unescaped (double-escape guard: <!-- also opens "script data" state
        # inside an inline <script> per the HTML spec, independent of </script).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "evil.py").write_text(
                'def payload():\n'
                '    return "</script><script>alert(1)<!--<script>alert(2)</script>"\n'
            )
            tree = scan(root)
            build(tree, root)
            html = render(tree, scan_infra(root), {}, root, "vscode")
            # none of the raw hostile sequences from the snippet may appear verbatim
            self.assertNotIn('return "</script>', html)
            self.assertNotIn('<!--<script', html)
            self.assertNotIn('<script>alert', html)
            # the guard's escaped artifact must be present in the baked JSON
            self.assertIn('\\u003cscript', html)
            self.assertIn('\\u003c!--', html)

if __name__ == "__main__":
    unittest.main()
