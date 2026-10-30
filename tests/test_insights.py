"""`ave cost` (transcript usage) and `ave trace` (span tree)."""

import json
from pathlib import Path

import pytest

from ave import tracetree, usage
from tests.conftest import parse_json


def _assistant(mid: str, model: str, inp=10, out=100, read=1000, w5m=0, w1h=0, agent=None) -> str:
    rec = {
        "type": "assistant",
        "message": {
            "id": mid,
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": w5m + w1h,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": w5m,
                    "ephemeral_1h_input_tokens": w1h,
                },
            },
        },
    }
    if agent:
        rec["agentId"] = agent
    return json.dumps(rec) + "\n"


def make_session(project_root: Path, sid: str, run_mark: str | None) -> Path:
    base = usage.claude_projects_dir() / usage.project_slug(project_root)
    base.mkdir(parents=True, exist_ok=True)
    t = base / f"{sid}.jsonl"
    lines = [json.dumps({"type": "user", "message": {"content": run_mark or "hello"}}) + "\n"]
    # one response split over two records (same id): counted once
    lines += [_assistant("m1", "claude-opus-5-5", w1h=2000)] * 2
    lines += [_assistant("m2", "claude-opus-5-5")]
    t.write_text("".join(lines))
    sub = base / sid / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a1.jsonl").write_text(_assistant("s1", "claude-sonnet-5-5", w5m=400) * 3)
    (sub / "agent-a1.meta.json").write_text(json.dumps({"agentType": "ave-vision-analyst"}))
    (sub / "agent-a2.jsonl").write_text(_assistant("s2", "mystery-model"))
    return t


def test_usage_dedupes_and_prices(project_dir: Path):
    make_session(project_dir, "s-run1", "/ave-edit r-0001")
    make_session(project_dir, "s-other", None)
    run_dir = project_dir / "runs" / "r-0001"
    sessions = usage.find_sessions(project_dir, "r-0001", run_dir)
    assert [s.stem for s in sessions] == ["s-run1"]
    rows = {(r.agent, r.model): r.usage for r in usage.run_usage(sessions)}
    orch = rows[("orchestrator", "claude-opus-5-5")]
    assert orch.messages == 2 and orch.output == 200 and orch.cache_write_1h == 2000
    # 20 in + 200 out + 2000 read (0.1x) + 2000 1h-write (2x), at $4/$20 per MTok
    assert orch.cost == pytest.approx((20 * 4 + 200 * 20 + 2000 * 0.4 + 2000 * 8) / 1e6)
    vision = rows[("ave-vision-analyst", "claude-sonnet-5-5")]
    assert vision.messages == 1 and vision.cache_write_5m == 400
    assert rows[("sub-agent", "mystery-model")].cost is None


def test_usage_finds_sessions_from_hooks(project_dir: Path):
    make_session(project_dir, "s-hooked", None)
    hooks = project_dir / "runs" / "r-0001" / ".hooks"
    hooks.mkdir(parents=True)
    (hooks / "session-s-hooked.json").write_text("{}")
    found = usage.find_sessions(project_dir, "r-0001", project_dir / "runs" / "r-0001")
    assert [s.stem for s in found] == ["s-hooked"]


def test_cost_cli(project_dir: Path, invoke):
    invoke("-p", str(project_dir), "runs", "new")
    assert "no Claude Code sessions" in invoke("-p", str(project_dir), "cost").stdout
    make_session(project_dir, "s-run1", "Run id: r-0001")
    out = parse_json(invoke("-p", str(project_dir), "cost", "--json").stdout)
    assert out["total"]["messages"] == 4 and out["total"]["cost"] is None  # unknown model inside
    table = invoke("-p", str(project_dir), "cost").stdout
    assert "ave-vision-analyst" in table and "total" in table


def _rec(i, name, parent=None, start="2026-10-30T00:00:00", kind="tool", status="ok", **kw):
    return {
        "id": i,
        "trace_id": "t",
        "parent_id": parent,
        "name": name,
        "kind": kind,
        "start": start,
        "end": start,
        "duration_ms": 1500.0,
        "status": status,
        "error": "boom" if status == "error" else None,
        "inputs": {},
        "outputs": {},
        "metadata": kw.get("metadata", {}),
    }


def write_trace(path: Path, recs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return path


def test_tracetree_build(tmp_path: Path):
    f = write_trace(
        tmp_path / "t.jsonl",
        [
            _rec("s", "claude:session", kind="run", start="2026-10-30T00:00:01"),
            _rec("a", "agent:x", "s", kind="agent", start="2026-10-30T00:00:02"),
            _rec("t2", "tool:Read", "a", start="2026-10-30T00:00:04"),
            _rec("t1", "tool:Bash", "a", start="2026-10-30T00:00:03", status="error"),
            _rec("o", "phase:analyze", "missing-parent", kind="phase", start="2026-10-30T00:00:00"),
            _rec(
                "s",
                "claude:session",
                kind="run",
                start="2026-10-30T00:00:01",
                metadata={"ls_run_id": "ls-1"},
            ),  # rewritten at the next Stop: last wins
        ],
    )
    roots = tracetree.build(f)
    assert [r.name for r in roots] == ["phase:analyze", "claude:session"]
    assert [c.name for c in roots[1].children[0].children] == ["tool:Bash", "tool:Read"]
    assert roots[1].to_dict()["ls_run_id"] == "ls-1"
    assert [n.name for n in tracetree.errors(roots)] == ["tool:Bash"]
    assert "children" not in roots[1].to_dict(depth=1)
    assert tracetree.fmt_ms(250) == "250ms" and tracetree.fmt_ms(1500) == "1.5s"
    assert tracetree.fmt_ms(185_000) == "3m05s"


def test_trace_cli(project_dir: Path, invoke):
    p = str(project_dir)
    invoke("-p", p, "runs", "new")  # writes run:r-0001
    run = project_dir / "runs" / "r-0001"
    recs = [_rec("ag", "agent:ave-vision-analyst", kind="agent")]
    recs += [_rec(f"r{k}", "tool:Read", "ag", start=f"2026-10-30T00:00:{k:02d}") for k in range(8)]
    recs += [_rec("bad", "tool:Bash", "ag", status="error")]
    with (run / "trace.jsonl").open("a") as f:
        f.writelines(json.dumps(r) + "\n" for r in recs)
    out = invoke("-p", p, "trace", "--depth", "3").stdout
    assert "run:r-0001" in out and "agent:ave-vision-analyst" in out
    assert "… 5 more tool:Read" in out and "boom" in out
    assert "child spans" in invoke("-p", p, "trace", "--depth", "1").stdout
    errs = invoke("-p", p, "trace", "--errors").stdout
    assert "tool:Bash" in errs and "tool:Read" not in errs
    data = parse_json(invoke("-p", p, "trace", "--json").stdout)
    assert {r["name"] for r in data["roots"]} >= {"run:r-0001", "agent:ave-vision-analyst"}


def test_trace_cli_without_trace(project_dir: Path, invoke):
    invoke("-p", str(project_dir), "config", "set", "observability.provider", "none")
    invoke("-p", str(project_dir), "runs", "new")
    assert "no trace for run r-0001" in invoke("-p", str(project_dir), "trace").stdout
