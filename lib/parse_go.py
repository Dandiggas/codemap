import re

TYPE_RE = re.compile(r'^type\s+(\w+)\s+(?:struct|interface)\b', re.M)
FUNC_RE = re.compile(r'^func\s+(?:\((\w+)\s+\*?(\w+)\)\s+)?(\w+)\s*\(', re.M)
IMPORT_ONE_RE = re.compile(r'^import\s+"([^"]+)"', re.M)
IMPORT_BLOCK_RE = re.compile(r'^import\s+\(([^)]*)\)', re.M | re.S)
IN_BLOCK_RE = re.compile(r'"([^"]+)"')

def parse(source: str) -> dict:
    symbols = []
    for m in TYPE_RE.finditer(source):
        line = source.count("\n", 0, m.start()) + 1
        symbols.append({"name": m.group(1), "kind": "class", "line": line, "parent": None})
    for m in FUNC_RE.finditer(source):
        line = source.count("\n", 0, m.start()) + 1
        receiver_type = m.group(2)
        symbols.append({
            "name": m.group(3), "kind": "method" if receiver_type else "fn",
            "line": line, "parent": receiver_type,
        })
    symbols.sort(key=lambda s: s["line"])
    imports = [m.group(1) for m in IMPORT_ONE_RE.finditer(source)]
    for m in IMPORT_BLOCK_RE.finditer(source):
        imports.extend(IN_BLOCK_RE.findall(m.group(1)))
    return {"symbols": symbols, "imports": sorted(set(imports)), "calls": []}
