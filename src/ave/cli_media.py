"""`ave probe`, `ave edl ...`, `ave export`, `ave preview`. Registered onto the main app."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.table import Table

from ave.cli import EXIT_USAGE, _ctx, app, console, err_console
from ave.config import PRESETS, Platform, Project
from ave.edl import EDL, EXAMPLE, EDLError, check_against_media, load_edl, resolve_sources
from ave.media import MediaError, Tools, pick_encoder, probe
from ave.observability import Tracer
from ave.render import Renderer, RenderError, export
from ave.runs import RunError, RunStore
from ave.sheet import contact_sheet

edl_app = typer.Typer(help="Validate and inspect edit decision lists.", no_args_is_help=True)
app.add_typer(edl_app, name="edl")

JsonFlag = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]


def _fail(msg: str, code: int = 1) -> typer.Exit:
    err_console.print(f"[red]error:[/] {msg}")
    return typer.Exit(code)


def _fmt_t(t: float) -> str:
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


# --- probe -----------------------------------------------------------------------------


@app.command(name="probe")
def probe_cmd(ctx: typer.Context, video: Path, as_json: JsonFlag = False) -> None:
    """Show duration, size, frame rate and audio of a video."""
    project = _ctx(ctx).project_or_none()
    tools = Tools.from_config(project.config.ffmpeg if project else None)
    try:
        info = probe(video, tools)
    except MediaError as e:
        raise _fail(str(e)) from e
    if as_json:
        print(json.dumps(info.to_dict(), indent=2))
        return
    audio = "yes" if info.has_audio else "no"
    console.print(
        f"{info.path}: {_fmt_t(info.duration)} · {info.width}x{info.height} · "
        f"{info.fps:.2f} fps · {info.codec} · audio {audio}",
        highlight=False,
    )


# --- edl -------------------------------------------------------------------------------


@edl_app.command("schema")
def edl_schema() -> None:
    """Print the EDL JSON schema (agents write EDLs against it)."""
    print(json.dumps(EDL.model_json_schema(), indent=2))


@edl_app.command("example")
def edl_example(
    out: Annotated[Path | None, typer.Option(help="Write to this file instead of stdout.")] = None,
) -> None:
    """Print a commented example EDL to start from."""
    if out is None:
        print(EXAMPLE, end="")
        return
    if out.exists():
        raise _fail(f"{out} exists", EXIT_USAGE)
    out.write_text(EXAMPLE)
    console.print(f"[green]wrote[/] {out}")


@edl_app.command("validate")
def edl_validate(ctx: typer.Context, file: Path, as_json: JsonFlag = False) -> None:
    """Check an EDL against its sources and the duration cap; print the timeline."""
    project = _ctx(ctx).project_or_none()
    tools = Tools.from_config(project.config.ffmpeg if project else None)
    try:
        edl = load_edl(file)
    except EDLError as e:
        raise _fail(str(e)) from e
    problems: list[str] = []
    media = {}
    for name, path in resolve_sources(edl, file.parent).items():
        try:
            media[name] = probe(path, tools)
        except MediaError as e:
            problems.append(f"source {name!r}: {e}")
    if len(media) == len(edl.sources):
        problems += check_against_media(edl, media)
    cap = project.config.max_duration if project else None
    if cap is not None and edl.duration > cap + 1e-6:
        problems.append(f"edit is {edl.duration:.1f}s, over the {cap:g}s cap")
    targets = {}
    if project:
        for p in project.config.platforms:
            pr = PRESETS[p]
            targets[p.value] = {"min": pr.target_min, "max": pr.target_max}

    if as_json:
        rows = [
            {"id": s.id, "source": s.source, "start": s.start, "end": s.end, "speed": s.speed,
             "duration": round(s.duration, 3), "audio": s.keeps_audio,
             "zooms": len(s.zooms), "texts": len(s.texts)}
            for s in edl.segments
        ]  # fmt: skip
        report = {
            "ok": not problems,
            "duration": round(edl.duration, 3),
            "cap": cap,
            "targets": targets,
            "segments": rows,
            "problems": problems,
        }
        print(json.dumps(report, indent=2))
    else:
        table = Table("#", "segment", "source range", "speed", "output", "zoom", "text", "audio")
        t = edl.start_banner.duration if edl.start_banner else 0.0
        if edl.start_banner:
            table.add_row(
                "", "[dim]start banner[/]", "", "", f"{_fmt_t(0)}–{_fmt_t(t)}", "", "", ""
            )
        for i, s in enumerate(edl.segments, 1):
            table.add_row(
                str(i), s.id, f"{_fmt_t(s.start)}–{_fmt_t(s.end)}", f"{s.speed:g}×",
                f"{_fmt_t(t)}–{_fmt_t(t + s.duration)}", str(len(s.zooms)) if s.zooms else "",
                str(len(s.texts)) if s.texts else "", "on" if s.keeps_audio else "muted",
            )  # fmt: skip
            t += s.duration
        if edl.end_banner:
            end = t + edl.end_banner.duration
            table.add_row(
                "", "[dim]end banner[/]", "", "", f"{_fmt_t(t)}–{_fmt_t(end)}", "", "", ""
            )
        console.print(table)
        cap_txt = f" / cap {cap:g}s" if cap is not None else ""
        console.print(f"total {edl.duration:.1f}s{cap_txt}")
        for name, rng in targets.items():
            inside = rng["min"] <= edl.duration <= rng["max"]
            mark = "[green]in range[/]" if inside else "[yellow]outside target[/]"
            console.print(f"  {name}: target {rng['min']:g}–{rng['max']:g}s · {mark}")
        for p in problems:
            err_console.print(f"[red]problem:[/] {p}")
        if not problems:
            console.print("[green]valid[/]")
    if problems:
        raise typer.Exit(1)


# --- export / preview ------------------------------------------------------------------


def _resolve_edl(project: Project, edl: Path | None, run_id: str | None) -> tuple[Path, str | None]:
    if edl is not None:
        return edl, run_id
    store = RunStore(project.runs_dir)
    try:
        state = store.get(run_id) if run_id else store.latest()
    except RunError as e:
        raise _fail(str(e), EXIT_USAGE) from e
    if state is not None and (store.run_dir(state.id) / "edl.json").is_file():
        return store.run_dir(state.id) / "edl.json", state.id
    raise _fail("no EDL: pass --edl FILE (see `ave edl example`)", EXIT_USAGE)


def _out_dir(project: Project, edl_path: Path, run_id: str | None, out: Path | None) -> Path:
    if out is not None:
        return out
    if run_id:
        return project.runs_dir / run_id / "out"
    return project.root / "out" / edl_path.stem


def _do_export(
    project: Project,
    edl_path: Path,
    run_id: str | None,
    platforms: list[Platform],
    out_dir: Path,
    *,
    proxy: bool,
    encoder: str | None,
    allow_overflow: bool,
    workers: int,
    quiet: bool = False,
):
    try:
        edl = load_edl(edl_path)
    except EDLError as e:
        raise _fail(str(e)) from e
    tools = Tools.from_config(project.config.ffmpeg)
    try:
        enc = pick_encoder(encoder or project.config.ffmpeg.encoder, tools)
    except MediaError as e:
        raise _fail(str(e)) from e
    tracer = Tracer.for_run(project, run_id)
    brand = project.config.brand
    if brand.photo:
        brand = brand.model_copy(update={"photo": project.resolve(brand.photo)})
    renderer = Renderer(tools=tools, encoder=enc, brand=brand,
                        cache_dir=project.state_dir / "cache", tracer=tracer)  # fmt: skip

    def progress(msg: str) -> None:
        if not quiet:
            err_console.print(f"[dim]{msg}[/]", highlight=False)

    try:
        results = export(
            edl, edl_path.parent, platforms, out_dir, renderer, proxy=proxy,
            max_duration=None if allow_overflow else project.config.max_duration,
            workers=workers, on_progress=progress,
        )  # fmt: skip
    except RenderError as e:
        raise _fail(str(e)) from e
    finally:
        tracer.close()
    return results, enc, tracer


EncoderOpt = Annotated[
    Literal["auto", "nvenc", "cpu"] | None, typer.Option(help="Override ffmpeg.encoder.")
]
PlatformOpt = Annotated[
    list[Platform] | None,
    typer.Option("--platform", help="Platform (repeatable); default: project."),
]
EdlOpt = Annotated[
    Path | None, typer.Option("--edl", help="EDL file; default: the run's edl.json.")
]
RunOpt = Annotated[str | None, typer.Option("--run", help="Run id; default: latest.")]


@app.command(name="export")
def export_cmd(
    ctx: typer.Context,
    edl: EdlOpt = None,
    run_id: RunOpt = None,
    platform: PlatformOpt = None,
    proxy: Annotated[bool, typer.Option(help="Half resolution, fast encode, no loudnorm.")] = False,
    out: Annotated[Path | None, typer.Option(help="Output directory.")] = None,
    encoder: EncoderOpt = None,
    allow_overflow: Annotated[bool, typer.Option(help="Render even if over max_duration.")] = False,
    workers: Annotated[int, typer.Option(min=1, max=8, help="Clips rendered in parallel.")] = 2,
    as_json: JsonFlag = False,
) -> None:
    """Render the EDL to one video per platform."""
    project = _ctx(ctx).project()
    edl_path, run = _resolve_edl(project, edl, run_id)
    platforms = platform or project.config.platforms
    out_dir = _out_dir(project, edl_path, run, out)
    results, enc, tracer = _do_export(
        project, edl_path, run, platforms, out_dir, proxy=proxy, encoder=encoder,
        allow_overflow=allow_overflow, workers=workers, quiet=as_json,
    )  # fmt: skip
    post = _copy_post(project, run, out_dir, platforms)
    if as_json:
        report = {"encoder": enc, "results": [r.to_dict() for r in results], "post": post}
        print(json.dumps(report, indent=2, default=str))
        return
    table = Table("platform", "file", "duration", "size", "clips", "time")
    for r in results:
        table.add_row(
            r.platform,
            str(r.path),
            f"{r.duration:.1f}s",
            f"{r.size_bytes / 1e6:.1f} MB",
            f"{r.clips} ({r.cached} cached)",
            f"{r.seconds:.1f}s",
        )
    console.print(table)
    console.print(f"encoder: {enc}")
    if post:
        console.print(f"LinkedIn post text: {post}")
    if url := tracer.trace_url():
        console.print(f"trace → {url}")


def _copy_post(project: Project, run: str | None, out_dir: Path, platforms) -> str | None:
    """Put the approved plan's LinkedIn post next to the LinkedIn video."""
    if run is None or Platform.LINKEDIN not in platforms:
        return None
    src = project.runs_dir / run / "post.linkedin.txt"
    if not src.is_file():
        return None
    dst = out_dir / "linkedin.post.txt"
    dst.write_text(src.read_text())
    return str(dst)


def _open(path: Path) -> bool:
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    if shutil.which(opener) is None:
        return False
    subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)  # fmt: skip
    return True


@app.command(name="preview")
def preview_cmd(
    ctx: typer.Context,
    what: Annotated[Literal["proxy", "sheet", "final"], typer.Argument()] = "proxy",
    edl: EdlOpt = None,
    run_id: RunOpt = None,
    platform: Annotated[
        Platform | None, typer.Option(help="Default: first project platform.")
    ] = None,
    frames: Annotated[int, typer.Option(min=1, max=48, help="Frames in a contact sheet.")] = 12,
    no_open: Annotated[bool, typer.Option("--no-open", help="Print the path only.")] = False,
    encoder: EncoderOpt = None,
) -> None:
    """Open a fast proxy render, a contact sheet, or the final render."""
    project = _ctx(ctx).project()
    edl_path, run = _resolve_edl(project, edl, run_id)
    plat = platform or project.config.platforms[0]
    out_dir = _out_dir(project, edl_path, run, None)
    final = out_dir / f"{plat.value}.mp4"
    proxy_file = out_dir / f"{plat.value}.proxy.mp4"

    def render_proxy() -> None:
        _do_export(project, edl_path, run, [plat], out_dir, proxy=True, encoder=encoder,
                   allow_overflow=True, workers=2)  # fmt: skip

    if what == "final":
        if not final.is_file():
            raise _fail(f"{final} not rendered yet; run `ave export`", EXIT_USAGE)
        target = final
    elif what == "proxy":
        render_proxy()
        target = proxy_file
    else:
        video = final if final.is_file() else proxy_file
        if not video.is_file():
            render_proxy()
            video = proxy_file
        tools = Tools.from_config(project.config.ffmpeg)
        try:
            target = contact_sheet(video, video.with_suffix(".sheet.png"), n=frames, tools=tools)
        except MediaError as e:
            raise _fail(str(e)) from e
    print(target)
    if not no_open and not _open(target):
        err_console.print("no opener found (xdg-open); open the file above yourself")
