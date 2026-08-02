import json, subprocess, sys, unittest
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "toyrepo"
CODEMAP = Path(__file__).parent.parent / "codemap.py"


class TestCliScan(unittest.TestCase):
    def test_scan_prints_file_edges(self):
        # AGENTS.md's arrow-key contract depends on `scan` actually emitting
        # file_edges in its printed JSON (edges.build already computes it,
        # the CLI used to discard it). This is the executable proof.
        r = subprocess.run(
            [sys.executable, str(CODEMAP), "scan", str(FIX)],
            capture_output=True, text=True, check=True,
        )
        payload = json.loads(r.stdout)
        self.assertIn("file_edges", payload)
        self.assertIn(
            {"src": "app/core/orders.py", "dst": "app/core/store.py", "kind": "import"},
            payload["file_edges"],
        )


if __name__ == "__main__":
    unittest.main()
