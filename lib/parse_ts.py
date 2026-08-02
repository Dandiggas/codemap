import re

IMPORT_RE = re.compile(r'''(?:import\s[^'"]*?from\s+|import\s+|require\()\s*['"]([^'"]+)['"]''')
CLASS_RE = re.compile(r'^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)', re.M)
FN_RE = re.compile(r'^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)', re.M)
ARROW_RE = re.compile(r'^\s*(?:export\s+)?(?:const|let)\s+(\w+)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*(?::[^=]+)?=>', re.M)

def parse(source: str) -> dict:
    symbols = []
    for regex, kind in ((CLASS_RE, "class"), (FN_RE, "fn"), (ARROW_RE, "fn")):
        for m in regex.finditer(source):
            line = source.count("\n", 0, m.start(1)) + 1
            symbols.append({"name": m.group(1), "kind": kind, "line": line, "parent": None})
    symbols.sort(key=lambda s: s["line"])
    imports = sorted({m.group(1) for m in IMPORT_RE.finditer(source)})
    return {"symbols": symbols, "imports": imports, "calls": []}
