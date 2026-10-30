"""`ave agents ...`, `ave run` (start Claude Code on /ave-edit) and the hidden `ave hook`."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from ave import agents, hooks
from ave.cli import EXIT_USAGE, _ctx, app, console, err_console
from ave.runs import RunStore, Status

agents_app = typer.Typer(help="Claude Code sub-agents, /ave-edit and hooks for this project.")
app.add_typer(agents_app, name="agents")

_STYLE = {"current": "green", "missing": "red", "outdated": "yellow", "modified": "yellow"}


def _show(results: list[agents.AssetStatus], root: Path) -> None:
    table = Table("file", "status")
    for r in results:
        rel = r.path.relative_to(root) if r.path.is_relative_to(root) else r.path
        table.add_row(str(rel), f"[{_STYLE[r.status]}]{r.status}[/]")
    console.print(table)


@agents_app.callback(invoke_without_command=True)
def agents_root(ctx: typer.Context) -> None:
    """Show whether the project's Claude Code assets are installed and current."""
    if ctx.invoked_subcommand is None:
        project = _ctx(ctx).project()
        _show(agents.status(project), project.root)


@agents_app.command("install")
def agents_install(
    ctx: typer.Context,
    force: Annotated[bool, typer.Option(help="Overwrite files you edited.")] = False,
) -> None:
    """Write .claude/agents, .claude/commands/ave-edit.md and hooks into the project."""
    project = _ctx(ctx).project()
    try:
        results = agents.install(project, force=force)
    except ValueError as e:
        err_console.print(f"[red]error:[/] {e}")
        raise typer.Exit(1) from e
    _show(results, project.root)
    if any(r.status == "modified" for r in results):
        console.print("[yellow]kept your edited files[/]; `--force` replaces them")
    console.print(f"hooks call {agents.hook_command()}; permission rule {agents.PERMISSION}")


@app.command(name="hook", hidden=True)
def hook_cmd() -> None:
    """Claude Code hook entry point: reads the event JSON on stdin."""
    hooks.run(sys.stdin.read())


def claude_command(project, run_id: str) -> list[str]:
    return [project.config.inference.claude_path, f"/ave-edit {run_id}"]


@app.command(name="run")
def run_cmd(
    ctx: typer.Context,
    new: Annotated[
        bool, typer.Option(help="Start a new run instead of resuming the latest.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option(help="Print the command instead of starting Claude.")
    ] = False,
) -> None:
    """Start Claude Code on /ave-edit for the latest (or a new) run."""
    project = _ctx(ctx).project()
    if project.config.inference.backend != "claude_code":
        err_console.print("[red]error:[/] `ave run` needs inference.backend=claude_code")
        raise typer.Exit(EXIT_USAGE)
    store = RunStore(project.runs_dir)
    latest = store.latest()
    if new or latest is None or latest.status is Status.DONE:
        latest = store.create()
    launch(project, latest.id, dry_run)


def launch(project, run_id: str, dry_run: bool = False) -> None:
    """Start (exec) Claude Code on /ave-edit for a run, refreshing the agents first."""
    stale = [r for r in agents.status(project) if r.status in ("missing", "outdated")]
    if stale:
        agents.install(project)
    cmd = claude_command(project, run_id)
    # agents call `ave` through Bash: make sure this ave is the one on PATH
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    if dry_run:
        print(json.dumps({"cwd": str(project.root), "cmd": cmd, "run": run_id}))
        return
    if shutil.which(cmd[0], path=env["PATH"]) is None:
        err_console.print(f"[red]error:[/] {cmd[0]!r} not found; install Claude Code")
        raise typer.Exit(1)
    console.print(f"starting Claude Code on run {run_id} …")
    os.chdir(project.root)
    os.execvpe(cmd[0], cmd, env)
