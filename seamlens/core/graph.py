"""Generic system-graph store (SQLite). Zero project-specific code.

A "system graph" captures the *seams* between components: which module writes
which artifact, which constant is defined/referenced where, which daemon installs
a logger, which FSM state transitions to which, etc. Bugs live in these seams
(producer/consumer filename mismatches, divergent duplicated constants, orphaned
artifacts) far more often than inside a single function -- so this is what we make
queryable and lint-able.
"""
import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id      TEXT PRIMARY KEY,
    kind    TEXT NOT NULL,
    name    TEXT,
    file    TEXT,
    line    INTEGER,
    attrs   TEXT,            -- JSON
    run_id  INTEGER
);
CREATE TABLE IF NOT EXISTS edges (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    src     TEXT NOT NULL,
    dst     TEXT NOT NULL,
    kind    TEXT NOT NULL,
    file    TEXT,
    line    INTEGER,
    attrs   TEXT,            -- JSON
    run_id  INTEGER
);
CREATE TABLE IF NOT EXISTS scan_runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT,
    git_rev   TEXT,
    nodes     INTEGER,
    edges     INTEGER,
    note      TEXT
);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind);
CREATE INDEX IF NOT EXISTS idx_edges_src  ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst  ON edges(dst);
"""


class GraphStore:
    """Append-then-swap graph store. Each scan writes into a fresh run_id and,
    on commit_run(), becomes the 'current' graph; the prior run is retained for
    diffing blast radius across sessions."""

    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.run_id = None

    # -- scan lifecycle ----------------------------------------------------
    def start_run(self, git_rev="", note=""):
        cur = self.conn.execute(
            "INSERT INTO scan_runs(ts, git_rev, nodes, edges, note) VALUES(?,?,0,0,?)",
            (time.strftime("%Y-%m-%dT%H:%M:%S"), git_rev, note),
        )
        self.run_id = cur.lastrowid
        self.conn.commit()
        return self.run_id

    def commit_run(self):
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM nodes WHERE run_id=?", (self.run_id,)
        ).fetchone()["c"]
        e = self.conn.execute(
            "SELECT COUNT(*) c FROM edges WHERE run_id=?", (self.run_id,)
        ).fetchone()["c"]
        self.conn.execute(
            "UPDATE scan_runs SET nodes=?, edges=? WHERE id=?", (n, e, self.run_id)
        )
        # retain only the two most recent runs (current + previous for diff)
        old = self.conn.execute(
            "SELECT id FROM scan_runs ORDER BY id DESC LIMIT -1 OFFSET 2"
        ).fetchall()
        for r in old:
            self.conn.execute("DELETE FROM nodes WHERE run_id=?", (r["id"],))
            self.conn.execute("DELETE FROM edges WHERE run_id=?", (r["id"],))
            self.conn.execute("DELETE FROM scan_runs WHERE id=?", (r["id"],))
        self.conn.commit()

    # -- writers -----------------------------------------------------------
    def add_node(self, id, kind, name=None, file=None, line=None, **attrs):
        # Extractors are independent and may each touch the same node (e.g. every
        # extractor sees a `module`). Merge instead of clobber: a later extractor's
        # attrs/name/file/line augment the earlier ones rather than dropping them
        # (this is why the imports extractor's `importers`/`has_main` survive a
        # subsequent bare add_node from file_io). Same-key writes: last wins.
        row = self.conn.execute(
            "SELECT name, file, line, attrs FROM nodes WHERE id=? AND run_id=?",
            (id, self.run_id),
        ).fetchone()
        if row is not None:
            merged = json.loads(row["attrs"] or "{}")
            merged.update(attrs)
            attrs = merged
            name = name if name is not None else row["name"]
            file = file if file is not None else row["file"]
            line = line if line is not None else row["line"]
        self.conn.execute(
            "INSERT OR REPLACE INTO nodes(id, kind, name, file, line, attrs, run_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (id, kind, name, file, line, json.dumps(attrs, ensure_ascii=False), self.run_id),
        )

    def add_edge(self, src, dst, kind, file=None, line=None, **attrs):
        self.conn.execute(
            "INSERT INTO edges(src, dst, kind, file, line, attrs, run_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (src, dst, kind, file, line, json.dumps(attrs, ensure_ascii=False), self.run_id),
        )

    def flush(self):
        self.conn.commit()

    # -- readers -----------------------------------------------------------
    def _runs(self, limit=2):
        return [r["id"] for r in self.conn.execute(
            "SELECT id FROM scan_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]

    def current_run(self):
        rs = self._runs(1)
        return rs[0] if rs else None

    def nodes(self, kind=None, run_id=None):
        run_id = run_id or self.current_run()
        q = "SELECT * FROM nodes WHERE run_id=?"
        args = [run_id]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        for r in self.conn.execute(q, args):
            d = dict(r)
            d["attrs"] = json.loads(d["attrs"] or "{}")
            yield d

    def edges(self, kind=None, run_id=None):
        run_id = run_id or self.current_run()
        q = "SELECT * FROM edges WHERE run_id=?"
        args = [run_id]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        for r in self.conn.execute(q, args):
            d = dict(r)
            d["attrs"] = json.loads(d["attrs"] or "{}")
            yield d

    def diff_nodes(self):
        """Return (added, removed) node-id sets between the two most recent runs."""
        runs = self._runs(2)
        if len(runs) < 2:
            return set(), set()
        cur, prev = runs[0], runs[1]
        cur_ids = {r["id"] for r in self.conn.execute(
            "SELECT id FROM nodes WHERE run_id=?", (cur,))}
        prev_ids = {r["id"] for r in self.conn.execute(
            "SELECT id FROM nodes WHERE run_id=?", (prev,))}
        return cur_ids - prev_ids, prev_ids - cur_ids

    def close(self):
        self.conn.close()
