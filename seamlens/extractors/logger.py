"""Logger-seam extractor.

The trap: importing/calling `install_fn` (e.g. error_logger.install) adds a
handler to the ROOT logger, which makes a later `logging.basicConfig()` a no-op
-> the daemon's stdout/stderr logging silently disappears (root cause of the
16-day kg_patch_merger silent failure). For each module we record whether it:
  - imports the installer's root module   (imports_installer)
  - calls install_fn                       (installs)
  - calls framework_setup e.g. basicConfig (calls_setup)
  - has an explicit StreamHandler/addHandler (has_explicit_handler)
  - whether ALL its setup calls sit inside `if __name__ == '__main__'`
    (setup_in_main) -- a standalone CLI runs in its own process where the
    installer never fired, so a main-guarded basicConfig is SAFE.
The logger-trap linter combines these for a high-precision verdict.
"""
import ast

from .base import Extractor, iter_py_files, parse_py, module_id, call_name


def _main_guard_spans(tree):
    """Line spans of top-level `if __name__ == '__main__':` blocks."""
    spans = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.If):
            t = node.test
            srcs = ast.dump(t)
            if "__name__" in srcs and "__main__" in srcs:
                start = node.lineno
                end = max((getattr(n, "lineno", start) for n in ast.walk(node)),
                          default=start)
                spans.append((start, end))
    return spans


def _in_spans(line, spans):
    return any(a <= line <= b for (a, b) in spans)


class LoggerExtractor(Extractor):
    name = "logger"

    def run(self):
        lg = self.cfg.get("logger", {})
        install_fn = lg.get("install_fn")
        setup_fn = lg.get("framework_setup", "logging.basicConfig")
        markers = lg.get("explicit_handler_markers", ["StreamHandler", "addHandler"])
        install_leaf = install_fn.rsplit(".", 1)[-1] if install_fn else None
        setup_leaf = setup_fn.rsplit(".", 1)[-1] if setup_fn else None
        install_root = install_fn.split(".")[0] if install_fn else None

        for ap, rel in iter_py_files(self.cfg):
            tree, src = parse_py(ap)
            if tree is None:
                continue
            mid = module_id(rel)
            spans = _main_guard_spans(tree)
            installs = calls_setup = explicit = imports_installer = False
            setup_lines = []
            iline = None

            for n in ast.walk(tree):
                if isinstance(n, (ast.Import, ast.ImportFrom)) and install_root:
                    names = []
                    if isinstance(n, ast.Import):
                        names = [a.name for a in n.names]
                    elif n.module:
                        names = [n.module]
                    if any(nm.split(".")[0] == install_root for nm in names):
                        imports_installer = True
                if isinstance(n, ast.Call):
                    leaf = (call_name(n) or "").rsplit(".", 1)[-1]
                    if install_leaf and leaf == install_leaf:
                        installs = True
                        iline = getattr(n, "lineno", None)
                    if setup_leaf and leaf == setup_leaf:
                        calls_setup = True
                        setup_lines.append(getattr(n, "lineno", 0))
                    if leaf in markers:
                        explicit = True
            if not explicit and any(m in (src or "") for m in markers):
                explicit = True

            if installs or calls_setup:
                setup_in_main = bool(setup_lines) and all(
                    _in_spans(l, spans) for l in setup_lines)
                lid = "logger:" + rel
                self.store.add_node(
                    lid, "logger_install", name=rel, file=rel,
                    line=iline or (setup_lines[0] if setup_lines else None),
                    installs=installs, calls_setup=calls_setup,
                    has_explicit_handler=explicit,
                    imports_installer=imports_installer,
                    setup_in_main=setup_in_main,
                )
                self.store.add_edge(mid, lid, "logger_seam", file=rel)
        self.store.flush()
