"""Claude Code assets, the hooks bridge, and `ave run`."""

import json
import sys
from pathlib import Path

import pytest
import yaml

from ave import agents, hooks
from ave.config import load_project
from ave.observability import read_trace
from tests.conftest import parse_json
from tests.test_observability import RecordingClient

AGENT_FILES = [
    "ave-alignment.md",
    "ave-copywriter.md",
    "ave-doc-analyst.md",
    "ave-pacing-editor.md",
    "ave-qa-critic.md",
    "ave-vision-analyst.md",
]


def frontmatter(path: Path) -> dict:
    text = path.read_text()
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n")[1])


def test_init_installs_assets(project_dir: Path):
    base = project_dir / ".claude"
    assert sorted(p.name for p in (base / "agents").iterdir()) == AGENT_FILES
    assert (base / "commands" / "ave-edit.md").is_file()
    for name in AGENT_FILES:
        fm = frontmatter(base / "agents" / name)
        assert fm["name"] == name[:-3] and fm["description"]
        assert fm["model"] in ("opus", "sonnet")
        assert "{{" not in (base / "agents" / name).read_text()
    assert frontmatter(base / "agents" / "ave-pacing-editor.md")["model"] == "opus"
    assert frontmatter(base / "agents" / "ave-vision-analyst.md")["model"] == "sonnet"
    assert frontmatter(base / "agents" / "ave-copywriter.md")["skills"] == ["sineth-writing-style"]
    assert "skills" not in frontmatter(base / "agents" / "ave-pacing-editor.md")
    settings = json.loads((base / "settings.json").read_text())
    assert agents.PERMISSION in settings["permissions"]["allow"]
    cmd = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert cmd == f'"{sys.executable}" -m ave hook'
    assert all(s.status == "current" for s in agents.status(load_project(project_dir)))


def test_install_respects_user_edits_and_settings(project_dir: Path, invoke):
    base = project_dir / ".claude"
    edited = base / "agents" / "ave-pacing-editor.md"
    edited.write_text("my own pacing rules")
    settings = base / "settings.json"
    data = json.loads(settings.read_text())
    data["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "notify-send done"}]})
    data["permissions"]["allow"].append("Bash(git status)")
    settings.write_text(json.dumps(data))

    res = invoke("-p", str(project_dir), "agents", "install")
    assert res.exit_code == 0 and "kept your edited files" in res.output
    assert edited.read_text() == "my own pacing rules"
    data = json.loads(settings.read_text())
    stop_cmds = [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]
    assert "notify-send done" in stop_cmds and sum("-m ave hook" in c for c in stop_cmds) == 1
    assert data["permissions"]["allow"].count(agents.PERMISSION) == 1
    assert "Bash(git status)" in data["permissions"]["allow"]

    invoke("-p", str(project_dir), "agents", "install", "--force")
    assert "ave-pacing-editor" in edited.read_text()


def test_model_change_marks_outdated(project_dir: Path, invoke):
    invoke("-p", str(project_dir), "config", "set", "models.worker", "haiku")
    statuses = {s.path.name: s.status for s in agents.status(load_project(project_dir))}
    assert statuses["ave-vision-analyst.md"] == "outdated"
    invoke("-p", str(project_dir), "agents", "install")
    fm = frontmatter(project_dir / ".claude" / "agents" / "ave-vision-analyst.md")
    assert fm["model"] == "haiku"


def test_install_refuses_broken_settings(project_dir: Path, invoke):
    (project_dir / ".claude" / "settings.json").write_text("{broken")
    res = invoke("-p", str(project_dir), "agents", "install")
    assert res.exit_code == 1 and "not valid JSON" in res.output


def test_init_without_agents(tmp_path: Path, invoke):
    invoke("-p", str(tmp_path), "init", "--yes", "--no-agents")
    assert not (tmp_path / ".claude").exists()


# --- ave run ---------------------------------------------------------------------------


def test_run_dry_run(project_dir: Path, invoke):
    out = parse_json(invoke("-p", str(project_dir), "run", "--dry-run").stdout)
    assert out["cmd"] == ["claude", "/ave-edit r-0001"] and out["run"] == "r-0001"
    again = parse_json(invoke("-p", str(project_dir), "run", "--dry-run").stdout)
    assert again["run"] == "r-0001"  # resumes the latest run
    new = parse_json(invoke("-p", str(project_dir), "run", "--dry-run", "--new").stdout)
    assert new["run"] == "r-0002"


def test_run_needs_claude_code_backend(project_dir: Path, invoke):
    invoke("-p", str(project_dir), "config", "set", "inference.backend", "api")
    assert invoke("-p", str(project_dir), "run", "--dry-run").exit_code == 2


# --- hooks -----------------------------------------------------------------------------


def _events(cwd: Path) -> list[dict]:
    base = {"session_id": "s1", "cwd": str(cwd)}
    sub = {"agent_id": "a1", "agent_type": "ave-vision-analyst"}
    bash = {"tool_name": "Bash", "tool_input": {"command": "ave analyze --run r-0001"}}
    read = {"tool_name": "Read", "tool_input": {"file_path": "x.png"}}
    return [
        base | e
        for e in [
            {"hook_event_name": "SessionStart"},
            {"hook_event_name": "UserPromptSubmit", "prompt_text": "/ave-edit r-0001"},
            {"hook_event_name": "PreToolUse", "tool_use_id": "t1"} | bash,
            {"hook_event_name": "PostToolUse", "tool_use_id": "t1", "tool_response": "x" * 5000}
            | bash,
            {"hook_event_name": "SubagentStart"} | sub,
            {"hook_event_name": "PreToolUse", "tool_use_id": "t2"} | read | sub,
            {"hook_event_name": "PostToolUseFailure", "tool_use_id": "t2", "error": "no such file"}
            | read
            | sub,
            {"hook_event_name": "SubagentStop", "last_assistant_message": "done"} | sub,
            {"hook_event_name": "Stop", "last_assistant_message": "Gate 1 ready"},
        ]
    ]


def test_hooks_build_span_tree(project_dir: Path, invoke):
    invoke("-p", str(project_dir), "runs", "new")
    for ev in _events(project_dir):
        hooks.run(json.dumps(ev))
    recs = {r["name"]: r for r in read_trace(project_dir / "runs" / "r-0001" / "trace.jsonl")}
    session, agent = recs["claude:session"], recs["agent:ave-vision-analyst"]
    assert session["parent_id"] is None
    assert recs["user:prompt"]["inputs"]["prompt"] == "/ave-edit r-0001"
    assert recs["tool:Bash[ave analyze]"]["parent_id"] == session["id"]
    assert len(recs["tool:Bash[ave analyze]"]["outputs"]["response"]) < 2100  # clipped
    assert agent["parent_id"] == session["id"]
    assert recs["tool:Read"]["parent_id"] == agent["id"] and recs["tool:Read"]["status"] == "error"
    assert all(r["metadata"]["thread_id"] == "r-0001" for r in recs.values())
    assert not list((project_dir / "runs" / "r-0001" / ".hooks").glob("tool-*.json"))  # cleaned up


def test_hooks_langsmith_parents(project_dir: Path, invoke):
    invoke("-p", str(project_dir), "config", "set", "observability.provider", "langsmith")
    invoke("-p", str(project_dir), "runs", "new")
    client = RecordingClient()
    for ev in _events(project_dir):
        hooks.run(json.dumps(ev), ls_client=client)
    creates = {c[1]: c for c in client.calls if c[0] == "create"}
    session, agent = creates["claude:session"], creates["agent:ave-vision-analyst"]
    assert session[2] is None
    assert str(agent[2]) == str(session[4])
    assert str(creates["tool:Bash[ave analyze]"][2]) == str(session[4])
    assert str(creates["tool:Read"][2]) == str(agent[4])
    updated = {c[1] for c in client.calls if c[0] == "update"}
    assert {str(session[4]), str(agent[4])} <= updated  # sub-agent end + session end


@pytest.mark.parametrize(
    "stdin", ["", "{not json", json.dumps({"hook_event_name": "Stop", "cwd": "/"})]
)
def test_hooks_never_raise(stdin, capsys):
    hooks.run(stdin)


def test_hooks_noop_without_runs_or_tracing(project_dir: Path, invoke):
    ev = json.dumps({"hook_event_name": "SessionStart", "session_id": "s", "cwd": str(project_dir)})
    hooks.run(ev)  # no runs yet
    assert not (project_dir / "runs").exists()
    invoke("-p", str(project_dir), "config", "set", "observability.provider", "none")
    invoke("-p", str(project_dir), "runs", "new")
    hooks.run(ev)
    assert not (project_dir / "runs" / "r-0001" / ".hooks").exists()


def test_hook_cli_reads_stdin(project_dir: Path, runner):
    from ave.cli import app

    runner.invoke(app, ["-p", str(project_dir), "runs", "new"])
    ev = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "s",
        "cwd": str(project_dir),
        "prompt_text": "hi",
    }
    res = runner.invoke(app, ["hook"], input=json.dumps(ev))
    assert res.exit_code == 0
    names = [r["name"] for r in read_trace(project_dir / "runs" / "r-0001" / "trace.jsonl")]
    assert "user:prompt" in names


def test_copywriter_without_style_skill(project_dir: Path, invoke):
    invoke("-p", str(project_dir), "config", "set", "writing.skill", "null")
    invoke("-p", str(project_dir), "agents", "install")
    fm = frontmatter(project_dir / ".claude" / "agents" / "ave-copywriter.md")
    assert "skills" not in fm and fm["name"] == "ave-copywriter"


def test_session_span_exists_before_stop(project_dir: Path, invoke):
    """A session killed before Stop still roots its tool spans."""
    invoke("-p", str(project_dir), "runs", "new")
    for ev in _events(project_dir)[:4]:  # start, prompt, one tool call; no Stop
        hooks.run(json.dumps(ev))
    from ave import tracetree

    roots = tracetree.build(project_dir / "runs" / "r-0001" / "trace.jsonl")
    session = next(r for r in roots if r.name == "claude:session")
    assert {c.name for c in session.children} >= {"user:prompt", "tool:Bash[ave analyze]"}
