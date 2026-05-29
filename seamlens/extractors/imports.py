"""Import-graph extractor.

Builds module --imports--> module edges (intra-project only). This gives every
module an in-degree (# of importers), which distinguishes a *library* (imported
elsewhere) from an *entrypoint* (only run directly). That distinction sharpens
the logger-trap linter (a top-level basicConfig is a trap only in a library) and
powers dead-module detection (0 importers + no __main__ guard = likely orphan,
e.g. the divergent legacy state_machine.py copy)."""
import ast

from .base import Extractor, iter_py_files, parse_py, module_id

# Top-level statement types that are "library-shaped" -- present in a module that
# is meant to be imported, not run. Anything else at module level (a bare call,
# a loop, a with-block) is script behavior that executes on `python3 file.py`.
_LIB_STMTS = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
              ast.ClassDef, ast.Assign, ast.AnnAssign, ast.AugAssign)


def _has_toplevel_exec(tree):
    """True if the module runs work at import/run time beyond definitions --
    i.e. it behaves like a script. A guard-less runnable script has this; a pure
    importable library (only defs/imports/constants) does not. Used to tell a
    dead library (0 importers, no exec) from a runnable entrypoint script."""
    for n in ast.iter_child_nodes(tree):
        if isinstance(n, _LIB_STMTS):
            continue
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant):
            continue                                  # module docstring
        if isinstance(n, ast.If):
            t = ast.dump(n.test)
            if "__name__" in t and "__main__" in t:
                continue                              # the entry guard itself
        return True
    return False


def _candidate_names(rel):
    """Importable dotted forms for a module path:
    'framework/boss.py' -> {'framework.boss', 'boss'} (+ package 'framework')."""
    noext = rel[:-3] if rel.endswith(".py") else rel
    parts = noext.replace("\\", "/").split("/")
    names = set()
    if parts[-1] == "__init__":
        parts = parts[:-1]
        if parts:
            names.add(".".join(parts))
    else:
        names.add(".".join(parts))
        names.add(parts[-1])
    return names


class ImportsExtractor(Extractor):
    name = "imports"

    def run(self):
        files = list(iter_py_files(self.cfg))
        # name -> set(relpath); leaf names may be ambiguous, keep all
        index = {}
        for ap, rel in files:
            for nm in _candidate_names(rel):
                index.setdefault(nm, set()).add(rel)

        has_main = {}
        toplevel_exec = {}
        for ap, rel in files:
            tree, _ = parse_py(ap)
            if tree is None:
                continue
            mid = module_id(rel)
            self.store.add_node(mid, "module", name=rel, file=rel)
            # record __main__ guard presence on the module node
            main = any(
                isinstance(n, ast.If) and "__main__" in ast.dump(n.test)
                for n in ast.iter_child_nodes(tree))
            has_main[rel] = main
            toplevel_exec[rel] = _has_toplevel_exec(tree)

            targets = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        targets.add(a.name)
                        targets.add(a.name.split(".")[0])
                elif isinstance(n, ast.ImportFrom):
                    if n.level == 0:
                        # Absolute. Resolve the module and each imported name;
                        # the latter matters for `from lib import dashboard_auth`
                        # (a submodule imported by name) -- without it every
                        # `from pkg import mod` target is missed and the submodule
                        # looks like a dead orphan.
                        if n.module:
                            targets.add(n.module)
                            targets.add(n.module.split(".")[-1])
                        for a in n.names:
                            if a.name == "*":
                                continue
                            if n.module:
                                targets.add(n.module + "." + a.name)
                            targets.add(a.name)
                    else:
                        # Relative (`from .adapter import X`, `from . import mod`).
                        # Resolve against this file's package: drop the filename,
                        # then climb `level` packages. Without this, package-style
                        # projects undercount imports and libraries look dead.
                        pkg = rel[:-3].replace("\\", "/").split("/")[:-1]
                        base = pkg[:len(pkg) - (n.level - 1)] if n.level >= 1 else pkg
                        prefix = ".".join(base)
                        if n.module:
                            full = (prefix + "." + n.module) if prefix else n.module
                            targets.add(full)
                            targets.add(n.module.split(".")[-1])
                            for a in n.names:
                                if a.name != "*":
                                    targets.add(full + "." + a.name)
                        else:
                            for a in n.names:
                                if a.name == "*":
                                    continue
                                targets.add((prefix + "." + a.name) if prefix else a.name)
                                targets.add(a.name)
            for t in targets:
                for dst_rel in index.get(t, ()):
                    if dst_rel == rel:
                        continue
                    self.store.add_edge(mid, module_id(dst_rel), "imports", file=rel)

        # annotate modules with importer count + has_main as standalone nodes
        indeg = {}
        for e in self.store.edges("imports"):
            indeg[e["dst"]] = indeg.get(e["dst"], 0) + 1
        for ap, rel in files:
            mid = module_id(rel)
            self.store.add_node(
                mid, "module", name=rel, file=rel,
                importers=indeg.get(mid, 0), has_main=has_main.get(rel, False),
                toplevel_exec=toplevel_exec.get(rel, False))
        self.store.flush()
