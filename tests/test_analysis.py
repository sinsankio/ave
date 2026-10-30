"""Analysis: pure helpers (unit) and extraction on the synthetic demo (ffmpeg / ocr)."""

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from ave.analysis import align, docs, keyframes, ocr, signals
from ave.analysis.signals import Signals
from ave.media import Tools, probe
from ave.schemas import Catalog
from tests import synth

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

# --- pure ------------------------------------------------------------------------------


def test_runs_below():
    vals = [0, 0, 0, 0, 0, 0, 1, 0, 0, 0]
    assert signals.runs_below(vals, 0.5, fps=4, min_len=1.0) == [(0.0, 1.5)]
    assert signals.runs_below(vals, 0.5, fps=4, min_len=0.5) == [(0.0, 1.5), (1.75, 2.5)]


def test_detect_cuts_respects_min_gap():
    motion = [0, 0.5, 0, 0.5, 0.5, 0, 0, 0, 0.2]
    # 0.25 is too close to the start, 1.0 too close to the cut at 0.75
    assert signals.detect_cuts(motion, fps=4) == [0.0, 0.75, 2.0]


def test_boxes_finds_separate_regions():
    mask = np.zeros((90, 160), np.uint16)
    mask[10:20, 10:40] = 1  # big region
    mask[70:80, 120:130] = 1  # small region
    boxes = signals._boxes(mask)
    assert len(boxes) == 2
    big, small = boxes
    assert big["x"] == pytest.approx(8 / 160, abs=0.02) and big["w"] > small["w"]
    assert small["y"] == pytest.approx(68 / 90, abs=0.03)
    assert signals._boxes(np.zeros((90, 160), np.uint16)) == []


def _sig(**kw) -> Signals:
    base = dict(
        duration=20.0, sample_fps=4, motion=[], scenes=[0.0], idle=[], activity=[], has_audio=False
    )
    return Signals(**(base | kw))


def test_select_times_after_cuts_and_gaps():
    times = keyframes.select_times(_sig(scenes=[0.0, 10.0]), max_gap=4)
    assert times == [0.4, 4.4, 8.4, 10.4, 14.4, 18.4]


def test_select_times_one_frame_per_idle_stretch():
    times = keyframes.select_times(_sig(idle=[(2.0, 18.0)]), max_gap=2)
    assert 10.0 in times  # middle of the idle range
    assert not any(2.0 < t < 10.0 or 10.0 < t < 18.0 for t in times)


def test_select_times_caps_count():
    assert len(keyframes.select_times(_sig(duration=600.0), max_gap=1, max_count=50)) == 50


def test_split_markdown(tmp_path: Path):
    md = tmp_path / "docs" / "guide.md"
    md.parent.mkdir()
    md.write_text(
        "Intro text before any heading.\n\n# Guide\n\nPress `Export PDF` or **Save**.\n"
        '```bash\n# not a heading\nave run\n```\n## Setup\nRun "Install".\n'
        "## Setup\nAgain.\n![shot](img/a.png)\n"
    )
    secs = docs.split_markdown(md, tmp_path)
    assert [s.id for s in secs] == ["guide#guide", "guide#guide-2", "guide#setup", "guide#setup-2"]
    first, guide, setup, setup2 = secs
    assert first.heading_path == ["guide"] and first.line_start == 1
    assert guide.cues == ["Export PDF", "Save"]
    assert "# not a heading" in guide.text  # fenced code stays inside the section
    assert setup.heading_path == ["Guide", "Setup"] and setup.cues == ["Install"]
    assert setup2.images == ["img/a.png"]
    assert all(s.file == "docs/guide.md" for s in secs)


@pytest.mark.parametrize(
    ("cue", "text", "hit"),
    [
        ("Export PDF", "Export PDF", True),
        ("Export PDF", "Click Export PDF now", True),
        ("Export PDF", "Exp0rt PDF", True),
        ("Export PDF", "Import CSV", False),
        ("Go", "Google", False),
        ("Go", "Go", True),
        ("", "x", False),
    ],
)
def test_match_score(cue, text, hit):
    assert (align.match_score(cue, text) >= align.MATCH_THRESHOLD) is hit


def test_cue_evidence_groups_consecutive_keyframes():
    cat = Catalog.model_validate(
        {
            "project_name": "x",
            "overview": "x",
            "features": [
                {
                    "id": "export",
                    "name": "Export",
                    "summary": "",
                    "behavior": "",
                    "ui_cues": ["Export PDF"],
                    "emphasis": 3,
                    "sources": ["s"],
                }
            ],
        }
    )
    box = {"x": 0, "y": 0, "w": 0.1, "h": 0.1}
    frames = [
        {"index": i, "t": t, "items": [{"text": txt, "conf": 0.9, "box": box}] if txt else []}
        for i, t, txt in [
            (1, 0.4, ""),
            (2, 3.0, "Export PDF"),
            (3, 5.0, "Export PDF"),
            (4, 8.0, ""),
            (5, 9.0, "export pdf"),
        ]
    ]
    (ev,) = align.cue_evidence(cat, {"demo": frames})
    assert [h.keyframe for h in ev.hits] == [2, 3, 5]
    assert [r["keyframes"] for r in ev.ranges] == [[2, 3], [5]]
    assert ev.ranges[0]["start"] == pytest.approx(2.5) and ev.ranges[0]["end"] == 8.0
    assert ev.ranges[1]["end"] == pytest.approx(10.0)  # last keyframe: padded


# --- on the synthetic demo -------------------------------------------------------------


@pytest.fixture(scope="module")
def demo(tmp_path_factory) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    return synth.make_demo(tmp_path_factory.mktemp("demo") / "demo.mp4")


@pytest.mark.ffmpeg
@needs_ffmpeg
def test_signals_on_demo(demo: Path):
    sig = signals.extract_signals(probe(demo), Tools())
    near = lambda t: any(abs(c - t) <= 0.3 for c in sig.scenes)  # noqa: E731
    assert near(4.0) and near(6.0)
    assert not near(2.0)  # typing is not a cut
    idle = [(a, b) for a, b in sig.idle if b - a >= 2]
    assert any(a <= 6.6 and b >= 9.5 for a, b in idle)
    assert any(a <= 11.6 and b >= 15.5 for a, b in idle)
    toast = next(a for a in sig.activity if a.start <= 11 < a.end)
    assert any(b["x"] > 0.6 and b["y"] > 0.7 for b in toast.boxes)  # "Report ready" toast region
    assert sig.has_audio and sig.silence == []


@pytest.mark.ffmpeg
@needs_ffmpeg
def test_keyframes_and_sheets(demo: Path, tmp_path: Path):
    info = probe(demo)
    sig = signals.extract_signals(info, Tools())
    times = keyframes.select_times(sig)
    frames = keyframes.extract_frames(info, times, tmp_path / "frames", Tools())
    sheets = keyframes.build_sheets(frames, tmp_path, tmp_path / "sheets")
    assert len(frames) == len(times) >= 5
    assert all((tmp_path / f.file).is_file() for f in frames)
    assert sheets[0]["keyframes"][0] == 1 and (tmp_path / sheets[0]["file"]).is_file()
    assert frames[0].sheet == sheets[0]["file"] and frames[0].cell == 1


@pytest.mark.ocr
@pytest.mark.skipif(not ocr.available(), reason="ocr extra not installed")
@needs_ffmpeg
def test_ocr_reads_ui_text(demo: Path, tmp_path: Path):
    info = probe(demo)
    frames = keyframes.extract_frames(info, [8.0, 8.5, 13.0], tmp_path / "frames", Tools())
    texts = ocr.read_keyframes(frames, tmp_path)
    words = [i.text for i in texts[2].items]
    assert "Export PDF" in words and "Report ready" in words
    assert texts[1].reused_from == 1  # identical screen: OCR result reused
    toast = next(i for i in texts[2].items if i.text == "Report ready")
    assert toast.box["x"] > 0.6 and toast.box["y"] > 0.75
    json.dumps(ocr.to_json(texts))  # serializable
