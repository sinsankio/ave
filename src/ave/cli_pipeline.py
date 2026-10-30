"""Pipeline commands used by you and by the Claude Code agents:
analyze, context, schema, validate, align, catalog, coverage, storyboard, plan."""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.table import Table

from ave.cli import EXIT_USAGE, _ctx, app, console, err_console
from ave.config import Project
from ave.observability import Tracer
from ave.pipeline import (
    PipelineError,
    RunContext,
    apply_copy,
    approve_plan,
    build_evidence,
    open_run,
    reject_plan,
    run_analysis,
    submit_plan,
    validate,
)
from ave.runs import RunError
from ave.schemas import KINDS, Alignment, Catalog, Plan

copy_app = typer.Typer(help="Merge the copywriter's copy.json into a plan.", no_args_is_help=True)
app.add_typer(copy_app, name="copy")
plan_app = typer.Typer(
    help="Submit, inspect and decide on edit plans (Gate 1).", no_args_is_help=True
)
app.add_typer(plan_app, name="plan")

RunOpt = Annotated[str | None, typer.Option("--run", help="Run id; default: latest.")]
JsonFlag = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
Kind = Literal["catalog", "vision", "alignment", "plan", "copy", "critic", "change", "edl"]


def _fail(msg: str, code: int = 1) -> typer.Exit:
    err_console.print(f"[red]error:[/] {msg}", highlight=False)
    return typer.Exit(code)


def _run(ctx: typer.Context, run_id: str | None, create: bool = False) -> RunContext:
    try:
        return open_run(_ctx(ctx).project(), run_id, create=create)
    except PipelineError as e:
        raise _fail(str(e), EXIT_USAGE) from e


def _t(t: float) -> str:
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def _read(path: Path, kind: str):
    return KINDS[kind].model_validate_json(path.read_text())


# --- analyze / context ---------------------------------------------------------------


@app.command()
def analyze(
    ctx: typer.Context,
    run_id: RunOpt = None,
    new: Annotated[bool, typer.Option(help="Start a new run instead of using the latest.")] = False,
    no_ocr: Annotated[bool, typer.Option("--no-ocr", help="Skip on-screen text reading.")] = False,
    as_json: JsonFlag = False,
) -> None:
    """Signals, keyframes, contact sheets, OCR and doc sections for a run."""
    project = _ctx(ctx).project()
    if new:
        from ave.runs import RunStore

        run_id = RunStore(project.runs_dir).create().id
    rc = _run(ctx, run_id, create=True)
    tracer = Tracer.for_run(project, rc.state.id)
    log = (
        (lambda _: None)
        if as_json
        else (lambda m: err_console.print(f"[dim]{m}[/]", highlight=False))
    )
    try:
        manifest = run_analysis(rc, tracer, use_ocr=not no_ocr, log=log)
    except (PipelineError, RunError) as e:
        raise _fail(str(e)) from e
    finally:
        tracer.close()
    if as_json:
        print(json.dumps({"run": rc.state.id, "dir": str(rc.analysis_dir), **manifest}, indent=2))
        return
    for v in manifest["videos"]:
        console.print(
            f"[bold]{v['name']}[/] {_t(v['duration'])} · {v['scenes']} scenes · "
            f"{v['idle_seconds']:.1f}s idle · {v['keyframes']} keyframes · {v['sheets']} sheets"
        )
    console.print(
        f"docs: {manifest['docs']['sections']} sections from {len(manifest['docs']['files'])} files"
    )
    console.print(f"[green]analyzed[/] run {rc.state.id} → {rc.analysis_dir}")


def _context(rc: RunContext) -> dict:
    files = {
        "run_dir": rc.dir,
        "analysis_dir": rc.analysis_dir,
        "manifest": rc.analysis_dir / "manifest.json",
        "docs": rc.analysis_dir / "docs.json",
        "catalog": rc.dir / "catalog.json",
        "vision_dir": rc.dir / "vision",
        "evidence": rc.dir / "evidence.json",
        "alignment": rc.dir / "alignment.json",
        "plan_dir": rc.plan_dir,
        "feedback": rc.plan_dir / "feedback.md",
        "edl": rc.dir / "edl.json",
    }
    cfg = rc.project.config
    vision = (
        sorted(p.stem for p in (rc.dir / "vision").glob("*.json"))
        if (rc.dir / "vision").is_dir()
        else []
    )
    return {
        "run": rc.state.id,
        "status": rc.state.status.value,
        "pending_gate": rc.state.pending_gate,
        "plan_version": rc.state.plan_version,
        "project": {
            "name": cfg.name,
            "root": str(rc.project.root),
            "platforms": [p.value for p in cfg.platforms],
            "max_duration": cfg.max_duration,
            "brand": cfg.brand.model_dump(mode="json", exclude_none=True),
        },
        "paths": {k: str(v) for k, v in files.items()},
        "exists": {k: Path(v).exists() for k, v in files.items()},
        "vision_reports": vision,
        "plan_versions": rc.plan_versions(),
    }


@app.command()
def context(ctx: typer.Context, run_id: RunOpt = None) -> None:
    """JSON with the run's state and every file path the agents read or write."""
    print(json.dumps(_context(_run(ctx, run_id)), indent=2))


# --- schema / validate / align -------------------------------------------------------


@app.command()
def schema(kind: Kind) -> None:
    """JSON schema of an agent output (catalog, vision, alignment, plan, copy, critic, ...)."""
    print(json.dumps(KINDS[kind].model_json_schema(), indent=2))


@app.command(name="validate")
def validate_cmd(
    ctx: typer.Context, kind: Kind, file: Path, run_id: RunOpt = None, as_json: JsonFlag = False
) -> None:
    """Check an agent output against its schema and the run (ids, ranges, sources, cap)."""
    rc = _run(ctx, run_id)
    try:
        _, problems = validate(rc, kind, file)
    except PipelineError as e:
        if as_json:
            print(json.dumps({"ok": False, "problems": [str(e)]}, indent=2))
            raise typer.Exit(1) from e
        raise _fail(str(e)) from e
    if as_json:
        print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
    else:
        for p in problems:
            err_console.print(f"[red]problem:[/] {p}", highlight=False)
        if not problems:
            console.print(f"[green]valid[/] {kind}: {file}")
    if problems:
        raise typer.Exit(1)


@app.command(name="align")
def align_cmd(ctx: typer.Context, run_id: RunOpt = None, as_json: JsonFlag = False) -> None:
    """Find each catalog feature's UI cues in the OCR text; writes evidence.json."""
    rc = _run(ctx, run_id)
    try:
        ev = build_evidence(rc)
    except PipelineError as e:
        raise _fail(str(e)) from e
    if as_json:
        print(json.dumps(ev, indent=2))
        return
    table = Table("feature", "hits", "ranges")
    for e in ev:
        ranges = ", ".join(f"{r['video']} {_t(r['start'])}–{_t(r['end'])}" for r in e["ranges"][:4])
        table.add_row(e["feature_id"], str(len(e["hits"])), ranges or "[dim]none[/]")
    console.print(table)
    console.print(f"wrote {rc.dir / 'evidence.json'}")


# --- catalog / coverage / storyboard -------------------------------------------------


def _need(path: Path, what: str) -> Path:
    if not path.is_file():
        raise _fail(f"{path.name} not written yet ({what})", EXIT_USAGE)
    return path


@app.command()
def catalog(ctx: typer.Context, run_id: RunOpt = None, as_json: JsonFlag = False) -> None:
    """Feature catalog the doc analyst built from your docs."""
    rc = _run(ctx, run_id)
    cat: Catalog = _read(_need(rc.dir / "catalog.json", "doc analyst"), "catalog")
    if as_json:
        print(cat.model_dump_json(indent=2))
        return
    console.print(f"[bold]{cat.project_name}[/]: {cat.overview}", highlight=False)
    table = Table("feature", "name", "emphasis", "ui cues", "sources")
    for f in cat.features:
        table.add_row(
            f.id, f.name, "★" * f.emphasis, ", ".join(f.ui_cues[:4]), ", ".join(f.sources[:2])
        )
    console.print(table)


@app.command()
def coverage(ctx: typer.Context, run_id: RunOpt = None, as_json: JsonFlag = False) -> None:
    """Docs × video matrix: shown features, docs-only, video-only and conflicts."""
    rc = _run(ctx, run_id)
    cat: Catalog = _read(_need(rc.dir / "catalog.json", "doc analyst"), "catalog")
    al: Alignment = _read(_need(rc.dir / "alignment.json", "alignment agent"), "alignment")
    names = {f.id: f.name for f in cat.features}
    # features built from the recording (no docs) have nothing to check against: show "—"
    in_docs = {
        f.id: "✓" if any(not src.startswith("recording:") for src in f.sources) else "—"
        for f in cat.features
    }
    if as_json:
        print(al.model_dump_json(indent=2))
        return
    table = Table("feature", "docs", "video", "time", "conf", "note")
    for c in sorted(al.features, key=lambda c: (c.status != "conflict", c.status, -c.confidence)):
        shown = c.status in ("both", "conflict")
        when = ", ".join(f"{_t(r.start)}–{_t(r.end)}" for r in c.ranges[:2])
        conf = f"{c.confidence:.2f}" if shown else ""
        low = shown and c.confidence < 0.75
        note = (
            f"[red]⚠ {c.conflict}[/]"
            if c.status == "conflict"
            else ("[yellow]low confidence[/]" if low else "")
        )
        table.add_row(
            names.get(c.feature_id, c.feature_id),
            in_docs.get(c.feature_id, "✓"),
            "✓" if shown else "✗",
            when,
            conf,
            note,
        )
    for v in al.video_only:
        table.add_row(
            v.suggested_name or "[dim](unnamed)[/]",
            "✗",
            "✓",
            f"{_t(v.start)}–{_t(v.end)}",
            f"{v.confidence:.2f}",
            v.description,
        )
    console.print(table)
    for q in al.questions:
        console.print(f"[yellow]?[/] {q}", highlight=False)


def _plan(rc: RunContext, version: int | None) -> tuple[Plan, int]:
    try:
        f = rc.plan_file(version)
    except PipelineError as e:
        raise _fail(str(e), EXIT_USAGE) from e
    return _read(f, "plan"), int(f.stem.split(".v")[1])


def _storyboard_rows(plan: Plan, project: Project) -> list[dict]:
    rows, t = [], 0.0
    if plan.edl.start_banner:
        b = plan.edl.start_banner
        rows.append(
            {
                "kind": "banner",
                "id": "start",
                "out": [0, b.duration],
                "text": [b.title, b.subtitle or ""],
            }
        )
        t = b.duration
    for s in plan.edl.segments:
        action = (
            "keep"
            if s.speed == 1
            else (f"ffwd {s.speed:g}×" if s.speed > 1 else f"slow {s.speed:g}×")
        )
        rows.append(
            {
                "kind": "segment",
                "id": s.id,
                "feature": s.feature,
                "source": s.source,
                "src": [s.start, s.end],
                "out": [round(t, 3), round(t + s.duration, 3)],
                "action": action,
                "zooms": len(s.zooms),
                "texts": [x.text for x in s.texts],
                "audio": s.keeps_audio,
                "note": s.note,
            }
        )
        t += s.duration
    if plan.edl.end_banner:
        b = plan.edl.end_banner
        rows.append(
            {
                "kind": "banner",
                "id": "end",
                "out": [round(t, 3), round(t + b.duration, 3)],
                "text": [b.title, *b.lines],
            }
        )
    return rows


@app.command()
def storyboard(
    ctx: typer.Context,
    run_id: RunOpt = None,
    version: Annotated[int | None, typer.Option(help="Plan version; default: latest.")] = None,
    as_json: JsonFlag = False,
) -> None:
    """Timeline of a plan: what is kept, cut, sped up, zoomed and captioned."""
    rc = _run(ctx, run_id)
    plan, v = _plan(rc, version)
    rows = _storyboard_rows(plan, rc.project)
    cap = rc.project.config.max_duration
    if as_json:
        print(
            json.dumps(
                {
                    "version": v,
                    "duration": round(plan.edl.duration, 3),
                    "cap": cap,
                    "rows": rows,
                    "cuts": [c.model_dump() for c in plan.cuts],
                },
                indent=2,
            )
        )
        return
    table = Table("out", "segment", "source", "action", "zoom", "text", "why", title=f"plan v{v}")
    for r in rows:
        out = f"{_t(r['out'][0])}–{_t(r['out'][1])}"
        if r["kind"] == "banner":
            table.add_row(
                out,
                f"[dim]{r['id']} banner[/]",
                "",
                "",
                "",
                " · ".join(x for x in r["text"] if x),
                "",
            )
            continue
        src = f"{_t(r['src'][0])}–{_t(r['src'][1])}"
        table.add_row(
            out,
            r["feature"] or r["id"],
            src,
            r["action"],
            str(r["zooms"] or ""),
            " / ".join(r["texts"]),
            r["note"] or "",
        )
    console.print(table)
    console.print(
        f"duration {plan.edl.duration:.1f}s / cap {cap:g}s · target {plan.target_duration:g}s"
    )
    if plan.cuts:
        console.print("[bold]cut:[/]")
        for c in plan.cuts:
            console.print(f"  {c.video} {_t(c.start)}–{_t(c.end)}  {c.reason}", highlight=False)


# --- plan ----------------------------------------------------------------------------


@plan_app.command("submit")
def plan_submit(ctx: typer.Context, file: Path, run_id: RunOpt = None) -> None:
    """Validate a plan and store it as the next version; the run then waits at Gate 1."""
    rc = _run(ctx, run_id)
    tracer = Tracer.for_run(rc.project, rc.state.id)
    try:
        v = submit_plan(rc, file, tracer)
    except (PipelineError, RunError) as e:
        raise _fail(str(e)) from e
    finally:
        tracer.close()
    console.print(f"[green]submitted[/] plan v{v}; waiting for approval (`ave plan show`)")


@plan_app.command("show")
def plan_show(
    ctx: typer.Context,
    run_id: RunOpt = None,
    version: Annotated[int | None, typer.Option(help="Plan version; default: latest.")] = None,
) -> None:
    """Summary, storyboard and open questions of a plan."""
    rc = _run(ctx, run_id)
    plan, v = _plan(rc, version)
    console.print(f"[bold]plan v{v}[/] ({rc.state.status.value})")
    console.print(plan.summary, highlight=False)
    storyboard(ctx, run_id, v, False)
    for q in plan.questions:
        console.print(f"[yellow]?[/] {q}", highlight=False)
    for n in plan.copy_notes:
        console.print(f"[cyan]copy note:[/] {n}", highlight=False)
    if plan.post:
        console.print("[bold]LinkedIn post:[/]")
        console.print(plan.post, highlight=False, markup=False)


@plan_app.command("diff")
def plan_diff(ctx: typer.Context, a: int, b: int, run_id: RunOpt = None) -> None:
    """Unified diff between two plan versions."""
    rc = _run(ctx, run_id)
    try:
        fa, fb = rc.plan_file(a), rc.plan_file(b)
    except PipelineError as e:
        raise _fail(str(e), EXIT_USAGE) from e
    diff = difflib.unified_diff(
        fa.read_text().splitlines(),
        fb.read_text().splitlines(),
        f"plan.v{a}",
        f"plan.v{b}",
        lineterm="",
    )
    print("\n".join(diff))


@plan_app.command("approve")
def plan_approve(
    ctx: typer.Context,
    run_id: RunOpt = None,
    message: Annotated[str | None, typer.Option("-m", "--message", help="Optional note.")] = None,
) -> None:
    """Gate 1: approve the latest plan; its EDL becomes the run's edl.json."""
    rc = _run(ctx, run_id)
    tracer = Tracer.for_run(rc.project, rc.state.id)
    try:
        edl = approve_plan(rc, tracer, message)
    except (PipelineError, RunError) as e:
        raise _fail(str(e)) from e
    finally:
        tracer.close()
    console.print(f"[green]approved[/] plan v{rc.state.plan_version} → {edl}")


@plan_app.command("reject")
def plan_reject(
    ctx: typer.Context,
    message: Annotated[str, typer.Option("-m", "--message", help="What should change.")],
    run_id: RunOpt = None,
) -> None:
    """Gate 1: send the plan back with feedback; the planner revises it."""
    rc = _run(ctx, run_id)
    tracer = Tracer.for_run(rc.project, rc.state.id)
    try:
        reject_plan(rc, tracer, message)
    except (PipelineError, RunError) as e:
        raise _fail(str(e)) from e
    finally:
        tracer.close()
    console.print(
        f"[yellow]rejected[/] plan v{rc.state.plan_version}; "
        f"feedback saved to {rc.plan_dir / 'feedback.md'}"
    )


@copy_app.command("apply")
def copy_apply(
    ctx: typer.Context,
    file: Path,
    plan_file: Annotated[
        Path | None, typer.Option("--plan", help="Plan to update; default: plan/draft.json.")
    ] = None,
    run_id: RunOpt = None,
) -> None:
    """Validate copy.json and write its titles, captions, banners and post into the plan."""
    rc = _run(ctx, run_id)
    target = plan_file or rc.plan_dir / "draft.json"
    try:
        plan = apply_copy(rc, file, target)
    except PipelineError as e:
        raise _fail(str(e)) from e
    n = sum(len(s.texts) for s in plan.edl.segments)
    console.print(
        f"[green]applied[/] {n} texts, banners{' and post' if plan.post else ''} → {target}"
    )
