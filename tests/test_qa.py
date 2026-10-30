"""Deterministic QA checks and review points (no ffmpeg)."""

import pytest

from ave.config import Brand, Platform
from ave.edl import EDL
from ave.qa import Checker, Timeline, review_points

SIZE = {"demo": (1280, 720)}
DASHBOARD = {"text": "Dashboard", "conf": 0.99, "box": {"x": 0.03, "y": 0.02, "w": 0.15, "h": 0.08}}
TOAST = {"text": "Report ready", "conf": 0.99, "box": {"x": 0.66, "y": 0.8, "w": 0.27, "h": 0.06}}
OCR = {
    "demo": [
        {"index": 1, "t": 10.4, "items": [DASHBOARD]},
        {"index": 2, "t": 13.6, "items": [DASHBOARD, TOAST]},
    ]
}


def edl(segments, **kw) -> EDL:
    return EDL.model_validate({"sources": {"demo": "demo.mp4"}, "segments": segments} | kw)


def seg(**kw) -> dict:
    return {"id": "export", "source": "demo", "start": 9.5, "end": 14} | kw


def kinds(issues) -> list[str]:
    return [i.kind for i in issues]


def check(e: EDL, platforms=(Platform.LINKEDIN,), ocr=None, cap=60.0):
    return Checker(e, Brand(), SIZE, OCR if ocr is None else ocr, cap).run(list(platforms))


def test_timeline_and_review_points():
    e = edl(
        [
            seg(
                texts=[{"text": "Export", "start": 9.5, "end": 14, "style": "title"}],
                zooms=[{"start": 11, "end": 13.8, "box": {"x": 0.6, "y": 0.7, "w": 0.3, "h": 0.2}}],
            ),
            seg(id="b", start=0, end=4, speed=4),
        ],
        start_banner={"title": "S", "duration": 2},
        end_banner={"title": "E", "duration": 2},
    )
    assert Timeline.of(e).starts == {"export": 2.0, "b": 6.5}
    labels = [label for _, label in review_points(e)]
    assert labels[0] == "start banner" and labels[-1] == "end banner"
    assert any("export zoom" in x for x in labels) and any("export title" in x for x in labels)
    times = [t for t, _ in review_points(e)]
    assert times == sorted(times) and len(set(round(t, 1) for t in times)) == len(times)


def test_duration_cap_and_targets():
    issues = check(
        edl([seg(start=0, end=70)]), platforms=[Platform.LINKEDIN, Platform.REELS], cap=60
    )
    assert ("duration", "error") in [(i.kind, i.severity) for i in issues]
    targets = [i for i in issues if i.kind == "target"]
    assert {i.platform for i in targets} == {"reels", "linkedin"}  # 70s: long for both


def test_flash_and_reading():
    e = edl(
        [
            seg(start=0, end=4, speed=8),
            seg(
                id="c",
                start=5,
                end=7,
                texts=[{"text": "Seven words that take time to read", "start": 5, "end": 6}],
            ),
        ]
    )
    issues = check(e, ocr={})
    assert "flash" in kinds(issues) and "reading" in kinds(issues)
    assert next(i for i in issues if i.kind == "reading").segment == "c"


def test_zoom_soft_and_small():
    tight = seg(
        zooms=[{"start": 11, "end": 13.8, "box": {"x": 0.66, "y": 0.8, "w": 0.1, "h": 0.05}}]
    )
    issues = check(edl([tight]))
    assert "zoom-soft" in kinds(issues)
    loose = seg(zooms=[{"start": 11, "end": 13.8, "box": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}}])
    tiny_text = {
        "demo": [
            {
                "index": 1,
                "t": 12.4,
                "items": [
                    {"text": "x", "conf": 1, "box": {"x": 0.5, "y": 0.5, "w": 0.02, "h": 0.01}}
                ],
            }
        ]
    }
    issues = check(edl([loose]), ocr=tiny_text)
    assert "zoom-small" in kinds(issues) and "zoom-soft" not in kinds(issues)


def test_title_covering_header_and_fix():
    title = {"text": "Export to PDF", "start": 9.5, "end": 14, "style": "title"}
    issues = check(edl([seg(texts=[title])]))
    (cover,) = [i for i in issues if i.kind == "covers-text"]
    assert "'Dashboard'" in cover.message and "position: bottom" in cover.suggestion
    moved = check(edl([seg(texts=[title | {"position": "bottom"}])]))
    assert "covers-text" not in kinds(moved)


def test_no_ocr_skips_text_checks():
    title = {"text": "Export to PDF", "start": 9.5, "end": 14, "style": "title"}
    assert "covers-text" not in kinds(check(edl([seg(texts=[title])]), ocr={}))


@pytest.mark.parametrize("platform", list(Platform))
def test_issue_ids_unique(platform):
    title = {"text": "Export to PDF", "start": 9.5, "end": 14, "style": "title"}
    issues = check(
        edl([seg(texts=[title]), seg(id="f", start=0, end=4, speed=8)]), platforms=[platform]
    )
    ids = [i.id for i in issues]
    assert len(ids) == len(set(ids)) and ids[0] == "Q01"
