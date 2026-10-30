"""Run-level pipeline steps: analysis, agent-output validation, plan versions and Gate 1.

Layout of runs/<id>/:
    state.json, trace.jsonl
    analysis/manifest.json, analysis/docs.json, analysis/<video>/{signals,keyframes,ocr}.json,
        analysis/<video>/frames/*.jpg, analysis/<video>/sheets/*.png
    catalog.json, vision/<chunk>.json, alignment.json, evidence.json   (agents + `ave align`)
    plan/plan.v<N>.json, plan/feedback.md, edl.json (approved plan's EDL)
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from ave.analysis import align, docs, keyframes, ocr, signals
from ave.config import Project
from ave.edl import check_against_media, resolve_sources
from ave.media import MediaError, MediaInfo, Tools, probe
from ave.observability import Tracer
from ave.runs import RunError, RunState, RunStore, Status
from ave.schemas import (
    CAPTION_MAX_WORDS,
    KINDS,
    TITLE_MAX_CHARS,
    Alignment,
    Catalog,
    Copy,
    Plan,
    VisionReport,
)

SHEETS_PER_CHUNK = 2


class PipelineError(Exception):
    """A pipeline step cannot run (missing inputs, invalid agent output, wrong run state)."""


def video_name(path: Path, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-") or "video"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


@dataclass
class RunContext:
    project: Project
    store: RunStore
    state: RunState

    @property
    def dir(self) -> Path:
        return self.store.run_dir(self.state.id)

    @property
    def analysis_dir(self) -> Path:
        return self.dir / "analysis"

    @property
    def plan_dir(self) -> Path:
        return self.dir / "plan"

    def manifest(self) -> dict:
        f = self.analysis_dir / "manifest.json"
        if not f.is_file():
            raise PipelineError(f"run {self.state.id} has no analysis yet; run `ave analyze`")
        return json.loads(f.read_text())

    def move(self, dst: Status, note: str | None = None) -> None:
        self.state.move(dst, note)
        self.store.save(self.state)

    def plan_versions(self) -> list[int]:
        if not self.plan_dir.is_dir():
            return []
        vs = [
            int(m.group(1))
            for p in self.plan_dir.glob("plan.v*.json")
            if (m := re.fullmatch(r"plan\.v(\d+)\.json", p.name))
        ]
        return sorted(vs)

    def plan_file(self, version: int | None = None) -> Path:
        versions = self.plan_versions()
        if not versions:
            raise PipelineError(f"run {self.state.id} has no plan yet")
        v = version if version is not None else versions[-1]
        if v not in versions:
            raise PipelineError(f"run {self.state.id} has no plan v{v} (have {versions})")
        return self.plan_dir / f"plan.v{v}.json"


def open_run(project: Project, run_id: str | None, create: bool = False) -> RunContext:
    store = RunStore(project.runs_dir)
    try:
        state = store.get(run_id) if run_id else store.latest()
    except RunError as e:
        raise PipelineError(str(e)) from e
    if state is None:
        if not create:
            raise PipelineError("no runs yet; `ave runs new` or `ave analyze` creates one")
        state = store.create()
    return RunContext(project, store, state)


# --- analysis ------------------------------------------------------------------------


def run_analysis(
    ctx: RunContext, tracer: Tracer, *, use_ocr: bool = True, log=lambda _: None
) -> dict:
    project = ctx.project
    videos = [project.resolve(v) for v in project.config.videos]
    if not videos:
        raise PipelineError("project.yaml lists no videos (`ave config set videos '[demo.mp4]'`)")
    if use_ocr and not ocr.available():
        raise PipelineError("OCR needs the `ocr` extra: `uv sync --extra ocr` (or pass --no-ocr)")
    if ctx.state.status is not Status.ANALYZING:
        ctx.move(Status.ANALYZING)
    tools = Tools.from_config(project.config.ffmpeg)
    out = ctx.analysis_dir
    if out.exists():
        shutil.rmtree(out)  # analysis is derived data; re-running replaces it
    out.mkdir(parents=True)
    try:
        with tracer.span(
            "phase:analyze", kind="phase", inputs={"videos": [str(v) for v in videos]}
        ) as phase:
            manifest = _analyze(ctx, tracer, tools, videos, use_ocr, log)
            phase.set_outputs(
                videos=len(manifest["videos"]),
                chunks=len(manifest["chunks"]),
                doc_sections=manifest["docs"]["sections"],
            )
    except Exception as e:
        ctx.move(Status.FAILED, f"analyze: {e}")
        raise
    ctx.move(Status.ANALYZED)
    return manifest


def _analyze(ctx, tracer, tools, videos, use_ocr, log) -> dict:
    out = ctx.analysis_dir
    manifest: dict[str, Any] = {
        "run": ctx.state.id,
        "created_at": datetime.now(UTC).isoformat(),
        "project": ctx.project.config.name,
        "videos": [],
        "chunks": [],
        "ocr": use_ocr,
    }
    engine = ocr.OCR() if use_ocr else None
    taken: set[str] = set()
    for path in videos:
        name = video_name(path, taken)
        taken.add(name)
        vdir = out / name
        vdir.mkdir()
        with tracer.span(f"tool:probe[{name}]", kind="tool") as sp:
            try:
                info = probe(path, tools)
            except MediaError as e:
                raise PipelineError(f"video {path}: {e}") from e
            sp.set_outputs(**info.to_dict())
        log(f"{name}: {info.duration:.1f}s {info.width}x{info.height}, extracting signals")
        with tracer.span(f"tool:signals[{name}]", kind="tool") as sp:
            sig = signals.extract_signals(info, tools)
            (vdir / "signals.json").write_text(json.dumps(sig.to_dict()))
            sp.set_outputs(scenes=len(sig.scenes), idle=len(sig.idle))
        with tracer.span(f"tool:keyframes[{name}]", kind="tool") as sp:
            times = keyframes.select_times(sig)
            frames = keyframes.extract_frames(info, times, vdir / "frames", tools)
            sheets = keyframes.build_sheets(frames, vdir, vdir / "sheets")
            sp.set_outputs(keyframes=len(frames), sheets=len(sheets))
        log(f"{name}: {len(frames)} keyframes on {len(sheets)} sheets")
        ocr_count = 0
        if engine is not None:
            with tracer.span(f"tool:ocr[{name}]", kind="tool") as sp:
                texts = ocr.read_keyframes(frames, vdir, engine)
                (vdir / "ocr.json").write_text(json.dumps(ocr.to_json(texts)))
                ocr_count = sum(len(t.items) for t in texts)
                sp.set_outputs(
                    frames=len(texts), reused=sum(1 for t in texts if t.reused_from), hits=ocr_count
                )
            log(f"{name}: OCR found {ocr_count} text items")
        (vdir / "keyframes.json").write_text(json.dumps([k.to_dict() for k in frames]))
        manifest["videos"].append(
            {
                "name": name,
                "path": str(path),
                "duration": info.duration,
                "width": info.width,
                "height": info.height,
                "fps": info.fps,
                "has_audio": info.has_audio,
                "keyframes": len(frames),
                "sheets": len(sheets),
                "scenes": len(sig.scenes),
                "idle_seconds": round(sum(b - a for a, b in sig.idle), 2),
                "dir": name,
            }
        )
        for c, start in enumerate(range(0, len(sheets), SHEETS_PER_CHUNK), 1):
            group = sheets[start : start + SHEETS_PER_CHUNK]
            nxt = (
                sheets[start + SHEETS_PER_CHUNK]["start"]
                if start + SHEETS_PER_CHUNK < len(sheets)
                else info.duration
            )
            manifest["chunks"].append(
                {
                    "id": f"{name}-{c:02d}",
                    "video": name,
                    "start": 0.0 if c == 1 else group[0]["start"],
                    "end": round(nxt, 3),
                    "sheets": [f"{name}/{g['file']}" for g in group],
                }
            )

    with tracer.span("tool:docs", kind="tool") as sp:
        files = ctx.project.doc_files()
        sections = [s.to_dict() for f in files for s in docs.split_markdown(f, ctx.project.root)]
        (out / "docs.json").write_text(json.dumps(sections, ensure_ascii=False, indent=1))
        sp.set_outputs(files=len(files), sections=len(sections))
    manifest["docs"] = {"files": [str(f) for f in files], "sections": len(sections)}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


# --- validation ----------------------------------------------------------------------


def _load(kind: str, path: Path) -> BaseModel:
    if not path.is_file():
        raise PipelineError(f"no such file: {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise PipelineError(f"{path}: invalid JSON: {e}") from e
    try:
        return KINDS[kind].model_validate(data)
    except ValidationError as e:
        raise PipelineError(f"{path}: {e}") from e


def _catalog(ctx: RunContext) -> Catalog | None:
    f = ctx.dir / "catalog.json"
    return _load("catalog", f) if f.is_file() else None  # type: ignore[return-value]


def _durations(ctx: RunContext) -> dict[str, float]:
    return {v["name"]: v["duration"] for v in ctx.manifest()["videos"]}


def validate(ctx: RunContext, kind: str, path: Path) -> tuple[BaseModel, list[str]]:
    """Schema check plus cross-file rules. Returns the model and a list of problems."""
    if kind not in KINDS:
        raise PipelineError(f"unknown kind {kind!r}; one of {', '.join(KINDS)}")
    model = _load(kind, path)
    problems: list[str] = []
    if kind == "catalog":
        problems += _check_catalog(ctx, model)  # type: ignore[arg-type]
    elif kind == "vision":
        problems += _check_vision(ctx, model)  # type: ignore[arg-type]
    elif kind == "alignment":
        problems += _check_alignment(ctx, model)  # type: ignore[arg-type]
    elif kind == "plan":
        problems += _check_plan(ctx, model)  # type: ignore[arg-type]
    elif kind == "copy":
        problems += _check_copy(ctx, model, _draft_plan(ctx))  # type: ignore[arg-type]
    return model, problems


def _check_catalog(ctx: RunContext, cat: Catalog) -> list[str]:
    docs_file = ctx.analysis_dir / "docs.json"
    known = {s["id"] for s in json.loads(docs_file.read_text())} if docs_file.is_file() else set()
    known |= {f"recording:{v['name']}" for v in ctx.manifest()["videos"]}
    return [
        f"feature {f.id}: unknown doc section {s!r}"
        for f in cat.features
        for s in f.sources
        if s not in known
    ]


def _check_vision(ctx: RunContext, rep: VisionReport) -> list[str]:
    problems = []
    durations = _durations(ctx)
    chunks = {c["id"]: c for c in ctx.manifest()["chunks"]}
    if rep.video not in durations:
        problems.append(f"unknown video {rep.video!r}")
    if rep.chunk not in chunks:
        problems.append(f"unknown chunk {rep.chunk!r}")
    cat = _catalog(ctx)
    ids = {f.id for f in cat.features} if cat else None
    for s in rep.segments:
        if rep.video in durations and s.end > durations[rep.video] + 0.5:
            problems.append(f"segment {s.start}-{s.end} runs past the video end")
        if s.feature_id and ids is not None and s.feature_id not in ids:
            problems.append(f"segment {s.start}-{s.end}: unknown feature {s.feature_id!r}")
    return problems


def _check_alignment(ctx: RunContext, al: Alignment) -> list[str]:
    problems = []
    cat = _catalog(ctx)
    if cat is None:
        return ["catalog.json is missing; alignment needs it"]
    ids = [f.id for f in cat.features]
    seen = [c.feature_id for c in al.features]
    for fid in ids:
        if seen.count(fid) != 1:
            problems.append(f"feature {fid!r} must appear exactly once (found {seen.count(fid)})")
    problems += [f"unknown feature {fid!r}" for fid in seen if fid not in ids]
    durations = _durations(ctx)
    ranges = [(c.feature_id, r) for c in al.features for r in c.ranges]
    ranges += [("video_only", v) for v in al.video_only]
    for owner, r in ranges:
        if r.video not in durations:
            problems.append(f"{owner}: unknown video {r.video!r}")
        elif r.end <= r.start or r.end > durations[r.video] + 0.5:
            problems.append(f"{owner}: range {r.start}-{r.end} is invalid for {r.video}")
    return problems


def _check_plan(ctx: RunContext, plan: Plan) -> list[str]:
    problems = []
    cap = ctx.project.config.max_duration
    if plan.edl.duration > cap + 1e-6:
        problems.append(f"edit is {plan.edl.duration:.1f}s, over the {cap:g}s cap")
    if plan.target_duration > cap:
        problems.append(f"target_duration {plan.target_duration:g}s is over the {cap:g}s cap")
    cat = _catalog(ctx)
    ids = {f.id for f in cat.features} if cat else set()
    for s in plan.edl.segments:
        if s.feature and s.feature not in ids:
            problems.append(f"segment {s.id}: unknown feature {s.feature!r}")
    sources = resolve_sources(plan.edl, ctx.dir)
    media: dict[str, MediaInfo] = {}
    tools = Tools.from_config(ctx.project.config.ffmpeg)
    for name, path in sources.items():
        try:
            media[name] = probe(path, tools)
        except MediaError as e:
            problems.append(f"source {name!r}: {e}")
    if len(media) == len(sources):
        problems += check_against_media(plan.edl, media)
    return problems


# --- copy ------------------------------------------------------------------------------

SOURCE_PREFIXES = ("ocr:", "vision:", "plan:", "recording:", "catalog:")


def _draft_plan(ctx: RunContext) -> Plan | None:
    draft = ctx.plan_dir / "draft.json"
    if draft.is_file():
        return _load("plan", draft)  # type: ignore[return-value]
    return _load("plan", ctx.plan_file()) if ctx.plan_versions() else None  # type: ignore[return-value]


def _check_copy(ctx: RunContext, copy: Copy, plan: Plan | None) -> list[str]:
    problems: list[str] = []
    if plan is None:
        return ["no plan to write copy for: the pacing editor writes plan/draft.json first"]
    segs = {s.id: s for s in plan.edl.segments}
    cat = _catalog(ctx)
    feature_ids = {f.id for f in cat.features} if cat else set()

    def check_sources(owner: str, sources: list[str]) -> None:
        for src in sources:
            if src.startswith(SOURCE_PREFIXES):
                continue
            if ":" in src:
                problems.append(f"{owner}: unknown source kind {src!r}")
            elif src not in feature_ids:
                problems.append(f"{owner}: unknown catalog feature {src!r}")

    def no_placeholder(owner: str, text: str | None) -> None:
        if text and "[[" in text:
            problems.append(f"{owner}: unresolved placeholder; leave it out and explain in notes")

    for t in copy.texts:
        owner = f"{t.segment}/{t.style}"
        if t.segment not in segs:
            problems.append(f"{owner}: no segment {t.segment!r} in the plan")
        if t.style == "title" and len(t.text) > TITLE_MAX_CHARS:
            problems.append(f"{owner}: title is {len(t.text)} chars (max {TITLE_MAX_CHARS})")
        if t.style == "caption" and len(t.text.split()) > CAPTION_MAX_WORDS:
            problems.append(
                f"{owner}: caption has {len(t.text.split())} words (max {CAPTION_MAX_WORDS})"
            )
        check_sources(owner, t.sources)
        no_placeholder(owner, t.text)
    durations = _durations(ctx)
    first_video = plan.edl.segments[0].source
    for name, b in (("start_banner", copy.start_banner), ("end_banner", copy.end_banner)):
        check_sources(name, b.sources)
        for text in (b.title, b.subtitle, b.cta, *b.lines):
            no_placeholder(name, text)
        if (
            b.hero_at is not None
            and first_video in durations
            and b.hero_at >= durations[first_video]
        ):
            problems.append(f"{name}: hero_at {b.hero_at}s is past the end of the video")
    no_placeholder("post", copy.post)
    if copy.post and not ctx.project.config.writing.linkedin_post:
        problems.append("post: writing.linkedin_post is off for this project; leave post empty")
    return problems


def apply_copy(ctx: RunContext, copy_path: Path, plan_path: Path) -> Plan:
    """Write the copy into the plan: texts per segment (keeping timings), banners, post, notes."""
    copy, problems = validate(ctx, "copy", copy_path)
    if problems:
        raise PipelineError("copy rejected by validation:\n- " + "\n- ".join(problems))
    plan: Plan = _load("plan", plan_path)  # type: ignore[assignment]
    data = plan.model_dump(mode="json", exclude_none=True)
    by_seg: dict[str, dict[str, str]] = {}
    for t in copy.texts:  # type: ignore[attr-defined]
        by_seg.setdefault(t.segment, {})[t.style] = t.text
    for seg in data["edl"]["segments"]:
        wanted = by_seg.get(seg["id"], {})
        old = {t.get("style", "caption"): t for t in seg.get("texts", [])}
        speed = seg.get("speed", 1.0)
        texts = []
        for style in ("title", "caption"):
            if style not in wanted:
                continue
            if style in old:
                t = dict(old[style]) | {"text": wanted[style]}
            else:
                a, b = seg["start"], seg["end"]
                if style == "caption" and b - a > 1.2 * speed:
                    a, b = a + 0.4 * speed, b - 0.3 * speed
                t = {
                    "text": wanted[style],
                    "start": round(a, 3),
                    "end": round(b, 3),
                    "style": style,
                }
            texts.append(t)
        seg["texts"] = texts
    source = data["edl"]["segments"][0]["source"]
    for key in ("start_banner", "end_banner"):
        bc = getattr(copy, key)
        banner = dict(data["edl"].get(key) or {"duration": 3.0 if key == "start_banner" else 3.5})
        banner |= {"title": bc.title, "lines": bc.lines}
        for field in ("subtitle", "cta"):
            if getattr(bc, field):
                banner[field] = getattr(bc, field)
            else:
                banner.pop(field, None)
        if bc.hero_at is not None:
            banner["hero"] = {"source": source, "at": bc.hero_at}
        data["edl"][key] = banner
    data["post"] = copy.post if ctx.project.config.writing.linkedin_post else None  # type: ignore[attr-defined]
    data["copy_notes"] = list(copy.notes)  # type: ignore[attr-defined]
    new_plan = Plan.model_validate(data)
    plan_path.write_text(new_plan.model_dump_json(indent=2, exclude_none=True))
    return new_plan


# --- evidence ------------------------------------------------------------------------


def build_evidence(ctx: RunContext) -> list[dict]:
    cat = _catalog(ctx)
    if cat is None:
        raise PipelineError("catalog.json is missing; the doc analyst writes it first")
    ocr_data = {}
    for v in ctx.manifest()["videos"]:
        f = ctx.analysis_dir / v["dir"] / "ocr.json"
        ocr_data[v["name"]] = json.loads(f.read_text()) if f.is_file() else []
    ev = align.to_json(align.cue_evidence(cat, ocr_data))
    (ctx.dir / "evidence.json").write_text(json.dumps(ev, indent=1))
    return ev


# --- plan + Gate 1 -------------------------------------------------------------------


def submit_plan(ctx: RunContext, path: Path, tracer: Tracer) -> int:
    plan, problems = validate(ctx, "plan", path)
    if problems:
        raise PipelineError("plan rejected by validation:\n- " + "\n- ".join(problems))
    st = ctx.state.status
    if st in (
        Status.ANALYZED,
        Status.AWAITING_CHANGE_DECISION,
        Status.AWAITING_FINAL_APPROVAL,
        Status.DONE,
    ):
        ctx.move(Status.PLANNING)
    elif st is not Status.PLANNING:
        raise PipelineError(f"run {ctx.state.id} is {st.value}; a plan can't be submitted now")
    ctx.move(Status.AWAITING_PLAN_APPROVAL)
    v = ctx.state.plan_version
    ctx.plan_dir.mkdir(exist_ok=True)
    (ctx.plan_dir / f"plan.v{v}.json").write_text(plan.model_dump_json(indent=2, exclude_none=True))
    with tracer.span("gate:plan_submitted", kind="gate", inputs={"version": v}) as sp:
        sp.set_outputs(duration=round(plan.edl.duration, 2), segments=len(plan.edl.segments))  # type: ignore[attr-defined]
    return v


def approve_plan(ctx: RunContext, tracer: Tracer, comment: str | None = None) -> Path:
    if ctx.state.status is not Status.AWAITING_PLAN_APPROVAL:
        raise PipelineError(
            f"run {ctx.state.id} is {ctx.state.status.value}, not awaiting plan approval"
        )
    plan: Plan = _load("plan", ctx.plan_file())  # type: ignore[assignment]
    ctx.move(Status.PLAN_APPROVED, comment)
    edl_file = ctx.dir / "edl.json"
    edl_file.write_text(plan.edl.model_dump_json(indent=2, exclude_none=True))
    post_file = ctx.dir / "post.linkedin.txt"
    if plan.post:
        post_file.write_text(plan.post.rstrip() + "\n")
    else:
        post_file.unlink(missing_ok=True)
    tracer.feedback(
        "gate:plan_approval", score=1, value=f"v{ctx.state.plan_version}", comment=comment
    )
    return edl_file


def reject_plan(ctx: RunContext, tracer: Tracer, feedback: str) -> None:
    if ctx.state.status is not Status.AWAITING_PLAN_APPROVAL:
        raise PipelineError(
            f"run {ctx.state.id} is {ctx.state.status.value}, not awaiting plan approval"
        )
    if not feedback.strip():
        raise PipelineError("say what should change (-m '...')")
    ctx.move(Status.PLANNING, feedback)
    ctx.plan_dir.mkdir(exist_ok=True)
    with (ctx.plan_dir / "feedback.md").open("a") as f:
        f.write(
            f"## v{ctx.state.plan_version} ({datetime.now(UTC):%Y-%m-%d %H:%M} UTC)\n\n"
            f"{feedback.strip()}\n\n"
        )
    tracer.feedback(
        "gate:plan_approval", score=0, value=f"v{ctx.state.plan_version}", comment=feedback
    )
