"""Import-graph extractor.

Builds module --imports--> module edges (intra-project only). This gives every
module an in-degree (# of importers), which distinguishes a *library* (imported
elsewhere) from an *entrypoint* (only run directly). That distinction sharpens
the logger-trap linter (a top-level basicConfig is a trap only in a library) and
powers dead-module detection (0 importers + no __main__ guard = likely orphan,
e.g. the divergent legacy state_machine.py copy)."""
import ast

from .base import Extractor, iter_py_files, parse_py, module_id


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

            targets = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        targets.add(a.name)
                        targets.add(a.name.split(".")[0])
                elif isinstance(n, ast.ImportFrom) and n.level == 0:
                    # Resolve both the module and each imported name. The latter
                    # matters for `from lib import dashboard_auth` (a submodule
                    # imported by name) -- without it every `from pkg import mod`
                    # target is missed and the submodule looks like a dead orphan.
                    if n.module:
                        targets.add(n.module)
                        targets.add(n.module.split(".")[-1])
                    for a in n.names:
                        if a.name == "*":
                            continue
                        if n.module:
                            targets.add(n.module + "." + a.name)
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
                importers=indeg.get(mid, 0), has_main=has_main.get(rel, False))
        self.store.flush()
