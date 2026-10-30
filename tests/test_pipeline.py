"""Run pipeline through Gate 1 via the CLI, the way the Claude Code agents drive it."""

import json
import shutil
from pathlib import Path

import pytest

from ave.observability import read_trace
from tests import synth
from tests.conftest import parse_json

pytestmark = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]

CATALOG = {
    "project_name": "Smart Report",
    "overview": "Smart Report turns your sales data into a shareable PDF report in one click.",
    "features": [
        {
            "id": "sign-in",
            "name": "Sign in",
            "summary": "s",
            "behavior": "b",
            "ui_cues": ["Sign in"],
            "emphasis": 2,
            "sources": ["readme#sign-in"],
        },
        {
            "id": "export-pdf",
            "name": "Export to PDF",
            "summary": "s",
            "behavior": "b",
            "ui_cues": ["Export PDF"],
            "emphasis": 5,
            "sources": ["readme#export-to-pdf"],
        },
    ],
}


@pytest.fixture(scope="module")
def demo_files(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("demo")
    synth.make_project_files(d)
    return d


@pytest.fixture
def proj(tmp_path: Path, demo_files: Path, invoke) -> Path:
    shutil.copy(demo_files / "demo.mp4", tmp_path / "demo.mp4")
    shutil.copytree(demo_files / "docs", tmp_path / "docs")
    res = invoke(
        "-p",
        str(tmp_path),
        "init",
        "--yes",
        "--name",
        "Smart Report",
        "--video",
        "demo.mp4",
        "--docs",
        "docs/*.md",
        "--platform",
        "linkedin",
        "--max-duration",
        "30",
    )
    assert res.exit_code == 0, res.output
    res = invoke("-p", str(tmp_path), "analyze", "--no-ocr", "--json")
    assert res.exit_code == 0, res.output
    return tmp_path


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def plan_data(video: Path, **edl_kw) -> dict:
    edl = {
        "sources": {"demo": str(video)},
        "start_banner": {"title": "Smart Report", "duration": 2},
        "segments": [
            {
                "id": "export",
                "source": "demo",
                "start": 9.5,
                "end": 14,
                "feature": "export-pdf",
                "note": "payoff",
            }
        ],
    } | edl_kw
    return {"summary": "Export moment only.", "target_duration": 8, "edl": edl}


def test_analyze_outputs(proj: Path, invoke):
    ctx = parse_json(invoke("-p", str(proj), "context").stdout)
    assert ctx["status"] == "analyzed"
    run = Path(ctx["paths"]["run_dir"])
    manifest = json.loads((run / "analysis" / "manifest.json").read_text())
    (video,) = manifest["videos"]
    assert video["name"] == "demo" and video["keyframes"] >= 5
    assert manifest["chunks"][0]["id"] == "demo-01" and manifest["ocr"] is False
    sections = json.loads((run / "analysis" / "docs.json").read_text())
    assert "readme#export-to-pdf" in {s["id"] for s in sections}
    names = [r["name"] for r in read_trace(run / "trace.jsonl")]
    assert "phase:analyze" in names and "tool:signals[demo]" in names


def test_catalog_can_cite_the_recording(proj: Path, invoke):
    p = str(proj)
    run = Path(parse_json(invoke("-p", p, "context").stdout)["paths"]["run_dir"])
    feature = CATALOG["features"][0]
    cat = write(
        run / "catalog.json", CATALOG | {"features": [feature | {"sources": ["recording:demo"]}]}
    )
    assert parse_json(invoke("-p", p, "validate", "catalog", str(cat), "--json").stdout)["ok"]

    write(cat, CATALOG | {"features": [feature | {"sources": ["recording:other"]}]})
    out = parse_json(invoke("-p", p, "validate", "catalog", str(cat), "--json").stdout)
    assert out["ok"] is False and "unknown doc section" in out["problems"][0]


def test_coverage_marks_recording_only_features(proj: Path, invoke):
    p = str(proj)
    run = Path(parse_json(invoke("-p", p, "context").stdout)["paths"]["run_dir"])
    docs_feat, rec_feat = CATALOG["features"]
    write(
        run / "catalog.json",
        CATALOG | {"features": [docs_feat, rec_feat | {"sources": ["recording:demo"]}]},
    )
    write(
        run / "alignment.json",
        {
            "features": [
                {
                    "feature_id": "sign-in",
                    "status": "both",
                    "confidence": 0.9,
                    "ranges": [{"video": "demo", "start": 0, "end": 4}],
                },
                {
                    "feature_id": "export-pdf",
                    "status": "both",
                    "confidence": 0.9,
                    "ranges": [{"video": "demo", "start": 10, "end": 16}],
                },
            ]
        },
    )
    rows = {
        line.split("│")[1].strip(): line.split("│")[2].strip()
        for line in invoke("-p", p, "coverage").stdout.splitlines()
        if line.count("│") > 3
    }
    assert rows["Sign in"] == "✓" and rows["Export to PDF"] == "—"


def test_validation_catches_cross_file_problems(proj: Path, invoke):
    p = str(proj)
    run = Path(parse_json(invoke("-p", p, "context").stdout)["paths"]["run_dir"])
    bad_cat = write(
        run / "catalog.json",
        CATALOG | {"features": [CATALOG["features"][0] | {"sources": ["nope"]}]},
    )
    out = parse_json(invoke("-p", p, "validate", "catalog", str(bad_cat), "--json").stdout)
    assert out["ok"] is False and "unknown doc section" in out["problems"][0]

    write(run / "catalog.json", CATALOG)
    vision = write(
        run / "vision" / "demo-01.json",
        {
            "chunk": "demo-01",
            "video": "demo",
            "segments": [
                {"start": 0, "end": 4, "description": "x", "feature_id": "ghost", "importance": 2},
                {"start": 4, "end": 40, "description": "x", "importance": 1},
            ],
        },
    )
    problems = parse_json(invoke("-p", p, "validate", "vision", str(vision), "--json").stdout)[
        "problems"
    ]
    assert any("ghost" in x for x in problems) and any("past the video end" in x for x in problems)

    al = write(
        run / "alignment.json",
        {
            "features": [
                {
                    "feature_id": "sign-in",
                    "status": "both",
                    "ranges": [{"video": "demo", "start": 0, "end": 4}],
                    "confidence": 0.9,
                }
            ]
        },
    )
    problems = parse_json(invoke("-p", p, "validate", "alignment", str(al), "--json").stdout)[
        "problems"
    ]
    assert any("export-pdf" in x and "exactly once" in x for x in problems)

    schema_err = write(
        run / "x.json",
        {
            "features": [
                {
                    "feature_id": "a",
                    "status": "docs_only",
                    "ranges": [{"video": "demo", "start": 0, "end": 1}],
                    "confidence": 1,
                }
            ]
        },
    )
    res = invoke("-p", p, "validate", "alignment", str(schema_err), "--json")
    assert res.exit_code == 1 and "docs_only" in res.stdout

    over = write(
        run / "plan" / "over.json",
        plan_data(
            proj / "demo.mp4",
            segments=[
                {
                    "id": "all",
                    "source": "demo",
                    "start": 0,
                    "end": 16,
                    "speed": 0.5,
                    "feature": "nope",
                }
            ],
        ),
    )
    problems = parse_json(invoke("-p", p, "validate", "plan", str(over), "--json").stdout)[
        "problems"
    ]
    assert any("cap" in x for x in problems) and any("unknown feature" in x for x in problems)


def test_gate_one_flow(proj: Path, invoke):
    p = str(proj)
    run = Path(parse_json(invoke("-p", p, "context").stdout)["paths"]["run_dir"])
    write(run / "catalog.json", CATALOG)
    assert invoke("-p", p, "align", "--json").exit_code == 0  # no OCR: evidence has no hits
    assert (run / "evidence.json").is_file()

    # approving before anything is submitted is refused
    assert invoke("-p", p, "plan", "approve").exit_code == 1
    bad = write(run / "plan" / "bad.json", plan_data(proj / "missing.mp4"))
    assert invoke("-p", p, "plan", "submit", str(bad)).exit_code == 1
    assert parse_json(invoke("-p", p, "status", "--json").stdout)["status"] == "analyzed"

    draft = write(run / "plan" / "draft.json", plan_data(proj / "demo.mp4"))
    assert invoke("-p", p, "plan", "submit", str(draft)).exit_code == 0
    st = parse_json(invoke("-p", p, "status", "--json").stdout)
    assert st["status"] == "awaiting_plan_approval" and st["pending_gate"] == "plan_approval"
    board = parse_json(invoke("-p", p, "storyboard", "--json").stdout)
    assert board["version"] == 1 and board["duration"] == pytest.approx(6.5)
    assert [r["id"] for r in board["rows"]] == ["start", "export"]

    assert invoke("-p", p, "plan", "reject", "-m", "add the sign in").exit_code == 0
    assert "add the sign in" in (run / "plan" / "feedback.md").read_text()
    data = plan_data(proj / "demo.mp4")
    data["edl"]["segments"].insert(
        0, {"id": "login", "source": "demo", "start": 0, "end": 4, "speed": 4, "feature": "sign-in"}
    )
    write(draft, data)
    assert invoke("-p", p, "plan", "submit", str(draft)).exit_code == 0
    diff = invoke("-p", p, "plan", "diff", "1", "2").stdout
    assert '+        "id": "login"' in diff
    assert invoke("-p", p, "plan", "approve", "-m", "ship it").exit_code == 0

    st = parse_json(invoke("-p", p, "status", "--json").stdout)
    assert st["status"] == "plan_approved" and st["plan_version"] == 2
    edl = json.loads((run / "edl.json").read_text())
    assert [s["id"] for s in edl["segments"]] == ["login", "export"]
    gates = [
        r for r in read_trace(run / "trace.jsonl") if r["name"] == "feedback:gate:plan_approval"
    ]
    assert [g["inputs"]["score"] for g in gates] == [0, 1]

    res = invoke("-p", p, "export", "--run", st["id"], "--proxy", "--encoder", "cpu", "--json")
    assert res.exit_code == 0, res.output
    assert Path(parse_json(res.stdout)["results"][0]["path"]).is_relative_to(run / "out")


def test_context_paths(proj: Path, invoke):
    ctx = parse_json(invoke("-p", str(proj), "context").stdout)
    assert ctx["exists"]["manifest"] and not ctx["exists"]["catalog"]
    assert ctx["project"]["max_duration"] == 30
    assert ctx["vision_reports"] == [] and ctx["plan_versions"] == []


def test_schema_command(invoke):
    for kind in ("catalog", "vision", "alignment", "plan", "edl"):
        res = invoke("schema", kind)
        assert res.exit_code == 0 and "properties" in parse_json(res.stdout)


def _copy(**kw) -> dict:
    return {
        "start_banner": {
            "title": "Smart Report",
            "subtitle": "Sales data to a PDF in one click.",
            "hero_at": 13.0,
            "sources": ["catalog:overview"],
        },
        "end_banner": {
            "title": "Built by Sineth Sankalpa",
            "lines": ["linkedin.com/in/sinsankio"],
            "cta": "Follow for more builds",
        },
        "texts": [
            {
                "segment": "export",
                "style": "title",
                "text": "Export to PDF",
                "sources": ["export-pdf"],
            },
            {
                "segment": "export",
                "style": "caption",
                "text": "One click, and the report is ready",
                "sources": ["export-pdf", "ocr:Report ready"],
            },
        ],
        "post": "I built Smart Report.\n\nOne click turns sales data into a PDF.",
        "notes": ["'One click' comes from the docs overview."],
    } | kw


def test_copy_validation(proj: Path, invoke):
    p = str(proj)
    run = Path(parse_json(invoke("-p", p, "context").stdout)["paths"]["run_dir"])
    write(run / "catalog.json", CATALOG)
    write(run / "plan" / "draft.json", plan_data(proj / "demo.mp4"))
    good = write(run / "copy.json", _copy())
    assert parse_json(invoke("-p", p, "validate", "copy", str(good), "--json").stdout)["ok"]

    bad = write(
        run / "bad-copy.json",
        _copy(
            texts=[
                {"segment": "ghost", "style": "title", "text": "x", "sources": ["export-pdf"]},
                {
                    "segment": "export",
                    "style": "title",
                    "text": "A very long feature title that keeps going on",
                    "sources": ["nope"],
                },
                {
                    "segment": "export",
                    "style": "caption",
                    "text": "one two three four five six seven eight nine ten",
                    "sources": ["web:x"],
                },
            ],
            post="Coming soon [[NEEDS INPUT: date]]",
            start_banner={"title": "x", "hero_at": 99},
        ),
    )
    problems = parse_json(invoke("-p", p, "validate", "copy", str(bad), "--json").stdout)[
        "problems"
    ]
    text = " | ".join(problems)
    for needle in (
        "no segment 'ghost'",
        "title is 45 chars",
        "caption has 10 words",
        "unknown catalog feature 'nope'",
        "unknown source kind 'web:x'",
        "placeholder",
        "hero_at 99",
    ):
        assert needle in text, needle

    dup = write(run / "dup.json", _copy(texts=_copy()["texts"] + [_copy()["texts"][0]]))
    res = invoke("-p", p, "validate", "copy", str(dup), "--json")
    assert res.exit_code == 1 and "more than one text" in res.stdout


def test_copy_apply_submit_approve_export(proj: Path, invoke):
    p = str(proj)
    run = Path(parse_json(invoke("-p", p, "context").stdout)["paths"]["run_dir"])
    write(run / "catalog.json", CATALOG)
    data = plan_data(proj / "demo.mp4")
    data["edl"]["segments"][0]["texts"] = [
        {"text": "draft caption", "start": 10.0, "end": 12.0, "style": "caption"}
    ]
    draft = write(run / "plan" / "draft.json", data)
    write(run / "copy.json", _copy())
    res = invoke("-p", p, "copy", "apply", str(run / "copy.json"))
    assert res.exit_code == 0, res.output

    plan = json.loads(draft.read_text())
    (seg,) = plan["edl"]["segments"]
    texts = {t["style"]: t for t in seg["texts"]}
    assert texts["caption"]["text"] == "One click, and the report is ready"
    assert (texts["caption"]["start"], texts["caption"]["end"]) == (10.0, 12.0)  # timing kept
    assert texts["title"]["start"] == 9.5 and texts["title"]["end"] == 14  # defaulted
    start = plan["edl"]["start_banner"]
    assert start["hero"] == {"source": "demo", "at": 13.0} and start["duration"] == 2
    assert plan["edl"]["end_banner"]["cta"] == "Follow for more builds"
    assert plan["post"].startswith("I built") and plan["copy_notes"]

    assert invoke("-p", p, "plan", "submit", str(draft)).exit_code == 0
    shown = invoke("-p", p, "plan", "show").stdout
    assert "LinkedIn post" in shown and "copy note" in shown
    assert invoke("-p", p, "plan", "approve").exit_code == 0
    assert (run / "post.linkedin.txt").read_text().startswith("I built Smart Report")
    res = invoke("-p", p, "export", "--run", "r-0001", "--proxy", "--encoder", "cpu", "--json")
    assert res.exit_code == 0, res.output
    post = parse_json(res.stdout)["post"]
    assert post and Path(post).read_text().startswith("I built")


def test_copy_post_respects_setting(proj: Path, invoke):
    p = str(proj)
    invoke("-p", p, "config", "set", "writing.linkedin_post", "false")
    run = Path(parse_json(invoke("-p", p, "context").stdout)["paths"]["run_dir"])
    write(run / "catalog.json", CATALOG)
    write(run / "plan" / "draft.json", plan_data(proj / "demo.mp4"))
    copy = write(run / "copy.json", _copy())
    problems = parse_json(invoke("-p", p, "validate", "copy", str(copy), "--json").stdout)[
        "problems"
    ]
    assert any("linkedin_post is off" in x for x in problems)
