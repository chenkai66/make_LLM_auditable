"""Per-project configuration. The engine is generic; everything project-specific
lives in a single `seamlens.yaml` at the project root (or passed via --config).

This is the ONLY host-aware surface -- mirrors kaizen's "only adapters/ are
host-aware" principle. Other team members adopt seamlens by writing this file,
not by touching engine code.
"""
import os

try:
    import yaml
    _HAVE_YAML = True
except Exception:
    _HAVE_YAML = False
import json

DEFAULTS = {
    # dirs to scan, relative to project root
    "roots": ["."],
    # path fragments to skip
    "exclude": ["__pycache__", ".git", "node_modules", ".seamlens",
                "_archive", "_legacy_archive", "backups", "venv", ".venv"],
    # helper functions that read/write files, beyond builtin open(). Used by the
    # file-io extractor to resolve producer/consumer edges through wrappers.
    "io_helpers": {
        "read":  ["read_json", "load_json", "json.load", "read_text"],
        "write": ["write_json", "save_json", "dump_json", "atomic_write", "write_text"],
    },
    # data dirs that hold runtime artifacts (for orphan-artifact linting)
    "artifact_roots": [],
    # logger seam: installing `install_fn` mutates the root logger so a later
    # `framework_setup` call becomes a no-op (the basicConfig trap). Daemons that
    # import install_fn must add an explicit stream handler.
    "logger": {
        "install_fn": None,           # e.g. "error_logger.install"
        "framework_setup": "logging.basicConfig",
        "explicit_handler_markers": ["StreamHandler", "addHandler"],
    },
    # test vs prod path discrimination (test-writes-to-prod linter)
    "test_file_markers": ["test_", "_test", "/tests/", "conftest"],
    "prod_path_markers": [],          # e.g. ["/data/research-agent/logs", "graph.db"]
    # FSM: dotted path of a dict literal mapping state -> set/list of next states
    "fsm_sources": [],                # e.g. ["framework/pipeline_state.py:PROTOCOL_TRANSITIONS"]
    # topology inputs
    "systemd_units": [],              # absolute paths or globs
    "cron_files": [],                 # absolute paths
    # Layer 2 curated semantic overlay
    "semantic_overlay": "seamlens.semantic.yaml",
    # where the graph db is written (relative to project root)
    "db_path": ".seamlens/system_graph.db",
}


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, project_root, data):
        self.project_root = os.path.abspath(project_root)
        self.data = data

    def __getitem__(self, k):
        return self.data[k]

    def get(self, k, default=None):
        return self.data.get(k, default)

    def abspath(self, rel):
        return rel if os.path.isabs(rel) else os.path.join(self.project_root, rel)

    @property
    def db_path(self):
        return self.abspath(self.data["db_path"])

    @classmethod
    def load(cls, project_root, config_path=None):
        project_root = os.path.abspath(project_root)
        if config_path is None:
            for cand in ("seamlens.yaml", "seamlens.yml", "seamlens.json"):
                p = os.path.join(project_root, cand)
                if os.path.exists(p):
                    config_path = p
                    break
        raw = {}
        if config_path and os.path.exists(config_path):
            with open(config_path) as f:
                txt = f.read()
            if config_path.endswith(".json") or not _HAVE_YAML:
                raw = json.loads(txt) if txt.strip() else {}
            else:
                raw = yaml.safe_load(txt) or {}
        return cls(project_root, _deep_merge(DEFAULTS, raw))
