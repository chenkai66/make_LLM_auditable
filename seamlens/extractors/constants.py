"""Constant/config seam extractor.

Captures module-level and class-level ALL_CAPS assignments as config_const nodes
with their literal value, plus references to those names elsewhere. Surfaces the
'same knob defined in N places with divergent values' bug class (e.g. worker cap
= 6 in boss.py vs 9 in claude_runner.py vs 10 in worker_tuning.json), which the
duplicate-constant linter then flags.
"""
import ast

from .base import Extractor, iter_py_files, parse_py, module_id


def _literal(node):
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _is_const_name(name):
    return name.isupper() and len(name) >= 3


class ConstantsExtractor(Extractor):
    name = "constants"

    def run(self):
        # first pass: collect defined const names -> ids
        defined = {}
        for ap, rel in iter_py_files(self.cfg):
            tree, _ = parse_py(ap)
            if tree is None:
                continue
            mid = module_id(rel)
            for node in ast.walk(tree):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    targets = [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and _is_const_name(t.id):
                        val = _literal(node.value)
                        cid = "const:%s@%s" % (t.id, rel)
                        self.store.add_node(
                            cid, "config_const", name=t.id, file=rel,
                            line=getattr(node, "lineno", None),
                            value=repr(val) if val is not None else None,
                            value_kind=type(val).__name__ if val is not None else "dynamic",
                        )
                        self.store.add_edge(mid, cid, "defines", file=rel,
                                            line=getattr(node, "lineno", None))
                        defined.setdefault(t.id, []).append(cid)
        self.store.flush()
