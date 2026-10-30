"""Interactive `ave` shell: same commands as the CLI, typed without the `ave` prefix."""

from __future__ import annotations

import shlex
from pathlib import Path

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import NestedCompleter
from prompt_toolkit.history import FileHistory, InMemoryHistory
from rich.console import Console

from ave.config import ConfigError, load_project
from ave.runs import RunStore

console = Console()
EXIT_WORDS = {"exit", "quit", ":q"}


def _command():
    from ave.cli import app

    return typer.main.get_command(app)


def command_names() -> list[str]:
    cmd = _command()
    return sorted(cmd.list_commands(typer.Context(cmd)))


def command_tree() -> dict:
    """{command: {subcommand: None} | None} for nested tab completion."""
    root = _command()
    tree: dict = {}
    for name in root.list_commands(typer.Context(root)):
        cmd = root.get_command(typer.Context(root), name)
        if cmd is None or getattr(cmd, "hidden", False):
            continue
        subs = None
        if hasattr(cmd, "list_commands"):
            subs = {s: None for s in cmd.list_commands(typer.Context(cmd))} or None
        tree[name] = subs
    tree["help"] = {n: None for n in tree}
    tree["exit"] = None
    return tree


def run_line(line: str, project: Path | None = None) -> int:
    """Run one shell line through the CLI and return its exit code."""
    try:
        args = shlex.split(line)
    except ValueError as e:
        console.print(f"[red]error:[/] {e}")
        return 2
    if args and args[0] == "ave":
        args = args[1:]
    if not args:
        return 0
    if args in (["help"], ["?"]):
        args = ["--help"]
    elif args[0] in ("help", "?"):
        args = [*args[1:], "--help"]  # help plan approve -> plan approve --help
    if project is not None and "-p" not in args and "--project" not in args:
        args = ["-p", str(project), *args]
    try:
        rv = _command().main(args=args, prog_name="ave", standalone_mode=False)
    except typer.Abort:
        console.print("aborted")
        return 1
    except typer.TyperException as e:  # usage errors, bad parameters
        if hasattr(e, "show"):
            e.show()
        else:
            console.print(f"[red]error:[/] {e}")
        return getattr(e, "exit_code", 2)
    except SystemExit as e:  # defensive: some code paths still raise it
        return int(e.code or 0)
    return rv if isinstance(rv, int) else 0


def _state(project: Path | None):
    try:
        proj = load_project(project)
    except ConfigError:
        return None, None
    return proj, RunStore(proj.runs_dir).latest()


def banner(project: Path | None) -> str:
    proj, latest = _state(project)
    if proj is None:
        return "ave shell · no project here (try `init`) · `help` for commands, `exit` to leave"
    run = f"run {latest.id} ({latest.status.value})" if latest else "no runs"
    tracing = proj.config.observability.provider
    return f"ave shell · project: {proj.config.name} · {run} · tracing: {tracing}"


def prompt_text(project: Path | None) -> str:
    """`ave r-0002 ⏸ plan_approval ▸ ` while a gate waits, `ave r-0002 ▸ ` otherwise."""
    _, latest = _state(project)
    if latest is None:
        return "ave ▸ "
    gate = f" ⏸ {latest.pending_gate}" if latest.pending_gate else ""
    return f"ave {latest.id}{gate} ▸ "


def next_hint(project: Path | None) -> str | None:
    from ave.cli import _NEXT_HINT

    _, latest = _state(project)
    if latest is None:
        return None
    hint = _NEXT_HINT.get(latest.status.value)
    return f"next: {hint}" if hint else None


def start_shell(project: Path | None = None) -> None:
    proj, _ = _state(project)
    if proj is not None:
        proj.state_dir.mkdir(exist_ok=True)
        history = FileHistory(str(proj.state_dir / "history"))
    else:
        history = InMemoryHistory()
    session = PromptSession(
        history=history, completer=NestedCompleter.from_nested_dict(command_tree())
    )
    console.print(banner(project))
    if hint := next_hint(project):
        console.print(f"[dim]{hint}[/]")
    last_status = _state(project)[1]
    while True:
        try:
            line = session.prompt(prompt_text(project))
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        if line.strip() in EXIT_WORDS:
            break
        run_line(line, project)
        now = _state(project)[1]
        moved = now is not None and (
            last_status is None or (now.id, now.status) != (last_status.id, last_status.status)
        )
        if moved and (hint := next_hint(project)):
            console.print(f"[dim]{hint}[/]")  # the run moved: say what comes next
        last_status = now
