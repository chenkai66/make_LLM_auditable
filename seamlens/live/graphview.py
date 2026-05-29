"""Pure graph logic for the live view -- no LLM, no HTTP. Turns the GraphStore
into cytoscape-ready elements, locates the node for an edited file, and diffs two
in-memory snapshots so the server can animate exactly the edges a change added.

Why snapshot diffing (not store.diff_nodes): the live view rescans a single file
*into the current run_id*, so the change is intra-run. diff_nodes() compares two
separate runs and would miss it. The server instead snapshots the node/edge sets
before and after the rescan and diffs those -- which also captures edge-level
changes (a new writes/reads edge), not just node adds/removes.
"""
from seamlens.ai import atlas
from seamlens.extractors.base import module_id

# node kind -> display group the UI colours by
KIND_GROUP = {
    "module": "module",
    "artifact": "artifact",
    "config_const": "const",
    "fsm_state": "state",
}


def _node_element(n):
    a = n.get("attrs") or {}
    kind = n["kind"]
    role = kind
    if kind == "module":
        if a.get("importers", 0) == 0 and (a.get("has_main") or a.get("toplevel_exec")):
            role = "entrypoint"
        elif a.get("importers", 1) == 0:
            role = "dead"
        else:
            role = "library"
    return {"data": {
        "id": n["id"],
        "label": n.get("name") or n["id"],
        "name": n.get("name") or n["id"],
        "kind": kind,
        "group": KIND_GROUP.get(kind, kind),
        "role": role,
        "file": n.get("file"),
        "line": n.get("line"),
        "importers": a.get("importers"),
        "attrs": a,
    }}


def edge_key(e):
    """Stable identity of an edge independent of run_id / row id."""
    return (e["src"], e["dst"], e["kind"])


def _edge_element(e):
    src, dst, kind = edge_key(e)
    return {"data": {
        "id": "%s|%s|%s" % (kind, src, dst),
        "source": src,
        "target": dst,
        "kind": kind,
        "file": e.get("file"),
        "line": e.get("line"),
    }}


def cytoscape_elements(store):
    """Full current graph as {nodes:[...], edges:[...]} for the initial render.
    Each node carries any AI-learned meta (architect reading / auditor risk / clean
    mark) so a freshly-loaded tab shows accumulated knowledge, not a cold graph."""
    meta = store.all_meta()
    nodes = []
    for n in store.nodes():
        el = _node_element(n)
        m = meta.get(n["id"])
        if m:
            el["data"]["meta"] = m
        nodes.append(el)
    seen = set()
    edges = []
    for e in store.edges():
        k = edge_key(e)
        if k in seen:
            continue
        seen.add(k)
        edges.append(_edge_element(e))
    return {"nodes": nodes, "edges": edges}


def snapshot(store):
    """(node_id_set, {edge_key: edge_element}) of the current run -- the before/
    after the server diffs around a rescan."""
    node_ids = {n["id"] for n in store.nodes()}
    edge_map = {}
    for e in store.edges():
        edge_map[edge_key(e)] = _edge_element(e)
    return node_ids, edge_map


def diff(before, after, store):
    """Delta between two snapshots. Returns added/removed node elements + added/
    removed edge elements, ready to hand the UI for animation."""
    (b_nodes, b_edges) = before
    (a_nodes, a_edges) = after
    added_node_ids = a_nodes - b_nodes
    removed_node_ids = b_nodes - a_nodes
    by_id = {n["id"]: _node_element(n) for n in store.nodes()}
    added_nodes = [by_id[i] for i in added_node_ids if i in by_id]
    added_edges = [a_edges[k] for k in (set(a_edges) - set(b_edges))]
    removed_edges = [b_edges[k] for k in (set(b_edges) - set(a_edges))]
    return {
        "added_nodes": added_nodes,
        "removed_nodes": sorted(removed_node_ids),
        "added_edges": added_edges,
        "removed_edges": removed_edges,
    }


def locate(rel_path):
    """The module node id for an edited file path (best effort)."""
    if not rel_path:
        return None
    return module_id(rel_path.replace("\\", "/"))


def neighborhood(store, node_id, depth=1):
    """Node ids within `depth` hops of node_id (undirected), for focused
    highlighting. Small BFS over the current edge set."""
    if not node_id:
        return set()
    adj = {}
    for e in store.edges():
        adj.setdefault(e["src"], set()).add(e["dst"])
        adj.setdefault(e["dst"], set()).add(e["src"])
    seen = {node_id}
    frontier = {node_id}
    for _ in range(max(0, depth)):
        nxt = set()
        for n in frontier:
            nxt |= adj.get(n, set())
        nxt -= seen
        seen |= nxt
        frontier = nxt
    return seen


def graph_summary(store):
    """Compact dict the narrator/Q&A use as grounding context. Reuses atlas."""
    return atlas.stats(store, [])


def _name_of(store, node_id, _cache={}):
    """Best-effort display name for a node id (falls back to the id)."""
    key = id(store)
    idx = _cache.get(key)
    if idx is None or idx.get("_run") != store.current_run():
        idx = {"_run": store.current_run()}
        for n in store.nodes():
            idx[n["id"]] = n.get("name") or n["id"]
        _cache.clear()
        _cache[key] = idx
    return idx.get(node_id, node_id)


def change_brief(store, node_id):
    """The ripple map for an edited module: who depends on it and the data
    contracts it sits on. This is what lets the auditor reason about cross-
    component breakage instead of only the local diff.

    Returns a dict:
      imports_out   modules this module imports (its own deps)
      imported_by   modules that import THIS module  (break if its interface changed)
      writes        artifacts this module produces, each with `also_read_by`
      reads         artifacts this module consumes,  each with `also_written_by`
      constants     config constants this module relates to
    """
    if not node_id:
        return {}
    writers = {}   # artifact -> set(modules that write it)
    readers = {}   # artifact -> set(modules that read it)
    imports_out, imported_by, consts = set(), set(), set()
    mine_writes, mine_reads = set(), set()
    for e in store.edges():
        k, s, d = e["kind"], e["src"], e["dst"]
        if k == "writes":
            writers.setdefault(d, set()).add(s)
            if s == node_id:
                mine_writes.add(d)
        elif k == "reads":
            readers.setdefault(d, set()).add(s)
            if s == node_id:
                mine_reads.add(d)
        elif k == "imports":
            if s == node_id:
                imports_out.add(d)
            if d == node_id:
                imported_by.add(s)
        elif k in ("uses_const", "defines_const", "reads_const"):
            if s == node_id or d == node_id:
                consts.add(d if s == node_id else s)

    def nm(x):
        return _name_of(store, x)

    return {
        "node": nm(node_id),
        "imports_out": sorted(nm(x) for x in imports_out),
        "imported_by": sorted(nm(x) for x in imported_by),
        "writes": [{"artifact": nm(a),
                    "also_read_by": sorted(nm(m) for m in (readers.get(a, set())) if m != node_id)}
                   for a in sorted(mine_writes)],
        "reads": [{"artifact": nm(a),
                   "also_written_by": sorted(nm(m) for m in (writers.get(a, set())) if m != node_id)}
                  for a in sorted(mine_reads)],
        "constants": sorted(nm(x) for x in consts),
    }
