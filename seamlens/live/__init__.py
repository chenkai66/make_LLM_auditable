"""seamlens live -- the always-on companion that sits beside Claude Code.

A browser sidebar that, on every meaningful tool action Claude Code takes,
narrates what changed and why it matters, lights up the edited node inside the
full system graph, and animates the new edges the change introduced -- a "god
view" over the codebase.

This subpackage is the ONLY place project-facing live machinery lives; the core
(extractors/linters/graph) stays project-agnostic and AI-free. Like the optional
ai/ layer, live/ is imported lazily by the CLI so the five core commands never
touch it.
"""
