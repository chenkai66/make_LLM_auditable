"""Linter base + Finding. Each linter consumes the current graph and yields
Findings. A Finding maps to a real bug-class we've actually hit, so the linter
docstring names the incident it would have caught."""


class Finding:
    def __init__(self, linter, severity, title, detail, where=None, ids=None):
        self.linter = linter
        self.severity = severity        # 'error' | 'warning' | 'info'
        self.title = title
        self.detail = detail
        self.where = where or []        # list of "file:line" strings
        self.ids = ids or []            # graph node ids involved

    def as_dict(self):
        return {
            "linter": self.linter, "severity": self.severity,
            "title": self.title, "detail": self.detail,
            "where": self.where, "ids": self.ids,
        }

    def __str__(self):
        loc = (" @ " + ", ".join(self.where)) if self.where else ""
        return "[%s] %s: %s%s" % (self.severity.upper(), self.title, self.detail, loc)


class Linter:
    name = "base"

    def __init__(self, cfg, store, semantic):
        self.cfg = cfg
        self.store = store
        self.semantic = semantic or {}

    def run(self):
        """Yield Finding objects."""
        raise NotImplementedError
