"""Architecture atlas: turn the system graph into something a human can read.

Two products, separated by how much you can trust them:

  * DETERMINISTIC (no AI): a Graphviz DOT of the data-flow + import graph, with
    the seams the linters flagged highlighted; plus a structured stats block
    (entrypoints, libraries, artifacts, producers/consumers, dead modules,
    divergent constants, findings by severity). This is ground truth.

  * AI NARRATIVE (optional): a short prose architecture overview generated from
    the stats + a graph excerpt + the findings. It is explicitly labelled as a
    model's reading of the deterministic facts, never a source of new facts.

The split mirrors seamlens' tiering discipline: the graph is the asset, the prose
is an advisory lens over it.
"""
from collections import defaultdict


def _esc(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def collect(store):
    """Pull the generic graph into plain dicts the renderers/narrator share."""
    modules = list(store.nodes("module"))
    consts = list(store.nodes("config_const"))
    writes = list(store.edges("writes"))
    reads = list(store.edges("reads"))
    imports = list(store.edges("imports"))

    artifacts = set()
    producers, consumers = defaultdict(set), defaultdict(set)
    for e in writes:
        fn = e["dst"].split("artifact:", 1)[-1]
        artifacts.add(fn)
        producers[fn].add(e["src"])
    for e in reads:
        fn = e["dst"].split("artifact:", 1)[-1]
        artifacts.add(fn)
        consumers[fn].add(e["src"])

    def is_entry(m):
        a = m["attrs"] or {}
        return a.get("importers", 0) == 0 and (a.get("has_main") or a.get("toplevel_exec"))

    def is_dead(m):
        a = m["attrs"] or {}
        return (a.get("importers", 1) == 0 and not a.get("has_main")
                and not a.get("toplevel_exec"))

    entrypoints = sorted(m["name"] for m in modules if is_entry(m))
    libraries = sorted(m["name"] for m in modules
                       if (m["attrs"] or {}).get("importers", 0) > 0)
    dead = sorted(m["name"] for m in modules if is_dead(m))

    return {
        "modules": modules, "consts": consts,
        "imports": imports, "writes": writes, "reads": reads,
        "artifacts": sorted(artifacts),
        "producers": producers, "consumers": consumers,
        "entrypoints": entrypoints, "libraries": libraries, "dead": dead,
    }


def stats(store, findings):
    g = collect(store)
    sev = defaultdict(int)
    for f in findings:
        sev[f.severity] += 1
    orphan_w = [a for a in g["artifacts"] if g["producers"].get(a) and not g["consumers"].get(a)]
    orphan_r = [a for a in g["artifacts"] if g["consumers"].get(a) and not g["producers"].get(a)]
    return {
        "modules": len(g["modules"]),
        "entrypoints": g["entrypoints"],
        "libraries": len(g["libraries"]),
        "dead_modules": g["dead"],
        "artifacts": len(g["artifacts"]),
        "shared_artifacts": [a for a in g["artifacts"]
                             if g["producers"].get(a) and g["consumers"].get(a)],
        "write_only_artifacts": orphan_w,
        "read_only_artifacts": orphan_r,
        "constants": len(g["consts"]),
        "imports_edges": len(g["imports"]),
        "findings": {"error": sev["error"], "warning": sev["warning"], "info": sev["info"]},
    }


def build_dot(store, findings, max_nodes=400):
    """Deterministic Graphviz DOT of the architecture: modules + artifacts with
    write/read edges (the data-flow seams) and import edges. Nodes named in an
    error/warning finding are highlighted."""
    g = collect(store)
    hot = set()
    for f in findings:
        if f.severity in ("error", "warning"):
            for i in f.ids:
                hot.add(i)
                if i.startswith("artifact:"):
                    hot.add(i.split("artifact:", 1)[-1])

    lines = ["digraph seamlens_atlas {",
             '  rankdir=LR; fontname="Helvetica"; node[fontname="Helvetica",fontsize=10];',
             '  edge[fontname="Helvetica",fontsize=8,color="#888888"];',
             '  label="seamlens architecture atlas"; labelloc=t;']

    count = 0
    # modules
    for m in g["modules"]:
        if count > max_nodes:
            break
        count += 1
        a = m["attrs"] or {}
        nid = m["id"]
        hotted = nid in hot
        if a.get("importers", 0) == 0 and (a.get("has_main") or a.get("toplevel_exec")):
            shape, fill = "box", "#cde7ff"        # entrypoint
        elif a.get("importers", 1) == 0:
            shape, fill = "box", "#f0f0f0"        # dead/isolated
        else:
            shape, fill = "box", "#ffffff"        # library
        if hotted:
            fill = "#ffd0d0"
        lines.append('  "%s" [label="%s\\n(imp=%s)",shape=%s,style=filled,fillcolor="%s"];'
                     % (_esc(nid), _esc(m["name"]), a.get("importers", "?"), shape, fill))
    # artifacts
    for fn in g["artifacts"]:
        if count > max_nodes:
            break
        count += 1
        aid = "artifact:" + fn
        fill = "#ffd0d0" if (aid in hot or fn in hot) else "#fff8d0"
        lines.append('  "%s" [label="%s",shape=note,style=filled,fillcolor="%s"];'
                     % (_esc(aid), _esc(fn), fill))
    # edges
    for e in g["writes"]:
        lines.append('  "%s" -> "%s" [color="#2e8b57"];' % (_esc(e["src"]), _esc(e["dst"])))
    for e in g["reads"]:
        lines.append('  "%s" -> "%s" [color="#4169e1"];' % (_esc(e["dst"]), _esc(e["src"])))
    for e in g["imports"]:
        lines.append('  "%s" -> "%s" [style=dashed,color="#cccccc"];'
                     % (_esc(e["src"]), _esc(e["dst"])))
    lines.append("}")
    return "\n".join(lines)


def _graph_excerpt(store, limit=60):
    """A compact text rendering of the data-flow for the LLM context."""
    g = collect(store)
    rows = []
    for fn in g["shared_artifacts"] if False else g["artifacts"]:
        p = sorted(g["producers"].get(fn, []))
        c = sorted(g["consumers"].get(fn, []))
        rows.append("  %s : produced_by=%s read_by=%s" % (
            fn, [x.replace("mod:", "") for x in p] or "NONE",
            [x.replace("mod:", "") for x in c] or "NONE"))
        if len(rows) >= limit:
            break
    return "\n".join(rows)


_SYS = ("You are a software architect. You are given the DETERMINISTIC system "
        "graph of a codebase (entrypoints, libraries, data artifacts and which "
        "module writes/reads each) plus seam findings from a static auditor. "
        "Write a concise architecture overview a new engineer could use to orient. "
        "Only use the facts given; do not invent components. Note the data-flow "
        "backbone, the risky seams, and what the findings imply. 250-400 words, "
        "plain prose with a short bullet list of the top risks at the end.")


def narrate(provider, st, findings, excerpt):
    if not (provider and provider.enabled and provider.available):
        return None
    find_lines = "\n".join(
        "  [%s] %s -- %s" % (f.severity, f.title, ", ".join(f.where[:2]))
        for f in findings[:25]) or "  (none)"
    user = (
        "STATS:\n%s\n\nDATA-FLOW (artifact: producers -> consumers):\n%s\n\n"
        "SEAM FINDINGS:\n%s\n" % (
            _fmt_stats(st), excerpt, find_lines))
    return provider.complete(_SYS, user)


def _fmt_stats(st):
    return ("  modules=%d  libraries=%d  artifacts=%d  imports_edges=%d  constants=%d\n"
            "  entrypoints=%s\n  dead_modules=%s\n"
            "  shared_artifacts=%s\n  write_only=%s  read_only=%s\n"
            "  findings: error=%d warning=%d info=%d" % (
                st["modules"], st["libraries"], st["artifacts"], st["imports_edges"],
                st["constants"], st["entrypoints"][:12], st["dead_modules"][:12],
                st["shared_artifacts"][:12], st["write_only_artifacts"][:8],
                st["read_only_artifacts"][:8],
                st["findings"]["error"], st["findings"]["warning"], st["findings"]["info"]))


def render_markdown(st, narrative):
    out = ["# seamlens architecture atlas", "",
           "## Structural facts (deterministic)", "", "```", _fmt_stats(st), "```", ""]
    if narrative:
        out += ["## Architecture overview (AI narrative over the facts above)", "",
                narrative, ""]
    else:
        out += ["_AI narrative skipped (ai disabled / unavailable)._", ""]
    return "\n".join(out)
