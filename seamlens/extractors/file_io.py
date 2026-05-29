"""File-I/O seam extractor.

Builds  module --writes/reads--> artifact  edges keyed by the *filename literal*
used (basename), because cross-component coupling is by filename, not variable.
This is what surfaces producer/consumer mismatches: e.g. experimenter writes
artifact:results_summary.json while invariant_checker only reads
artifact:done.json / artifact:results.json -> a 'designed' experiment is healed
back forever. The graph makes that missing overlap a queryable fact.
"""
import ast
import os

from .base import Extractor, iter_py_files, parse_py, module_id, call_name

_WRITE_MODES = ("w", "a", "x")
# existence/stat probes -- for signal & marker files this IS the read
_PROBE_LEAVES = {"exists", "isfile", "isdir", "lexists", "getsize", "stat", "getmtime"}
# atomic-write idiom: write to <name>.tmp then move it into place. The producer
# action is the move, not the open(tmp,'w') -- without this the artifact looks
# read-only and shows as a false orphan (the dominant orphan_artifact FP). Matched
# by fully-qualified name so str.replace() doesn't false-trigger; the destination
# (2nd positional arg) is the produced basename.
_MOVE_CALLS = ("os.rename", "os.replace", "shutil.move", "shutil.copy",
               "shutil.copy2", "shutil.copyfile")


def _path_literals(node):
    """Best-effort filename literals (basenames) referenced by a path expression.
    Handles string constants, os.path.join(..., 'name'), f-strings ending in a
    literal, and 'dir' + '/name' concatenation."""
    out = []

    def add(s):
        if isinstance(s, str) and s and not s.startswith("{"):
            b = os.path.basename(s.rstrip("/"))
            if b and ("." in b or b.isidentifier() or "/" not in b):
                out.append(b)

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        add(node.value)
    elif isinstance(node, ast.Call):
        cn = call_name(node) or ""
        if cn.endswith("join"):  # os.path.join(...) -- last string arg is the leaf
            for a in reversed(node.args):
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    add(a.value)
                    break
    elif isinstance(node, ast.JoinedStr):  # f"{dir}/done.json"
        for v in reversed(node.values):
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                add(v.value)
                break
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        if isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
            add(node.right.value)
    return out


def _open_mode(node):
    """Return 'write' or 'read' for an open() call."""
    mode = None
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        mode = node.args[1].value
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    if isinstance(mode, str) and any(m in mode for m in _WRITE_MODES):
        return "write"
    return "read"


class FileIOExtractor(Extractor):
    name = "file_io"

    def run(self):
        helpers = self.cfg.get("io_helpers", {})
        read_fns = set(helpers.get("read", []))
        write_fns = set(helpers.get("write", []))
        seen_art = set()

        write_leaves = {f.rsplit(".", 1)[-1] for f in write_fns}
        read_leaves = {f.rsplit(".", 1)[-1] for f in read_fns}

        for ap, rel in iter_py_files(self.cfg):
            tree, _ = parse_py(ap)
            if tree is None:
                continue
            mid = module_id(rel)
            self.store.add_node(mid, "module", name=rel, file=rel)

            # pass 1: var -> filename literals, for `p = os.path.join(d,'done.json')`
            # patterns later consumed as open(p)/os.path.exists(p).
            var_fnames = {}
            for n in ast.walk(tree):
                if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                        and isinstance(n.targets[0], ast.Name):
                    lits = _path_literals(n.value)
                    if lits:
                        var_fnames.setdefault(n.targets[0].id, set()).update(lits)

            def literals_of(arg):
                lits = _path_literals(arg)
                if not lits and isinstance(arg, ast.Name):
                    lits = list(var_fnames.get(arg.id, []))
                return lits

            for n in ast.walk(tree):
                if not isinstance(n, ast.Call):
                    continue
                cn = call_name(n) or ""
                short = cn.rsplit(".", 1)[-1]
                direction = None
                path_arg = None

                if short == "open" and n.args:
                    direction = _open_mode(n)
                    path_arg = n.args[0]
                elif cn in _MOVE_CALLS and len(n.args) >= 2:
                    direction = "write"          # move/copy produces the dest
                    path_arg = n.args[1]
                elif short in _PROBE_LEAVES and n.args:
                    direction = "read"          # existence/stat probe == read of a marker
                    path_arg = n.args[0]
                elif cn in write_fns or short in write_leaves:
                    direction = "write"
                    path_arg = _first_path_arg(n)
                elif cn in read_fns or short in read_leaves:
                    direction = "read"
                    path_arg = _first_path_arg(n)

                if direction and path_arg is not None:
                    for fname in literals_of(path_arg):
                        aid = "artifact:" + fname
                        if aid not in seen_art:
                            self.store.add_node(aid, "artifact", name=fname)
                            seen_art.add(aid)
                        self.store.add_edge(
                            mid, aid, "writes" if direction == "write" else "reads",
                            file=rel, line=getattr(n, "lineno", None), via=cn,
                        )
        self.store.flush()


def _first_path_arg(node):
    for a in node.args:
        return a
    return None
