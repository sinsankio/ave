from pathlib import Path

from ave.cli import EXIT_USAGE
from ave.shell import banner, command_names, run_line


def test_command_names_cover_cli():
    names = command_names()
    for expected in ("init", "doctor", "status", "runs", "config", "plan", "export"):
        assert expected in names


def test_run_line_dispatches(project_dir: Path, capsys):
    assert run_line("runs new", project_dir) == 0
    assert run_line("ave status", project_dir) == 0  # leading `ave` is optional
    out = capsys.readouterr().out
    assert "created r-0001" in out
    assert "r-0001" in out


def test_run_line_exit_codes(project_dir: Path):
    assert run_line("", project_dir) == 0
    assert run_line("ave", project_dir) == 0  # does not open a nested shell
    assert run_line("status r-0404", project_dir) == EXIT_USAGE
    assert run_line("no-such-command", project_dir) == 2
    assert run_line('runs "unclosed', project_dir) == 2


def test_help(project_dir: Path, capsys):
    assert run_line("help", project_dir) == 0
    assert "Commands" in capsys.readouterr().out


def test_banner(project_dir: Path, tmp_path_factory):
    assert "project: Test Demo" in banner(project_dir)
    assert "no runs" in banner(project_dir)
    run_line("runs new", project_dir)
    assert "run r-0001 (created)" in banner(project_dir)
    assert "no project" in banner(tmp_path_factory.mktemp("empty"))


def test_command_tree_nests_subcommands():
    from ave.shell import command_tree

    tree = command_tree()
    assert {"approve", "reject", "submit"} <= set(tree["plan"])
    assert {"choose", "new"} <= set(tree["changes"])
    assert tree["status"] is None and "hook" not in tree  # hidden command stays hidden
    assert "plan" in tree["help"]


def test_help_for_subcommand(project_dir: Path, capsys):
    assert run_line("help plan approve", project_dir) == 0
    assert "Gate 1" in capsys.readouterr().out


def test_prompt_and_hint_follow_the_run(project_dir: Path):
    import json

    from ave.shell import next_hint, prompt_text

    assert prompt_text(project_dir) == "ave ▸ "
    run_line("runs new", project_dir)
    assert prompt_text(project_dir) == "ave r-0001 ▸ "
    assert next_hint(project_dir) == "next: ave analyze"
    state = project_dir / "runs" / "r-0001" / "state.json"
    data = json.loads(state.read_text()) | {"status": "awaiting_plan_approval"}
    state.write_text(json.dumps(data))
    assert prompt_text(project_dir) == "ave r-0001 ⏸ plan_approval ▸ "
