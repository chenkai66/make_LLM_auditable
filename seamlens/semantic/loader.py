"""Layer 2: thin curated semantic overlay loader.

The overlay is a small hand-maintained file (artifact_groups, constant_groups,
allow_orphan_*, daemons, invariants, rationale). It is intentionally tiny: it
records only the couplings/intents that cannot be derived from code, so it
doesn't rot the way an exhaustive hand-drawn graph would. Linters consume it to
turn structural facts into judgements."""
import os

try:
    import yaml
    _HAVE_YAML = True
except Exception:
    _HAVE_YAML = False
import json


def load_semantic(cfg):
    rel = cfg.get("semantic_overlay")
    if not rel:
        return {}
    path = cfg.abspath(rel)
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        txt = f.read()
    if not txt.strip():
        return {}
    if path.endswith(".json") or not _HAVE_YAML:
        return json.loads(txt)
    return yaml.safe_load(txt) or {}
