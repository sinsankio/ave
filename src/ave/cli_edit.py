"""Editing-phase commands: edit (proxy + QA), qa, changes, final, resume."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.table import Table

from ave import editing
from ave.cli import EXIT_USAGE, _ctx, app, console, err_console
from ave.cli_media import _do_export
from ave.config import Platform
from ave.observability import Tracer
from ave.pipeline import PipelineError, RunContext, open_run
from ave.runs import RunError, Status

qa_app = typer.Typer(help="QA results of the editing phase.", no_args_is_help=True)
changes_app = typer.Typer(help="Change requests: decide how the edit departs from the plan.")
final_app = typer.Typer(help="Gate 3: approve or reject the finished edit.", no_args_is_help=True)
app.add_typer(qa_app, name="qa")
app.add_typer(changes_app, name="changes")
app.add_typer(final_app, name="final")

RunOpt = Annotated[str | None, typer.Option("--run", help="Run id; default: latest.")]
JsonFlag = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
PlatformOpt = Annotated[
    list[Platform] | None,
    typer.Option("--platform", help="Platform (repeatable); default: project."),
]
EncoderOpt = Annotated[
    Literal["auto", "nvenc", "cpu"] | None, typer.Option(help="Override ffmpeg.encoder.")
]


def _fail(msg: str, code: int = 1) -> typer.Exit:
    err_console.print(f"[red]error:[/] {msg}", highlight=False)
    return typer.Exit(code)


def _rc(ctx: typer.Context, run_id: str | None) -> RunContext:
    try:
        return open_run(_ctx(ctx).project(), run_id)
    except PipelineError as e:
        raise _fail(str(e), EXIT_USAGE) from e


def _renderer(rc: RunContext, encoder: str | None, quiet: bool) -> editing.RenderFn:
    def render(edl_path: Path, platforms: list[Platform], out_dir: Path, proxy: bool) -> list:
        results, _, _ = _do_export(
            rc.project,
            edl_path,
            rc.state.id,
            platforms,
            out_dir,
            proxy=proxy,
            encoder=encoder,
            allow_overflow=proxy,
            workers=2,
            quiet=quiet,
        )
        return results

    return render


def _t(t: float | None) -> str:
    return "" if t is None else f"{int(t // 60):02d}:{t % 60:04.1f}"


_SEV = {"error": "red", "warn": "yellow", "info": "dim"}


def _issues_table(issues: list[dict], source: str) -> Table:
    table = Table("id", "sev", "kind", "where", "issue", "suggestion", title=source)
    for i in issues:
        where = " ".join(x for x in (i.get("platform"), i.get("segment"), _t(i.get("at"))) if x)
        sev = f"[{_SEV[i['severity']]}]{i['severity']}[/]"
        table.add_row(
            i.get("id", ""), sev, i["kind"], where, i["message"], i.get("suggestion") or ""
        )
    return table


# --- edit / qa --------------------------------------------------------------------------


@app.command(name="edit")
def edit_cmd(
    ctx: typer.Context,
    run_id: RunOpt = None,
    platform: PlatformOpt = None,
    encoder: EncoderOpt = None,
    as_json: JsonFlag = False,
) -> None:
    """Render proxies of the current edit, run QA checks and build review sheets."""
    rc = _rc(ctx, run_id)
    tracer = Tracer.for_run(rc.project, rc.state.id)
    platforms = platform or rc.project.config.platforms
    log = (lambda _: None) if as_json else (lambda m: err_console.print(f"[dim]{m}[/]"))
    try:
        report = editing.run_qa(rc, tracer, _renderer(rc, encoder, as_json), platforms, log)
    except (PipelineError, RunError) as e:
        raise _fail(str(e)) from e
    finally:
        tracer.close()
    if as_json:
        print(json.dumps(report, indent=2))
        return
    _print_qa(report, None)


def _print_qa(report: dict, critic: dict | None) -> None:
    console.print(f"edit {report['duration']:.1f}s · QA at {report['created_at']}")
    for p, f in report["proxies"].items():
        console.print(f"  proxy {p}: {f}", highlight=False)
    for p, files in report["sheets"].items():
        console.print(
            f"  review sheets {p}: {len(files)} in {Path(files[0]).parent}", highlight=False
        )
    if report["issues"]:
        console.print(_issues_table(report["issues"], "automatic checks"))
    else:
        console.print("[green]automatic checks: no issues[/]")
    if not report["has_ocr"]:
        console.print("[dim]no OCR for this run: text-covering and zoom-text checks skipped[/]")
    if critic:
        console.print(_issues_table(critic["issues"], f"QA critic: {critic['verdict']}"))
        console.print(critic["summary"], highlight=False)


@qa_app.command("show")
def qa_show(ctx: typer.Context, run_id: RunOpt = None, as_json: JsonFlag = False) -> None:
    """Latest QA: automatic issues, the critic's report, proxies and review sheets."""
    rc = _rc(ctx, run_id)
    report = editing.latest_qa(rc)
    if report is None:
        raise _fail("no QA yet; run `ave edit`", EXIT_USAGE)
    critic_file = editing.edit_dir(rc) / "critic.json"
    critic = json.loads(critic_file.read_text()) if critic_file.is_file() else None
    if critic:
        critic["issues"] = editing.critic_issues(rc)
    stale = report["edl"] != editing.edl_digest(editing.edl_file(rc))
    open_ids = [i["id"] for i in editing.open_issues(rc)]
    if as_json:
        out = {"qa": report, "critic": critic, "stale": stale, "open_issues": open_ids}
        print(json.dumps(out, indent=2))
        return
    if stale:
        console.print("[yellow]the edit changed since this QA; run `ave edit` again[/]")
    _print_qa(report, critic)
    if open_ids:
        console.print(f"[yellow]not yet decided by you:[/] {', '.join(open_ids)}")


@qa_app.command("pass")
def qa_pass_cmd(
    ctx: typer.Context,
    run_id: RunOpt = None,
    accept_risk: Annotated[
        str | None, typer.Option(help="Pass despite QA errors; say why (recorded).")
    ] = None,
) -> None:
    """Editing done: the run waits for your final approval (Gate 3)."""
    rc = _rc(ctx, run_id)
    tracer = Tracer.for_run(rc.project, rc.state.id)
    try:
        editing.qa_pass(rc, tracer, accept_risk)
    except (PipelineError, RunError) as e:
        raise _fail(str(e)) from e
    finally:
        tracer.close()
    console.print("[green]QA passed[/]; waiting for final approval (`ave final approve`)")


# --- changes ----------------------------------------------------------------------------


def _print_change(rec: dict) -> None:
    req = rec["request"]
    console.print(f"[bold]{rec['id']}[/] {req['title']}  ({rec['status']}, cause: {req['cause']})")
    console.print(req["problem"], highlight=False)
    for o in req["options"]:
        rec_mark = " [green](recommended)[/]" if o["id"] == req["recommended"] else ""
        edits = "changes the edit" if o["edl"] else "keeps the edit"
        console.print(f"  [bold]{o['id']}[/]) {o['label']}{rec_mark} · {edits}", highlight=False)
        console.print(f"     {o['description']}", highlight=False)
        console.print(f"     [dim]trade-offs: {o['tradeoffs']}[/]", highlight=False)
    if rec["decision"]:
        d = rec["decision"]
        console.print(
            f"  decided: {d['option'] or d['other']}" + (f" ({d['note']})" if d["note"] else "")
        )


@changes_app.callback(invoke_without_command=True)
def changes_root(ctx: typer.Context, run_id: RunOpt = None, as_json: JsonFlag = False) -> None:
    """List change requests (default), or use a subcommand."""
    if ctx.invoked_subcommand is not None:
        return
    rc = _rc(ctx, run_id)
    recs = editing.list_changes(rc)
    if as_json:
        print(json.dumps(recs, indent=2))
        return
    if not recs:
        console.print("no change requests")
        return
    table = Table("id", "status", "title", "decision")
    for r in recs:
        d = r["decision"]
        table.add_row(
            r["id"], r["status"], r["request"]["title"], (d["option"] or d["other"]) if d else ""
        )
    console.print(table)


@changes_app.command("show")
def changes_show(ctx: typer.Context, cr_id: str, run_id: RunOpt = None) -> None:
    """A change request with its options and trade-offs."""
    rc = _rc(ctx, run_id)
    try:
        _print_change(editing.get_change(rc, cr_id))
    except PipelineError as e:
        raise _fail(str(e), EXIT_USAGE) from e


@changes_app.command("new")
def changes_new(ctx: typer.Context, file: Path, run_id: RunOpt = None) -> None:
    """Register a change request (agents); the run waits for your decision."""
    rc = _rc(ctx, run_id)
    tracer = Tracer.for_run(rc.project, rc.state.id)
    try:
        cr_id = editing.new_change(rc, file, tracer)
    except (PipelineError, RunError) as e:
        raise _fail(str(e)) from e
    finally:
        tracer.close()
    _print_change(editing.get_change(rc, cr_id))
    console.print(f"waiting for your decision: `ave changes choose {cr_id} <option>`")


@changes_app.command("choose")
def changes_choose(
    ctx: typer.Context,
    cr_id: str,
    option: Annotated[str | None, typer.Argument(help="Option letter.")] = None,
    message: Annotated[str | None, typer.Option("-m", "--message", help="Why (recorded).")] = None,
    other: Annotated[
        str | None, typer.Option(help="None of the options: what to do instead.")
    ] = None,
    run_id: RunOpt = None,
) -> None:
    """Decide a change request; an option with an EDL replaces the current edit."""
    if (option is None) == (other is None):
        raise _fail("give an option letter or --other '...'", EXIT_USAGE)
    rc = _rc(ctx, run_id)
    tracer = Tracer.for_run(rc.project, rc.state.id)
    try:
        rec = editing.choose_change(rc, cr_id, option, message, tracer, other=other)
    except (PipelineError, RunError) as e:
        raise _fail(str(e)) from e
    finally:
        tracer.close()
    d = rec["decision"]
    applied = f"; edit updated from {d['applied_edl']}" if d["applied_edl"] else ""
    console.print(f"[green]decided[/] {cr_id}: {d['option'] or d['other']}{applied}")
    if d["applied_edl"]:
        console.print("next: `ave edit` to re-render and re-check")


# --- final gate -------------------------------------------------------------------------


@final_app.command("approve")
def final_approve_cmd(
    ctx: typer.Context,
    run_id: RunOpt = None,
    message: Annotated[str | None, typer.Option("-m", "--message")] = None,
    platform: PlatformOpt = None,
    encoder: EncoderOpt = None,
) -> None:
    """Approve the edit: render the finals for every platform and finish the run."""
    rc = _rc(ctx, run_id)
    tracer = Tracer.for_run(rc.project, rc.state.id)
    platforms = platform or rc.project.config.platforms
    try:
        results = editing.final_approve(
            rc, tracer, _renderer(rc, encoder, False), platforms, message
        )
    except (PipelineError, RunError) as e:
        raise _fail(str(e)) from e
    finally:
        tracer.close()
    for r in results:
        console.print(
            f"[green]final[/] {r.platform}: {r.path} ({r.duration:.1f}s)", highlight=False
        )
    post = rc.dir / "out" / "linkedin.post.txt"
    if post.is_file():
        console.print(f"LinkedIn post: {post}", highlight=False)
    console.print(f"run {rc.state.id} done")


@final_app.command("reject")
def final_reject_cmd(
    ctx: typer.Context,
    message: Annotated[str, typer.Option("-m", "--message", help="What should change.")],
    run_id: RunOpt = None,
) -> None:
    """Send the edit back to editing with your feedback."""
    rc = _rc(ctx, run_id)
    tracer = Tracer.for_run(rc.project, rc.state.id)
    try:
        editing.final_reject(rc, tracer, message)
    except (PipelineError, RunError) as e:
        raise _fail(str(e)) from e
    finally:
        tracer.close()
    console.print("[yellow]sent back to editing[/]; feedback saved to edit/final-feedback.md")


# --- resume -------------------------------------------------------------------------------


@app.command(name="resume")
def resume_cmd(
    ctx: typer.Context,
    run_id: RunOpt = None,
    dry_run: Annotated[
        bool, typer.Option(help="Print the command instead of starting Claude.")
    ] = False,
) -> None:
    """Continue a run in Claude Code; a failed run first returns to the phase it failed in."""
    from ave.cli_agents import launch

    rc = _rc(ctx, run_id)
    if rc.state.status is Status.FAILED:
        if rc.state.failed_in is None:
            raise _fail(f"run {rc.state.id} failed at a gate and can't be resumed; start a new run")
        rc.move(rc.state.failed_in, "resumed")
        console.print(f"run {rc.state.id} back to {rc.state.status.value}")
    launch(rc.project, rc.state.id, dry_run)
