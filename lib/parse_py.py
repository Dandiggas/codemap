import ast

def parse(source: str) -> dict:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"symbols": [], "imports": [], "calls": []}
    symbols, imports, calls = [], [], []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append({"name": node.name, "kind": "fn", "line": node.lineno, "parent": None})
            calls.extend(_calls_in(node, node.name))
        elif isinstance(node, ast.ClassDef):
            symbols.append({"name": node.name, "kind": "class", "line": node.lineno, "parent": None})
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append({"name": item.name, "kind": "method",
                                    "line": item.lineno, "parent": node.name})
                    calls.extend(_calls_in(item, item.name))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            base = node.module or ""
            for a in node.names:
                dotted = f"{prefix}{base}.{a.name}" if base else f"{prefix}{a.name}"
                imports.append(dotted)
            if base:
                imports.append(f"{prefix}{base}")

    return {"symbols": symbols, "imports": sorted(set(imports)), "calls": calls}

def _calls_in(fn_node, fn_name):
    out = []
    stack = list(ast.iter_child_nodes(fn_node))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue  # nested definition: its calls are not ours
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name:
                out.append({"fn": fn_name, "callee": name})
        stack.extend(ast.iter_child_nodes(node))
    return out

def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None
