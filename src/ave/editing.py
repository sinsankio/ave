"""Editing phase: proxy render + QA, change requests, and the final gate.

Layout under runs/<id>/:
    edl.json                         current edit (approved plan, then chosen change options)
    edit/qa.json                     latest QA: deterministic issues, review sheets, proxies
    edit/critic.json                 QA critic agent's report (validated with `ave validate critic`)
    edit/proxy/<platform>.proxy.mp4  half-res renders reviewed in this phase
    edit/review/<platform>/*.png     labeled frames at banners, texts and zoom holds
    edit/changes/CR-NN.json          change requests and the user's decisions
    edit/changes/CR-NN/option-X.json frozen copy of each option's EDL
    edit/edl.rN.json                 previous edits, kept when an option replaces edl.json
    out/<platform>.mp4               finals, rendered at the final gate

Any departure from the approved plan goes through a change request: options are prepared,
the run waits at `awaiting_change_decision`, and only the user's choice is applied.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ave.config import Platform
from ave.edl import EDL, EDLError, check_against_media, load_edl, resolve_sources
from ave.media import MediaError, Tools, probe
from ave.observability import Tracer
from ave.pipeline import PipelineError, RunContext, _load
from ave.qa import Checker, review_points, review_sheets
from ave.runs import Status
from ave.schemas import ChangeRequest

# render(edl_path, platforms, out_dir, proxy) -> list of ExportResult
RenderFn = Callable[[Path, list[Platform], Path, bool], list]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def edit_dir(ctx: RunContext) -> Path:
    return ctx.dir / "edit"


def edl_file(ctx: RunContext) -> Path:
    return ctx.dir / "edl.json"


def edl_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _require(ctx: RunContext, *allowed: Status) -> None:
    if ctx.state.status not in allowed:
        want = " or ".join(s.value for s in allowed)
        raise PipelineError(f"run {ctx.state.id} is {ctx.state.status.value}; this needs {want}")


def _load_edl(path: Path) -> EDL:
    try:
        return load_edl(path)
    except EDLError as e:
        raise PipelineError(str(e)) from e


# --- QA ---------------------------------------------------------------------------------


def run_qa(
    ctx: RunContext, tracer: Tracer, render: RenderFn, platforms: list[Platform], log=lambda _: None
) -> dict:
    """Render proxies of the current edl.json, check them, and write edit/qa.json."""
    _require(ctx, Status.PLAN_APPROVED, Status.EDITING)
    if ctx.state.status is Status.PLAN_APPROVED:
        ctx.move(Status.EDITING)
    path = edl_file(ctx)
    if not path.is_file():
        raise PipelineError(f"run {ctx.state.id} has no edl.json; approve a plan first")
    edl = _load_edl(path)
    out = edit_dir(ctx)
    tools = Tools.from_config(ctx.project.config.ffmpeg)
    with tracer.span("phase:edit", kind="phase", inputs={"edl": edl_digest(path)}) as phase:
        log("rendering proxies")
        results = render(path, platforms, out / "proxy", True)
        sizes, ocr = {}, {}
        for name, src in resolve_sources(edl, path.parent).items():
            try:
                info = probe(src, tools)
            except MediaError as e:
                raise PipelineError(f"source {name!r}: {e}") from e
            sizes[name] = (info.width // 2 * 2, info.height // 2 * 2)
            f = ctx.analysis_dir / name / "ocr.json"
            if f.is_file():
                ocr[name] = json.loads(f.read_text())
        with tracer.span("tool:qa_checks", kind="tool") as sp:
            issues = Checker(
                edl, ctx.project.config.brand, sizes, ocr, ctx.project.config.max_duration
            ).run(platforms)
            sp.set_outputs(issues=len(issues), errors=sum(i.severity == "error" for i in issues))
        points = review_points(edl)
        sheets: dict[str, list[str]] = {}
        with tracer.span("tool:review_sheets", kind="tool", inputs={"points": len(points)}):
            for r in results:
                files = review_sheets(r.path, points, out / "review" / r.platform, tools)
                sheets[r.platform] = [str(f) for f in files]
        report = {
            "edl": edl_digest(path),
            "created_at": _now(),
            "duration": round(edl.duration, 3),
            "proxies": {r.platform: str(r.path) for r in results},
            "sheets": sheets,
            "points": [{"at": t, "label": label} for t, label in points],
            "issues": [i.to_dict() for i in issues],
            "has_ocr": bool(ocr),
        }
        (out / "qa.json").write_text(json.dumps(report, indent=2))
        phase.set_outputs(issues=len(issues), sheets=sum(len(v) for v in sheets.values()))
    return report


def latest_qa(ctx: RunContext) -> dict | None:
    f = edit_dir(ctx) / "qa.json"
    return json.loads(f.read_text()) if f.is_file() else None


def critic_issues(ctx: RunContext) -> list[dict]:
    """The critic's issues, with ids C01, C02... in file order."""
    f = edit_dir(ctx) / "critic.json"
    if not f.is_file():
        return []
    issues = json.loads(f.read_text()).get("issues", [])
    return [{"id": f"C{k:02d}"} | i for k, i in enumerate(issues, 1)]


def issue_key(issue: dict) -> str:
    """Stable identity of an issue across QA runs (ids are renumbered every run)."""
    return f"{issue['kind']}|{issue.get('segment') or ''}|{issue.get('platform') or ''}"


def open_issues(ctx: RunContext) -> list[dict]:
    """Warnings and errors not yet put to the user in a decided change request."""
    qa = latest_qa(ctx) or {"issues": []}
    decided = {
        k for c in list_changes(ctx) if c["status"] == "decided" for k in c.get("issue_keys", [])
    }
    return [
        i
        for i in [*qa["issues"], *critic_issues(ctx)]
        if i["severity"] in ("warn", "error") and issue_key(i) not in decided
    ]


# --- change requests ----------------------------------------------------------------------


def _changes_dir(ctx: RunContext) -> Path:
    return edit_dir(ctx) / "changes"


def list_changes(ctx: RunContext) -> list[dict]:
    d = _changes_dir(ctx)
    if not d.is_dir():
        return []
    return [json.loads(f.read_text()) for f in sorted(d.glob("CR-*.json"))]


def get_change(ctx: RunContext, cr_id: str) -> dict:
    f = _changes_dir(ctx) / f"{cr_id}.json"
    if not f.is_file():
        raise PipelineError(f"no change request {cr_id} in run {ctx.state.id}")
    return json.loads(f.read_text())


def _check_option_edl(ctx: RunContext, path: Path) -> list[str]:
    try:
        edl = load_edl(path)
    except EDLError as e:
        return [str(e)]
    problems = []
    cap = ctx.project.config.max_duration
    if edl.duration > cap + 1e-6:
        problems.append(f"{path.name}: {edl.duration:.1f}s is over the {cap:g}s cap")
    tools = Tools.from_config(ctx.project.config.ffmpeg)
    media = {}
    for name, src in resolve_sources(edl, path.parent).items():
        try:
            media[name] = probe(src, tools)
        except MediaError as e:
            problems.append(f"{path.name}: source {name!r}: {e}")
    if len(media) == len(edl.sources):
        problems += [f"{path.name}: {p}" for p in check_against_media(edl, media)]
    return problems


def new_change(ctx: RunContext, file: Path, tracer: Tracer) -> str:
    """Register a change request; the run then waits for the user's decision."""
    _require(ctx, Status.EDITING)
    req: ChangeRequest = _load("change", file)  # type: ignore[assignment]
    if any(c["status"] == "open" for c in list_changes(ctx)):
        raise PipelineError("another change request is still open; decide it first")
    problems = []
    for opt in req.options:
        if opt.edl is None:
            continue
        p = Path(opt.edl)
        p = p if p.is_absolute() else ctx.dir / p
        if not p.is_file():
            problems.append(f"option {opt.id}: no such EDL {opt.edl}")
            continue
        problems += [f"option {opt.id}: {x}" for x in _check_option_edl(ctx, p)]
    known = {
        i["id"]: i for i in [*(latest_qa(ctx) or {"issues": []})["issues"], *critic_issues(ctx)]
    }
    problems += [
        f"unknown issue id {i!r} (see `ave qa show`)" for i in req.issues if i not in known
    ]
    if problems:
        raise PipelineError("change request rejected:\n- " + "\n- ".join(problems))
    d = _changes_dir(ctx)
    d.mkdir(parents=True, exist_ok=True)
    cr_id = f"CR-{len(list(d.glob('CR-*.json'))) + 1:02d}"
    frozen = d / cr_id
    frozen.mkdir()
    data = req.model_dump(mode="json")
    for opt in data["options"]:
        if opt["edl"]:
            src = Path(opt["edl"])
            src = src if src.is_absolute() else ctx.dir / src
            dst = frozen / f"option-{opt['id']}.json"
            edl = load_edl(src)
            # freeze with absolute sources so the copy resolves wherever it ends up
            frozen_edl = edl.model_copy(update={"sources": resolve_sources(edl, src.parent)})
            dst.write_text(frozen_edl.model_dump_json(indent=2, exclude_none=True))
            opt["edl"] = str(dst.relative_to(ctx.dir))
    record = {
        "id": cr_id,
        "status": "open",
        "created_at": _now(),
        "request": data,
        "issue_keys": sorted({issue_key(known[i]) for i in req.issues}),
        "decision": None,
    }
    (d / f"{cr_id}.json").write_text(json.dumps(record, indent=2))
    ctx.move(Status.AWAITING_CHANGE_DECISION, f"{cr_id}: {req.title}")
    with tracer.span(f"gate:change_request[{cr_id}]", kind="gate", inputs=data) as sp:
        sp.set_outputs(options=[o["id"] for o in data["options"]], recommended=req.recommended)
    return cr_id


def choose_change(
    ctx: RunContext,
    cr_id: str,
    option: str | None,
    note: str | None,
    tracer: Tracer,
    other: str | None = None,
) -> dict:
    """Apply the user's choice: the option's EDL replaces edl.json (old one archived)."""
    _require(ctx, Status.AWAITING_CHANGE_DECISION)
    record = get_change(ctx, cr_id)
    if record["status"] != "open":
        raise PipelineError(f"{cr_id} was already decided")
    opts = {o["id"]: o for o in record["request"]["options"]}
    if other is None and option not in opts:
        raise PipelineError(f"{cr_id} has no option {option!r} (choose {', '.join(opts)})")
    applied = None
    if other is None and opts[option]["edl"]:
        current = edl_file(ctx)
        rev = len(list(edit_dir(ctx).glob("edl.r*.json"))) + 1
        shutil.copy(current, edit_dir(ctx) / f"edl.r{rev}.json")
        shutil.copy(ctx.dir / opts[option]["edl"], current)
        applied = opts[option]["edl"]
    record["status"] = "decided"
    record["decision"] = {
        "option": option if other is None else None,
        "other": other,
        "note": note,
        "at": _now(),
        "applied_edl": applied,
    }
    (_changes_dir(ctx) / f"{cr_id}.json").write_text(json.dumps(record, indent=2))
    ctx.move(Status.EDITING, f"{cr_id}: {other or option}")
    tracer.feedback(
        f"change_request:{cr_id}",
        value=other or option,
        score=1 if option == record["request"]["recommended"] else 0,
        comment=note,
    )
    return record


# --- gates ----------------------------------------------------------------------------------


def qa_pass(ctx: RunContext, tracer: Tracer, accept_risk: str | None = None) -> None:
    """Editing is done: the run waits for the user's final approval of the proxy."""
    _require(ctx, Status.EDITING)
    if any(c["status"] == "open" for c in list_changes(ctx)):
        raise PipelineError("a change request is still open")
    qa = latest_qa(ctx)
    if qa is None or qa["edl"] != edl_digest(edl_file(ctx)):
        raise PipelineError("the edit changed since the last QA; run `ave edit` again")
    blockers = open_issues(ctx)
    if blockers and not accept_risk:
        ids = ", ".join(i["id"] for i in blockers)
        raise PipelineError(
            f"{len(blockers)} warning/error issue(s) not decided by the user ({ids}): "
            "put them in a change request, or pass --accept-risk 'why'"
        )
    ctx.move(Status.AWAITING_FINAL_APPROVAL, accept_risk)
    with tracer.span("gate:qa_pass", kind="gate", inputs={"accepted": len(blockers)}) as sp:
        sp.set_outputs(reason=accept_risk)


def final_approve(
    ctx: RunContext,
    tracer: Tracer,
    render: RenderFn,
    platforms: list[Platform],
    note: str | None = None,
) -> list:
    """Gate 3: render the finals for every platform and finish the run."""
    _require(ctx, Status.AWAITING_FINAL_APPROVAL)
    ctx.move(Status.EXPORTING, note)
    tracer.feedback("gate:final_approval", score=1, comment=note)
    try:
        results = render(edl_file(ctx), platforms, ctx.dir / "out", False)
    except Exception as e:
        ctx.move(Status.FAILED, f"export: {e}")
        raise
    post = ctx.dir / "post.linkedin.txt"
    if post.is_file() and Platform.LINKEDIN in platforms:
        (ctx.dir / "out" / "linkedin.post.txt").write_text(post.read_text())
    ctx.move(Status.DONE)
    return results


def final_reject(ctx: RunContext, tracer: Tracer, feedback: str) -> None:
    _require(ctx, Status.AWAITING_FINAL_APPROVAL)
    if not feedback.strip():
        raise PipelineError("say what should change (-m '...')")
    ctx.move(Status.EDITING, feedback)
    edit_dir(ctx).mkdir(exist_ok=True)
    with (edit_dir(ctx) / "final-feedback.md").open("a") as f:
        f.write(f"## {_now()}\n\n{feedback.strip()}\n\n")
    tracer.feedback("gate:final_approval", score=0, comment=feedback)
