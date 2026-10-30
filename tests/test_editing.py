"""Editing phase through the CLI: QA, change requests, QA pass, Gate 3, resume."""

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


@pytest.fixture(scope="module")
def demo_files(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("demo")
    synth.make_project_files(d)
    return d


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def approved(tmp_path: Path, demo_files: Path, invoke) -> tuple[Path, Path]:
    """Project with run r-0001 at plan_approved: title over the header, soft zoom."""
    shutil.copy(demo_files / "demo.mp4", tmp_path / "demo.mp4")
    p = str(tmp_path)
    invoke(
        "-p",
        p,
        "init",
        "--yes",
        "--name",
        "QA",
        "--video",
        "demo.mp4",
        "--platform",
        "linkedin",
        "--max-duration",
        "30",
    )
    assert invoke("-p", p, "analyze", "--json").exit_code == 0  # OCR if the extra is installed
    run = tmp_path / "runs" / "r-0001"
    plan = {
        "summary": "s",
        "target_duration": 8,
        "edl": {
            "sources": {"demo": str(tmp_path / "demo.mp4")},
            "segments": [
                {
                    "id": "export",
                    "source": "demo",
                    "start": 9.5,
                    "end": 14,
                    "texts": [{"text": "Export to PDF", "start": 9.5, "end": 14, "style": "title"}],
                    "zooms": [
                        {
                            "start": 11,
                            "end": 13.8,
                            "box": {"x": 0.63, "y": 0.76, "w": 0.35, "h": 0.17},
                        }
                    ],
                }
            ],
            "end_banner": {"title": "Built by Sineth Sankalpa", "duration": 1.5},
        },
        "post": "post text",
    }
    draft = write(run / "plan" / "draft.json", plan)
    assert invoke("-p", p, "plan", "submit", str(draft)).exit_code == 0
    assert invoke("-p", p, "plan", "approve").exit_code == 0
    return tmp_path, run


def option_edl(run: Path, name: str, **seg_kw) -> str:
    edl = json.loads((run / "edl.json").read_text())
    edl["segments"][0].update(seg_kw)
    write(run / "edit" / "options" / name, edl)
    return f"edit/options/{name}"


def cr(issues, options, recommended="A") -> dict:
    return {
        "title": "Fix",
        "problem": "p",
        "cause": "qa",
        "issues": issues,
        "options": options,
        "recommended": recommended,
    }


def opt(letter: str, edl: str | None) -> dict:
    return {
        "id": letter,
        "label": f"option {letter}",
        "description": "d",
        "tradeoffs": "t",
        "edl": edl,
    }


def test_edit_phase_end_to_end(approved, invoke):
    proj, run = approved
    p = str(proj)
    res = invoke("-p", p, "edit", "--encoder", "cpu", "--json")
    assert res.exit_code == 0, res.output
    qa = parse_json(res.stdout)
    assert Path(qa["proxies"]["linkedin"]).is_file()
    assert all(Path(f).is_file() for f in qa["sheets"]["linkedin"])
    assert parse_json(invoke("-p", p, "status", "--json").stdout)["status"] == "editing"
    warn_ids = [i["id"] for i in qa["issues"] if i["severity"] in ("warn", "error")]
    assert warn_ids, qa["issues"]  # soft zoom on 720p at least

    # undecided warnings block the QA pass
    res = invoke("-p", p, "qa", "pass")
    assert res.exit_code == 1 and "not decided by the user" in res.output

    # change request validation
    bad = write(
        run / "edit" / "bad.json",
        cr(["Q99"], [opt("A", "edit/options/missing.json"), opt("B", None)]),
    )
    res = invoke("-p", p, "changes", "new", str(bad))
    assert (
        res.exit_code == 1
        and "unknown issue id 'Q99'" in res.output
        and "no such EDL" in res.output
    )
    long_opt = option_edl(run, "long.json", end=60)
    bad = write(run / "edit" / "bad2.json", cr(warn_ids, [opt("A", long_opt), opt("B", None)]))
    res = invoke("-p", p, "changes", "new", str(bad))
    assert res.exit_code == 1 and "16.00s long" in res.output

    good_opt = option_edl(
        run,
        "CR-01-A.json",
        texts=[
            {
                "text": "Export to PDF",
                "start": 9.5,
                "end": 14,
                "style": "title",
                "position": "bottom",
            }
        ],
    )
    draft = write(
        run / "edit" / "cr-draft.json", cr(warn_ids, [opt("A", good_opt), opt("B", None)])
    )
    assert invoke("-p", p, "validate", "change", str(draft)).exit_code == 0
    res = invoke("-p", p, "changes", "new", str(draft))
    assert res.exit_code == 0, res.output
    st = parse_json(invoke("-p", p, "status", "--json").stdout)
    assert st["status"] == "awaiting_change_decision" and st["pending_gate"] == "change_decision"
    assert (
        invoke("-p", p, "changes", "new", str(draft)).exit_code == 1
    )  # one open request at a time
    assert invoke("-p", p, "changes", "choose", "CR-01", "Z").exit_code == 1
    assert invoke("-p", p, "changes", "choose", "CR-01").exit_code == 2  # needs option or --other

    before = (run / "edl.json").read_text()
    res = invoke("-p", p, "changes", "choose", "CR-01", "A", "-m", "move it")
    assert res.exit_code == 0, res.output
    assert (run / "edit" / "edl.r1.json").read_text() == before
    texts = json.loads((run / "edl.json").read_text())["segments"][0]["texts"]
    assert texts[0]["position"] == "bottom"
    rec = parse_json(invoke("-p", p, "changes", "--json").stdout)[0]
    assert rec["decision"]["option"] == "A" and rec["issue_keys"]

    # the edit changed: QA must be re-run before passing
    assert "changed since the last QA" in invoke("-p", p, "qa", "pass").output
    assert invoke("-p", p, "edit", "--encoder", "cpu", "--json").exit_code == 0
    shown = parse_json(invoke("-p", p, "qa", "show", "--json").stdout)
    assert shown["stale"] is False and shown["open_issues"] == []
    assert invoke("-p", p, "qa", "pass").exit_code == 0

    res = invoke("-p", p, "final", "approve", "-m", "ship it", "--encoder", "cpu")
    assert res.exit_code == 0, res.output
    assert (run / "out" / "linkedin.mp4").is_file()
    assert (run / "out" / "linkedin.post.txt").read_text().strip() == "post text"
    assert parse_json(invoke("-p", p, "status", "--json").stdout)["status"] == "done"
    names = [r["name"] for r in read_trace(run / "trace.jsonl")]
    for n in (
        "phase:edit",
        "tool:qa_checks",
        "gate:change_request[CR-01]",
        "feedback:change_request:CR-01",
        "gate:qa_pass",
        "feedback:gate:final_approval",
    ):
        assert n in names, n


def test_critic_errors_and_accept_risk(approved, invoke):
    proj, run = approved
    p = str(proj)
    assert invoke("-p", p, "edit", "--encoder", "cpu").exit_code == 0
    critic = write(
        run / "edit" / "critic.json",
        {
            "verdict": "changes_needed",
            "summary": "s",
            "issues": [
                {
                    "severity": "error",
                    "kind": "awkward-cut",
                    "message": "cut mid-click",
                    "segment": "export",
                }
            ],
        },
    )
    assert invoke("-p", p, "validate", "critic", str(critic)).exit_code == 0
    out = invoke("-p", p, "qa", "show").stdout
    assert "C01" in out and "awkward-cut" in out
    res = invoke("-p", p, "qa", "pass")
    assert res.exit_code == 1 and "C01" in res.output
    assert invoke("-p", p, "qa", "pass", "--accept-risk", "author is fine with it").exit_code == 0
    st = parse_json(invoke("-p", p, "status", "--json").stdout)
    assert st["status"] == "awaiting_final_approval"

    assert invoke("-p", p, "final", "reject", "-m", "zoom in more").exit_code == 0
    assert "zoom in more" in (run / "edit" / "final-feedback.md").read_text()
    assert parse_json(invoke("-p", p, "status", "--json").stdout)["status"] == "editing"


def test_keep_as_is_decision(approved, invoke):
    proj, run = approved
    p = str(proj)
    qa = parse_json(invoke("-p", p, "edit", "--encoder", "cpu", "--json").stdout)
    ids = [i["id"] for i in qa["issues"] if i["severity"] in ("warn", "error")]
    draft = write(run / "edit" / "cr.json", cr(ids, [opt("A", None), opt("B", None)]))
    assert invoke("-p", p, "changes", "new", str(draft)).exit_code == 0
    before = (run / "edl.json").read_text()
    assert (
        invoke("-p", p, "changes", "choose", "CR-01", "--other", "leave it, I like it").exit_code
        == 0
    )
    assert (run / "edl.json").read_text() == before
    assert invoke("-p", p, "qa", "pass").exit_code == 0  # put to the user: no longer blocking


def test_gates_refuse_wrong_state(approved, invoke):
    proj, _ = approved
    p = str(proj)
    assert invoke("-p", p, "qa", "pass").exit_code == 1
    assert invoke("-p", p, "final", "approve").exit_code == 1
    assert invoke("-p", p, "qa", "show").exit_code == 2


def test_resume_failed_run(approved, invoke):
    proj, run = approved
    p = str(proj)
    state = json.loads((run / "state.json").read_text())
    state["status"], state["failed_in"], state["error"] = "failed", "editing", "boom"
    (run / "state.json").write_text(json.dumps(state))
    res = invoke("-p", p, "resume", "--dry-run")
    assert res.exit_code == 0 and '"run": "r-0001"' in res.stdout
    assert parse_json(invoke("-p", p, "status", "--json").stdout)["status"] == "editing"
