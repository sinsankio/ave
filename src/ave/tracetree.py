"""Build the span tree of a run from its local trace.jsonl (for `ave trace`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ave.observability import read_trace


@dataclass
class Node:
    rec: dict
    children: list[Node] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.rec["name"]

    def to_dict(self, depth: int | None = None) -> dict:
        r = self.rec
        out = {
            k: r.get(k) for k in ("id", "name", "kind", "start", "duration_ms", "status", "error")
        }
        if r.get("metadata", {}).get("ls_run_id"):
            out["ls_run_id"] = r["metadata"]["ls_run_id"]
        if depth is None or depth > 1:
            out["children"] = [
                c.to_dict(None if depth is None else depth - 1) for c in self.children
            ]
        return out


def build(trace_file: Path) -> list[Node]:
    """Roots of the span forest, oldest first. A span written twice (e.g. a Claude Code session
    updated at every Stop) keeps its last record; spans whose parent is missing become roots."""
    latest: dict[str, dict] = {}
    for rec in read_trace(trace_file):
        latest[rec["id"]] = rec
    nodes = {i: Node(r) for i, r in latest.items()}
    roots = []
    for node in nodes.values():
        parent = nodes.get(node.rec.get("parent_id") or "")
        (parent.children if parent else roots).append(node)
    for node in nodes.values():
        node.children.sort(key=lambda n: n.rec["start"])
    return sorted(roots, key=lambda n: n.rec["start"])


def fmt_ms(ms: float | None) -> str:
    if ms is None:
        return ""
    if ms < 1000:
        return f"{ms:.0f}ms"
    s = ms / 1000
    return f"{s:.1f}s" if s < 120 else f"{int(s // 60)}m{int(s % 60):02d}s"


def errors(roots: list[Node]) -> list[Node]:
    out, stack = [], list(roots)
    while stack:
        n = stack.pop()
        if n.rec.get("status") == "error":
            out.append(n)
        stack.extend(n.children)
    return out
