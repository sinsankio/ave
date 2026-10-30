"""Token usage of a run, read from the Claude Code transcripts of the sessions that worked on it.

Claude Code keeps one JSONL transcript per session under ~/.claude/projects/<project slug>/,
plus <session>/subagents/agent-<id>.jsonl (+ .meta.json with the agent type) per sub-agent.
Each API response can appear in several records (one per content block) with the same usage,
so usage is de-duplicated by message id.

On a subscription you pay for the plan, not per token; the dollar column is an API-equivalent
estimate to compare runs and agents, not a bill.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

# $ per million tokens (input, output); cache reads cost 0.1x input, cache writes 1.25x (5 min)
# or 2x (1 h). Prices as of 2026-09.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def project_slug(root: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(root.resolve()))


@dataclass
class Usage:
    messages: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cost: float | None = 0.0

    def add(self, model: str, u: dict) -> None:
        self.messages += 1
        inp, out = u.get("input_tokens", 0) or 0, u.get("output_tokens", 0) or 0
        read = u.get("cache_read_input_tokens", 0) or 0
        cc = u.get("cache_creation") or {}
        w1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
        w5m = cc.get("ephemeral_5m_input_tokens")
        if w5m is None:
            w5m = (u.get("cache_creation_input_tokens", 0) or 0) - w1h
        self.input += inp
        self.output += out
        self.cache_read += read
        self.cache_write_5m += w5m
        self.cache_write_1h += w1h
        price = PRICES.get(model)
        if price is None or self.cost is None:
            self.cost = None
            return
        pin, pout = price
        self.cost += (
            inp * pin + out * pout + read * pin * 0.1 + w5m * pin * 1.25 + w1h * pin * 2
        ) / 1e6

    def merge(self, other: Usage) -> None:
        for f in ("messages", "input", "output", "cache_read", "cache_write_5m", "cache_write_1h"):
            setattr(self, f, getattr(self, f) + getattr(other, f))
        self.cost = None if self.cost is None or other.cost is None else self.cost + other.cost


@dataclass
class Row:
    agent: str
    model: str
    usage: Usage = field(default_factory=Usage)

    def to_dict(self) -> dict:
        return {"agent": self.agent, "model": self.model} | asdict(self.usage)


def _transcript_usage(path: Path) -> dict[str, Usage]:
    """model -> usage for one transcript file, de-duplicated by message id."""
    last: dict[str, tuple[str, dict]] = {}
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"assistant"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = rec.get("message") or {}
            if rec.get("type") != "assistant" or not msg.get("usage") or not msg.get("id"):
                continue
            last[msg["id"]] = (msg.get("model", "?"), msg["usage"])
    out: dict[str, Usage] = defaultdict(Usage)
    for model, u in last.values():
        out[model].add(model, u)
    return dict(out)


def find_sessions(project_root: Path, run_id: str, run_dir: Path) -> list[Path]:
    """Transcripts of the Claude Code sessions that worked on this run."""
    base = claude_projects_dir() / project_slug(project_root)
    if not base.is_dir():
        return []
    known = {p.stem.removeprefix("session-") for p in (run_dir / ".hooks").glob("session-*.json")}
    marks = (f"ave-edit {run_id}", f"--run {run_id}", f"Run id: {run_id}")
    found = []
    for t in sorted(base.glob("*.jsonl")):
        if t.stem in known:
            found.append(t)
            continue
        with t.open(encoding="utf-8", errors="replace") as f:
            if any(m in line for line in f for m in marks):
                found.append(t)
    return found


def run_usage(sessions: list[Path]) -> list[Row]:
    rows: dict[tuple[str, str], Row] = {}

    def add(agent: str, per_model: dict[str, Usage]) -> None:
        for model, u in per_model.items():
            row = rows.setdefault((agent, model), Row(agent, model))
            row.usage.merge(u)

    for t in sessions:
        add("orchestrator", _transcript_usage(t))
        sub = t.with_suffix("") / "subagents"
        for f in sorted(sub.glob("agent-*.jsonl")) if sub.is_dir() else []:
            meta = f.with_suffix(".meta.json")
            agent = "sub-agent"
            if meta.is_file():
                with contextlib.suppress(json.JSONDecodeError):
                    agent = json.loads(meta.read_text()).get("agentType") or agent
            add(agent, _transcript_usage(f))
    return sorted(rows.values(), key=lambda r: (r.agent != "orchestrator", r.agent, r.model))
