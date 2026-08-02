import unittest
from lib.layout import place

def boxes_overlap(a, b):
    ax, ay, aw, ah = a; bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah

class TestLayout(unittest.TestCase):
    def _children(self, names):
        return [{"name": n, "kind": "file", "size": 100, "children": [], "symbols": []} for n in names]

    def test_bounds_and_no_overlap(self):
        children = self._children(["a", "b", "c", "d", "e", "f", "g"])
        edges = [{"source": "a", "target": "b"}, {"source": "b", "target": "c"}]
        pos = place(children, edges)
        self.assertEqual(set(pos), {"a", "b", "c", "d", "e", "f", "g"})
        for name, (x, y, w, h) in pos.items():
            self.assertGreaterEqual(x, 0); self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + w, 100); self.assertLessEqual(y + h, 62)
        names = list(pos)
        for i, n1 in enumerate(names):
            for n2 in names[i+1:]:
                self.assertFalse(boxes_overlap(pos[n1], pos[n2]), f"{n1} overlaps {n2}")

    def test_flow_direction(self):
        children = self._children(["src", "mid", "sink"])
        edges = [{"source": "src", "target": "mid"}, {"source": "mid", "target": "sink"}]
        pos = place(children, edges)
        self.assertLess(pos["src"][0], pos["mid"][0])
        self.assertLess(pos["mid"][0], pos["sink"][0])

    def test_no_edges_grid(self):
        children = self._children([f"n{i}" for i in range(12)])
        pos = place(children, [])
        self.assertEqual(len(pos), 12)  # falls back to grid, everything placed

    def test_many_isolated_nodes_positive_boxes(self):
        children = self._children([f"n{i}" for i in range(40)])
        pos = place(children, [])
        self.assertEqual(len(pos), 40)
        for name, (x, y, w, h) in pos.items():
            self.assertGreater(w, 0, name); self.assertGreater(h, 0, name)
            self.assertGreaterEqual(x, 0); self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + w, 100); self.assertLessEqual(y + h, 62)
        names = list(pos)
        for i, a in enumerate(names):
            for b in names[i+1:]:
                self.assertFalse(boxes_overlap(pos[a], pos[b]), f"{a} overlaps {b}")

    def test_deep_chain_positive_boxes(self):
        names = [f"c{i}" for i in range(33)]
        children = self._children(names)
        edges = [{"source": f"c{i}", "target": f"c{i+1}"} for i in range(32)]
        pos = place(children, edges)
        self.assertEqual(len(pos), 33)
        for name, (x, y, w, h) in pos.items():
            self.assertGreater(w, 0, name); self.assertGreater(h, 0, name)
            self.assertLessEqual(x + w, 100); self.assertLessEqual(y + h, 62)

if __name__ == "__main__":
    unittest.main()
