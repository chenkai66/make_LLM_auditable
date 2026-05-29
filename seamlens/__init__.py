"""seamlens -- make a complex system auditable.

A portable engine that derives a *system graph* (the seams between components)
from source, overlays a thin curated semantic layer, and runs linters that
encode real bug-classes. Generic core; everything project-specific lives in one
seamlens.yaml. Adopt it on any Python project by writing that file.
"""
__version__ = "0.1.0"
