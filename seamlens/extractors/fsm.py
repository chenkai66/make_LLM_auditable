"""FSM-seam extractor.

Reads explicit transition tables declared in config as
'relpath:DICT_NAME' where the dict literal maps state -> iterable of next states
(e.g. framework/pipeline_state.py:PROTOCOL_TRANSITIONS). Emits fsm_state nodes
and transitions_to edges so linters can check that every artifact-accepting /
auto-heal target state is actually reachable, and that validators agree with the
declared terminal set (root cause of the 'analyzed' TERMINAL false-positive).
"""
import ast
import os

from .base import parse_py


class FSMExtractor:
    name = "fsm"

    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store

    def run(self):
        for spec in self.cfg.get("fsm_sources", []) or []:
            if ":" not in spec:
                continue
            rel, dname = spec.rsplit(":", 1)
            ap = self.cfg.abspath(rel)
            if not os.path.exists(ap):
                continue
            tree, _ = parse_py(ap)
            if tree is None:
                continue
            table = self._find_dict(tree, dname)
            if table is None:
                continue
            fsm = "fsm:" + dname
            self.store.add_node(fsm, "fsm", name=dname, file=rel)
            for state, nexts in table.items():
                sid = "fsm_state:%s/%s" % (dname, state)
                self.store.add_node(sid, "fsm_state", name=str(state), file=rel,
                                    fsm=dname, terminal=(not nexts))
                self.store.add_edge(fsm, sid, "has_state", file=rel)
                for nx in (nexts or []):
                    nid = "fsm_state:%s/%s" % (dname, nx)
                    self.store.add_edge(sid, nid, "transitions_to", file=rel)
            self.store.flush()

    @staticmethod
    def _find_dict(tree, dname):
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == dname:
                        try:
                            val = ast.literal_eval(node.value)
                        except Exception:
                            val = _coerce_set_dict(node.value)
                        if isinstance(val, dict):
                            return {k: list(v) if v else [] for k, v in val.items()}
        return None


def _coerce_set_dict(node):
    """literal_eval can't handle dict-of-set with names; do a shallow manual parse
    of {'a': {'b','c'}} where keys/values are string constants."""
    if not isinstance(node, ast.Dict):
        return None
    out = {}
    for k, v in zip(node.keys, node.values):
        if not isinstance(k, ast.Constant):
            continue
        vals = []
        if isinstance(v, (ast.Set, ast.List, ast.Tuple)):
            for e in v.elts:
                if isinstance(e, ast.Constant):
                    vals.append(e.value)
        out[k.value] = vals
    return out
