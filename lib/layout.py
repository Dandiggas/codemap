W, H = 100, 62
GAP = 3
MAX_COLS = 8
MAX_ROWS = 8

def place(children: list, edges: list) -> dict:
    names = [c["name"] for c in children]
    if not names:
        return {}
    layer = _layers(names, edges or [])
    max_layer = max(layer.values())
    if max_layer >= MAX_COLS:  # compress deep chains proportionally (order preserved, ties allowed)
        layer = {n: round(l * (MAX_COLS - 1) / max_layer) for n, l in layer.items()}
    columns = {}
    for n in names:
        columns.setdefault(layer[n], []).append(n)
    ordered = []  # wrap overfull columns into extra columns to the right
    for ci in sorted(columns):
        col = columns[ci]
        for j in range(0, len(col), MAX_ROWS):
            ordered.append(col[j:j + MAX_ROWS])
    ncols = len(ordered)
    col_stride = (W - 2 * GAP) / ncols
    col_w = min(26.0, col_stride * 0.85)          # always positive
    pos = {}
    for ci, col in enumerate(ordered):
        rows = len(col)
        row_stride = (H - 2 * GAP) / rows
        box_h = min(14.0, row_stride * 0.85)      # always positive
        x = GAP + ci * col_stride
        for ri, n in enumerate(col):
            y = GAP + ri * row_stride
            pos[n] = [round(x, 1), round(y, 1), round(col_w, 1), round(box_h, 1)]
    return pos

def _layers(names, edges):
    valid = set(names)
    out_edges = {}
    indeg = {n: 0 for n in names}
    for e in edges:
        s, t = e["source"], e["target"]
        if s in valid and t in valid and s != t:
            out_edges.setdefault(s, []).append(t)
            indeg[t] += 1
    # longest-path layering via repeated relaxation (bounded — breaks cycles)
    layer = {n: 0 for n in names}
    for _ in range(len(names)):
        changed = False
        for s, targets in out_edges.items():
            for t in targets:
                if layer[t] < layer[s] + 1 and layer[s] + 1 < len(names):
                    layer[t] = layer[s] + 1
                    changed = True
        if not changed:
            break
    # cap sprawl: nodes with no edges at all go to the densest existing layer range
    max_layer = max(layer.values())
    loose = [n for n in names if indeg[n] == 0 and not out_edges.get(n)]
    for i, n in enumerate(loose):
        layer[n] = i % MAX_COLS
    return layer
