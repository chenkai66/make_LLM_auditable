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
        "kind": kind,
        "group": KIND_GROUP.get(kind, kind),
        "role": role,
        "file": n.get("file"),
        "importers": a.get("importers"),
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
    }}


def cytoscape_elements(store):
    """Full current graph as {nodes:[...], edges:[...]} for the initial render."""
    nodes = [_node_element(n) for n in store.nodes()]
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
