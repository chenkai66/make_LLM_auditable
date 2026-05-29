"""Extractor base + shared Python-source iteration. Extractors populate the
graph; each is independent and can be enabled/disabled. All are language-Python
(AST) for now; the graph schema itself is language-agnostic."""
import ast
import os


def iter_py_files(cfg):
    """Yield (abspath, relpath) for every .py file under cfg roots, honoring
    excludes. Exclusion matches path *segments* (relative to project root), not
    raw substrings -- so an exclude of 'data' never accidentally matches a
    project rooted under /data/..."""
    excl = set(cfg.get("exclude", []))
    seen = set()

    def excluded(rel):
        return any(seg in excl for seg in rel.split(os.sep))

    for root in cfg.get("roots", ["."]):
        base = cfg.abspath(root)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            rel_dir = os.path.relpath(dirpath, cfg.project_root)
            dirnames[:] = [d for d in dirnames if d not in excl]
            if rel_dir != "." and excluded(rel_dir):
                continue
            for fn in filenames:
                if fn.endswith(".py"):
                    ap = os.path.join(dirpath, fn)
                    if ap in seen:
                        continue
                    seen.add(ap)
                    yield ap, os.path.relpath(ap, cfg.project_root)


def parse_py(abspath):
    try:
        with open(abspath, "r", encoding="utf-8") as f:
            src = f.read()
        return ast.parse(src, filename=abspath), src
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None, None


def module_id(relpath):
    """Stable node id for a module: 'mod:framework/boss.py'."""
    return "mod:" + relpath.replace("\\", "/")


def const_node_string(node):
    """Return the python literal value of an ast node if it's a simple constant
    (str/num/bool/None) or a tuple/list/set of them, else None."""
    if isinstance(node, ast.Constant):
        return node.value
    return None


def call_name(node):
    """Best-effort dotted name of a Call's func: open / json.load / self.foo.bar."""
    f = node.func
    parts = []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    return ".".join(reversed(parts)) if parts else None


class Extractor:
    name = "base"

    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store

    def run(self):
        raise NotImplementedError
