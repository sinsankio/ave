"""`ave` command line. With no subcommand it opens the interactive shell."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console
from rich.prompt import FloatPrompt, Prompt
from rich.table import Table

from ave import __version__, doctor
from ave.config import (
    PROJECT_FILE,
    ConfigError,
    Observability,
    Platform,
    Project,
    ProjectConfig,
    dump_config,
    get_key,
    load_env_file,
    load_project,
    set_key,
)
from ave.observability import Tracer
from ave.runs import RunError, RunState, RunStore

console = Console()
err_console = Console(stderr=True)

EXIT_USAGE = 2

app = typer.Typer(
    help="AI video editor for software demos. Run `ave` with no command for the shell.",
    no_args_is_help=False,
    add_completion=False,
)
config_app = typer.Typer(help="Read and change project.yaml.", no_args_is_help=True)
runs_app = typer.Typer(help="List, create and inspect runs.")
app.add_typer(config_app, name="config")
app.add_typer(runs_app, name="runs")


@dataclass
class Ctx:
    project_path: Path | None

    def project(self) -> Project:
        try:
            return load_project(self.project_path)
        except ConfigError as e:
            err_console.print(f"[red]error:[/] {e}")
            raise typer.Exit(EXIT_USAGE) from e

    def project_or_none(self) -> Project | None:
        try:
            return load_project(self.project_path)
        except ConfigError:
            return None


def _ctx(ctx: typer.Context) -> Ctx:
    return ctx.obj if isinstance(ctx.obj, Ctx) else Ctx(None)


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    project: Annotated[
        Path | None,
        typer.Option("--project", "-p", help=f"Project dir or {PROJECT_FILE} path."),
    ] = None,
    version: Annotated[bool, typer.Option("--version", help="Print version and exit.")] = False,
) -> None:
    if version:
        console.print(f"ave {__version__}")
        raise typer.Exit()
    ctx.obj = Ctx(project)
    if ctx.invoked_subcommand is None:
        from ave.shell import start_shell

        start_shell(project)


# --- init ----------------------------------------------------------------------------


def _split(values: list[str]) -> list[str]:
    return [v.strip() for raw in values for v in raw.split(",") if v.strip()]


@app.command()
def init(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Option(help="Project name.")] = None,
    video: Annotated[
        list[Path] | None, typer.Option("--video", help="Demo video (repeatable).")
    ] = None,
    docs: Annotated[
        list[str] | None, typer.Option("--docs", help="Doc path or glob (repeatable).")
    ] = None,
    platform: Annotated[
        list[Platform] | None, typer.Option("--platform", help="Target platform (repeatable).")
    ] = None,
    max_duration: Annotated[float | None, typer.Option(help="Hard cap in seconds.")] = None,
    backend: Annotated[
        Literal["claude_code", "api"],
        typer.Option(help="Where agents run: Claude Code subscription or Anthropic API."),
    ] = "claude_code",
    tracing: Annotated[
        Literal["langsmith", "local", "none"] | None, typer.Option(help="Observability provider.")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="No prompts; use flags and defaults.")
    ] = False,
    force: Annotated[bool, typer.Option(help=f"Overwrite an existing {PROJECT_FILE}.")] = False,
    agents: Annotated[
        bool, typer.Option(help="Install Claude Code agents, /ave-edit and hooks (claude_code).")
    ] = True,
) -> None:
    """Create project.yaml (interactive unless --yes)."""
    target = _ctx(ctx).project_path or Path.cwd()
    file = target / PROJECT_FILE if target.suffix not in (".yaml", ".yml") else target
    root_dir = file.parent
    if file.exists() and not force:
        err_console.print(f"[red]error:[/] {file} exists (use --force to overwrite)")
        raise typer.Exit(EXIT_USAGE)

    videos = [str(v) for v in video or []]
    doc_list = list(docs or [])
    platforms = [p.value for p in platform or []]
    if not yes:
        name = name or Prompt.ask("Project name", default=root_dir.resolve().name)
        if not videos:
            videos = _split([Prompt.ask("Demo video path(s), comma separated", default="")])
        if not doc_list:
            doc_list = _split(
                [Prompt.ask("Docs (.md paths or globs), comma separated", default="")]
            )
        if not platforms:
            choices = ", ".join(p.value for p in Platform)
            platforms = _split([Prompt.ask(f"Platforms ({choices})", default="linkedin")])
        if max_duration is None:
            max_duration = FloatPrompt.ask("Max duration (s)", default=60.0)
        if tracing is None:
            tracing = Prompt.ask("Tracing", choices=["langsmith", "local", "none"], default="local")

    try:
        cfg = ProjectConfig.model_validate(
            {
                "name": name or root_dir.resolve().name,
                "videos": videos,
                "docs": doc_list,
                "platforms": platforms or ["linkedin"],
                "max_duration": max_duration if max_duration is not None else 60.0,
                "observability": Observability(provider=tracing or "local").model_dump(),
                "inference": {"backend": backend},
            }
        )
    except ValueError as e:
        err_console.print(f"[red]error:[/] {e}")
        raise typer.Exit(EXIT_USAGE) from e

    root_dir.mkdir(parents=True, exist_ok=True)
    dump_config(cfg, file)
    console.print(f"[green]wrote[/] {file}")
    project = Project(root_dir.resolve(), cfg)
    missing = [v for v in cfg.videos if not project.resolve(v).is_file()]
    for v in missing:
        console.print(f"[yellow]warn:[/] video not found yet: {v}")
    if cfg.docs and not project.doc_files():
        console.print("[yellow]warn:[/] no doc files match the docs entries yet")
    if cfg.observability.provider == "langsmith":
        console.print("tracing: set LANGSMITH_API_KEY in your environment, then `ave doctor`")
    if cfg.inference.backend == "api":
        console.print("inference: set ANTHROPIC_API_KEY in your environment, then `ave doctor`")
    elif agents:
        from ave import agents as agent_assets

        agent_assets.install(project)
        console.print("installed Claude Code agents, /ave-edit and hooks in .claude/")


# --- doctor --------------------------------------------------------------------------

_STATUS_STYLE = {"ok": "green", "warn": "yellow", "fail": "red", "skip": "dim"}


@app.command(name="doctor")
def doctor_cmd(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    online: Annotated[
        bool, typer.Option(help="Also call the Anthropic and LangSmith APIs.")
    ] = False,
    deep: Annotated[bool, typer.Option(help="Test-encode a frame with NVENC.")] = False,
    skip: Annotated[
        list[str] | None,
        typer.Option(help=f"Skip a check ({', '.join(doctor.CHECK_NAMES)}); repeatable."),
    ] = None,
) -> None:
    """Check ffmpeg, GPU, credentials and optional dependencies."""
    unknown = set(skip or []) - set(doctor.CHECK_NAMES)
    if unknown:
        err_console.print(f"[red]error:[/] unknown check(s): {', '.join(sorted(unknown))}")
        raise typer.Exit(EXIT_USAGE)
    project = _ctx(ctx).project_or_none()
    checks = doctor.run_checks(
        project.config if project else None,
        online=online,
        deep=deep,
        skip=frozenset(skip or []),
    )
    ok = doctor.healthy(checks)
    if as_json:
        print(json.dumps({"healthy": ok, "checks": [c.to_dict() for c in checks]}, indent=2))
    else:
        table = Table(
            "check",
            "status",
            "detail",
            title=f"ave doctor ({'project: ' + project.config.name if project else 'no project'})",
        )
        for c in checks:
            style = _STATUS_STYLE[c.status]
            req = "" if c.required else " [dim](optional)[/]"
            table.add_row(c.name, f"[{style}]{c.status}[/]{req}", c.detail)
        console.print(table)
        console.print("[green]healthy[/]" if ok else "[red]not healthy: fix the failed checks[/]")
    if not ok:
        raise typer.Exit(1)


# --- config --------------------------------------------------------------------------


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Print the whole project.yaml."""
    project = _ctx(ctx).project()
    console.print(project.file.read_text(), highlight=False, markup=False)


@config_app.command("get")
def config_get(ctx: typer.Context, key: str) -> None:
    """Print one value by dotted key, e.g. observability.provider."""
    project = _ctx(ctx).project()
    try:
        value = get_key(project.config, key)
    except ConfigError as e:
        err_console.print(f"[red]error:[/] {e}")
        raise typer.Exit(EXIT_USAGE) from e
    print(json.dumps(value) if isinstance(value, (dict, list)) else value)


@config_app.command("set")
def config_set(ctx: typer.Context, key: str, value: str) -> None:
    """Set one value by dotted key. VALUE is parsed as YAML: `[linkedin, reels]`, `45`."""
    project = _ctx(ctx).project()
    try:
        project.config = set_key(project.config, key, value)
    except ConfigError as e:
        err_console.print(f"[red]error:[/] {e}")
        raise typer.Exit(EXIT_USAGE) from e
    project.save()
    console.print(f"[green]set[/] {key} = {get_key(project.config, key)}")


# --- runs and status -----------------------------------------------------------------


def _run_row(s: RunState) -> dict:
    return {
        "id": s.id,
        "status": s.status.value,
        "pending_gate": s.pending_gate,
        "plan_version": s.plan_version,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


def _print_runs(runs: list[RunState], as_json: bool) -> None:
    if as_json:
        print(json.dumps([_run_row(r) for r in runs], indent=2))
        return
    if not runs:
        console.print("no runs yet; `ave runs new` creates one")
        return
    table = Table("run", "status", "gate", "plan", "updated")
    for r in runs:
        table.add_row(
            r.id,
            r.status.value,
            r.pending_gate or "",
            f"v{r.plan_version}",
            f"{r.updated_at:%Y-%m-%d %H:%M}",
        )
    console.print(table)


@runs_app.callback(invoke_without_command=True)
def runs_root(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List runs (default), or use a subcommand."""
    if ctx.invoked_subcommand is None:
        project = _ctx(ctx).project()
        _print_runs(RunStore(project.runs_dir).list(), as_json)


@runs_app.command("list")
def runs_list(ctx: typer.Context, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List runs."""
    project = _ctx(ctx).project()
    _print_runs(RunStore(project.runs_dir).list(), as_json)


@runs_app.command("new")
def runs_new(ctx: typer.Context, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Create a run in state `created`."""
    project = _ctx(ctx).project()
    store = RunStore(project.runs_dir)
    state = store.create()
    tracer = Tracer.for_run(project, state.id)
    try:
        with tracer.span(
            f"run:{state.id}", kind="run", inputs={"project": project.config.name}
        ) as sp:
            sp.set_outputs(status=state.status.value)
    finally:
        tracer.close()
    if as_json:
        print(json.dumps(_run_row(state)))
    else:
        console.print(f"[green]created[/] {state.id}")
        if url := tracer.trace_url():
            console.print(f"trace → {url}")


def _resolve_run(project: Project, run_id: str | None) -> RunState:
    store = RunStore(project.runs_dir)
    try:
        state = store.get(run_id) if run_id else store.latest()
    except RunError as e:
        err_console.print(f"[red]error:[/] {e}")
        raise typer.Exit(EXIT_USAGE) from e
    if state is None:
        err_console.print("[red]error:[/] no runs yet; `ave runs new` creates one")
        raise typer.Exit(EXIT_USAGE)
    return state


_NEXT_HINT = {
    "created": "ave analyze",
    "analyzed": "ave plan",
    "awaiting_plan_approval": "ave plan show | ave plan approve | ave plan reject -m ...",
    "plan_approved": "ave edit",
    "editing": "ave qa show | ave changes new <file> | ave qa pass",
    "awaiting_change_decision": "ave changes list | ave changes choose <id> <option>",
    "awaiting_final_approval": "watch the proxy, then ave final approve | ave final reject -m ...",
    "failed": "ave resume",
}


@app.command()
def status(
    ctx: typer.Context,
    run_id: Annotated[str | None, typer.Argument(help="Run id; latest if omitted.")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show a run's phase, pending gate and history."""
    project = _ctx(ctx).project()
    state = _resolve_run(project, run_id)
    if as_json:
        row = _run_row(state) | {
            "error": state.error,
            "history": [t.model_dump(mode="json") for t in state.history],
        }
        print(json.dumps(row, indent=2))
        return
    console.print(f"[bold]{state.id}[/]  {state.status.value}  plan v{state.plan_version}")
    if state.pending_gate:
        console.print(f"[yellow]waiting on gate:[/] {state.pending_gate}")
    if state.error:
        console.print(f"[red]error:[/] {state.error}")
    if hint := _NEXT_HINT.get(state.status.value):
        console.print(f"next: {hint}")
    for t in state.history[-5:]:
        note = f"  ({t.note})" if t.note else ""
        console.print(f"  {t.at:%H:%M:%S}  {t.src.value} → {t.dst.value}{note}", highlight=False)


@runs_app.command("show")
def runs_show(
    ctx: typer.Context, run_id: str, as_json: Annotated[bool, typer.Option("--json")] = False
) -> None:
    """Same as `ave status RUN_ID`."""
    status(ctx, run_id, as_json)


# media commands live in their own module; importing it registers them on `app`
from ave import cli_agents, cli_edit, cli_insights, cli_media, cli_pipeline  # noqa: E402, F401


def main() -> None:
    load_env_file()
    app()


if __name__ == "__main__":
    main()
