import json, unittest, tempfile
from pathlib import Path
from unittest import mock
from lib.scan import scan
from lib.edges import build
from lib.infra import scan_infra
from lib.render import render, write

FIX = Path(__file__).parent / "fixtures" / "toyrepo"
IAC_FIX = Path(__file__).parent / "fixtures" / "iacrepo"

def _payload(html):
    """The baked DATA object, parsed back out of the rendered page."""
    marker = "const DATA = "
    i = html.index(marker) + len(marker)
    raw = html[i:html.index("\n", i)].rstrip().rstrip(";")
    return json.loads(raw)


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

    def test_zero_code_children_collapse_into_group(self):
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir(); (root / "src" / "app.py").write_text("x = 1\n")
            (root / "db.sqlite").write_text("bin")
            (root / "db.sqlite-wal").write_text("bin")
            (root / "notes.md").write_text("hi")
            tree = scan(root); build(tree, root)
            html = render(tree, scan_infra(root), {}, root, "vscode")
            self.assertIn('"data & docs"', html)
            self.assertIn('"collapsed_count": 3', html)

    def test_no_collapse_when_all_children_are_code(self):
        tree = scan(FIX)
        build(tree, FIX)
        html = render(tree, scan_infra(FIX), {}, FIX, "vscode")
        # app/ level: core/ and adapters/ both contain code, no group forms there
        self.assertNotIn('"name": "core", "kind": "group"', html)
        # root has .github (zero-code, exempt) + Dockerfile (zero-code, lone
        # candidate: needs 2+ to collapse) alongside code-bearing app/web/main.go -
        # nothing to collapse at root either, so all of these stay top-level boxes
        self.assertIn('"name": "app", "kind": "dir"', html)
        self.assertIn('"name": "web", "kind": "dir"', html)
        self.assertIn('"name": "main.go", "kind": "file"', html)

class TestRenderIac(unittest.TestCase):
    """DATA.iac: the System tab's infra model is baked by the renderer itself
    (scan_iac over the repo root), so every map carries it without a CLI change."""

    def _iac_html(self, labels=None):
        tree = scan(IAC_FIX)
        build(tree, IAC_FIX)
        return render(tree, scan_infra(IAC_FIX), labels or {}, IAC_FIX, "vscode")

    def test_iac_model_baked(self):
        html = self._iac_html()
        self.assertIn('"iac"', html)
        # a role, its grant actions and its grant target all reach the payload
        self.assertIn('"lambda_exec"', html)
        self.assertIn('"dynamodb:PutItem"', html)
        self.assertIn('"tf:.::aws_dynamodb_table.orders"', html)
        # a via-role connection carries its mechanism and the role name
        self.assertIn('"via role"', html)
        # resources keep their file:line evidence
        self.assertIn('"source_file": "main.tf"', html)

    def test_zero_iac_repo_bakes_empty_model(self):
        # toyrepo has no IaC at all: the model is present but empty, which is
        # what the template keys off to keep rendering the legacy System view.
        tree = scan(FIX)
        build(tree, FIX)
        html = render(tree, scan_infra(FIX), {}, FIX, "vscode")
        self.assertIn('"iac": {"resources": [], "roles": [], "connections": [], "deployed": {}}', html)

    def test_deployed_state_baked_from_fetch_deployed(self):
        # render() calls lib.deployed.fetch_deployed(root) and bakes its result
        # onto iac["deployed"]; fetch_deployed itself is best-effort/network-y
        # (see test_deployed.py), so here it's mocked to prove only the wiring.
        fake = {"workflows": [{"workflow": "deploy", "status": "completed",
                                "conclusion": "success", "sha": "abc1234",
                                "at": "2026-08-01T00:00:00Z", "url": "https://x"}],
                "fetched_at": "2026-08-01T00:05:00Z"}
        with mock.patch("lib.render.fetch_deployed", return_value=fake):
            html = self._iac_html()
        deployed = _payload(html)["iac"]["deployed"]
        self.assertEqual(deployed, fake)

    def test_no_gh_skips_the_fetch_entirely(self):
        # --no-gh is the documented opt-out from the only network call a
        # render can make, so it must not merely discard the result: the
        # fetch must never happen (a cached-but-stale deployed.json read
        # would still count as "not skipped" for the payload check alone).
        fake = {"workflows": [{"workflow": "deploy", "status": "completed",
                                "conclusion": "success", "sha": "abc1234",
                                "at": "2026-08-01T00:00:00Z", "url": "https://x"}],
                "fetched_at": "2026-08-01T00:05:00Z"}
        tree = scan(IAC_FIX)
        build(tree, IAC_FIX)
        with mock.patch("lib.render.fetch_deployed", return_value=fake) as fetched:
            html = render(tree, scan_infra(IAC_FIX), {}, IAC_FIX, "vscode", fetch_gh=False)
        fetched.assert_not_called()
        self.assertEqual(_payload(html)["iac"]["deployed"], {})

    def test_infra_labels_merge_onto_roles_and_connections(self):
        labels = {
            "infra": {
                "roles": {"tf:.::aws_iam_role.lambda_exec": {"why": "lets the order lambda touch orders"}},
                "connections": {"tf:.::aws_lambda_function.process_order→tf:.::aws_dynamodb_table.orders":
                                "writes each approved order"},
            }
        }
        iac = _payload(self._iac_html(labels))["iac"]
        role = next(r for r in iac["roles"] if r["id"] == "tf:.::aws_iam_role.lambda_exec")
        self.assertEqual(role["why"], "lets the order lambda touch orders")
        conn = next(c for c in iac["connections"]
                    if c["src"] == "tf:.::aws_lambda_function.process_order"
                    and c["dst"] == "tf:.::aws_dynamodb_table.orders")
        self.assertEqual(conn["why"], "writes each approved order")
        # an unlabeled role stays unlabeled rather than inheriting anything
        other = next(r for r in iac["roles"] if r["id"] == "tf:.::aws_iam_role.reader_role")
        self.assertNotIn("why", other)

    def test_infra_labels_for_unknown_ids_are_ignored(self):
        # a stale label key (renamed role) must not invent a role or crash
        html = self._iac_html({"infra": {"roles": {"aws_iam_role.gone": {"why": "nope"}},
                                          "connections": {"a→b": "nope either"}}})
        self.assertIn('"iac"', html)


if __name__ == "__main__":
    unittest.main()
