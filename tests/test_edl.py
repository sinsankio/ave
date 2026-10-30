import json
from pathlib import Path

import pytest
import yaml

from ave.edl import EDL, EXAMPLE, EDLError, Segment, check_against_media, load_edl, resolve_sources
from ave.media import MediaInfo


def seg(**kw) -> dict:
    return {"id": "s1", "start": 10, "end": 20} | kw


FULL = {"x": 0, "y": 0, "w": 1, "h": 1}
HALF = {"x": 0, "y": 0, "w": 0.5, "h": 0.5}


def edl(**kw) -> dict:
    return {"sources": {"main": "demo.mp4"}, "segments": [seg()]} | kw


def test_example_is_valid():
    parsed = EDL.model_validate(yaml.safe_load(EXAMPLE))
    assert parsed.segments[0].speed == 4
    assert parsed.end_banner.lines == ["linkedin.com/in/sinsankio"]


def test_durations_and_time_mapping():
    e = EDL.model_validate(
        edl(
            segments=[seg(speed=4), seg(id="s2", start=30, end=36)],
            start_banner={"title": "Hi", "duration": 2},
            end_banner={"title": "Bye", "duration": 3},
        )
    )
    s = e.segments[0]
    assert s.duration == 2.5
    assert s.to_output(14) == 1.0
    assert e.duration == 2 + 2.5 + 6 + 3


@pytest.mark.parametrize(
    ("speed", "audio", "keeps", "badge"),
    [(1, "auto", True, False), (1.5, "auto", True, True), (4, "auto", False, True),
     (4, "keep", True, True), (1, "mute", False, False)],
)  # fmt: skip
def test_audio_and_badge_defaults(speed, audio, keeps, badge):
    s = Segment.model_validate(seg(speed=speed, audio=audio))
    assert s.keeps_audio is keeps
    assert s.shows_badge is badge
    assert Segment.model_validate(seg(speed=speed, badge=False)).shows_badge is False


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (edl(segments=[seg(end=10)]), "end must be after start"),
        (edl(segments=[seg(), seg()]), "duplicate segment ids"),
        (edl(segments=[seg(source="other")]), "unknown source"),
        (edl(segments=[seg(texts=[{"text": "x", "start": 5, "end": 12}])]), "outside the segment"),
        (edl(segments=[seg(zooms=[{"start": 11, "end": 21, "box": FULL}])]), "outside the segment"),
        (edl(segments=[seg(zooms=[{"start": 11, "end": 15, "box": HALF},
                                  {"start": 14, "end": 18, "box": HALF}])]), "overlap"),
        (edl(segments=[seg(zooms=[{"start": 11, "end": 15, "box": HALF | {"x": 0.8}}])]),
         "inside the frame"),
        (edl(segments=[seg(fade_in=2, fade_out=2, speed=4)]), "fades are longer"),
        (edl(segments=[seg(speed=20)]), "less than or equal to 16"),
        (edl(segments=[seg(id="bad id")]), "pattern"),
        (edl(segments=[]), "at least 1"),
        (edl(sources={}), "at least 1"),
        (edl(extra=1), "Extra inputs"),
        (edl(start_banner={"title": "x", "lines": ["a"] * 5}), "at most 4"),
        (edl(start_banner={"title": "x", "hero": {"source": "nope", "at": 1}}),
         "unknown hero source"),
    ],
)  # fmt: skip
def test_invalid(data, match):
    with pytest.raises(ValueError, match=match):
        EDL.model_validate(data)


def test_load_yaml_and_json(tmp_path: Path):
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(edl()))
    (tmp_path / "a.json").write_text(json.dumps(edl()))
    assert load_edl(tmp_path / "a.yaml") == load_edl(tmp_path / "a.json")


def test_load_errors(tmp_path: Path):
    with pytest.raises(EDLError, match="no such EDL"):
        load_edl(tmp_path / "missing.yaml")
    (tmp_path / "bad.yaml").write_text("segments: [unclosed")
    with pytest.raises(EDLError, match="cannot parse"):
        load_edl(tmp_path / "bad.yaml")
    (tmp_path / "bad.json").write_text(json.dumps({"segments": []}))
    with pytest.raises(EDLError, match="sources"):
        load_edl(tmp_path / "bad.json")


def test_resolve_sources(tmp_path: Path):
    e = EDL.model_validate(edl(sources={"main": "demo.mp4", "abs": "/data/x.mp4"}))
    resolved = resolve_sources(e, tmp_path)
    assert resolved["main"] == tmp_path / "demo.mp4"
    assert resolved["abs"] == Path("/data/x.mp4")


def test_hero_past_end():
    e = EDL.model_validate(edl(end_banner={"title": "x", "hero": {"source": "main", "at": 30}}))
    info = MediaInfo(Path("demo.mp4"), 25.0, 1920, 1080, 30, True, "h264")
    (problem,) = check_against_media(e, {"main": info})
    assert "hero frame" in problem


def test_check_against_media():
    e = EDL.model_validate(edl(segments=[seg(end=20.04), seg(id="s2", start=0, end=25)]))
    info = MediaInfo(Path("demo.mp4"), 20.0, 1920, 1080, 30, True, "h264")
    problems = check_against_media(e, {"main": info})
    assert len(problems) == 1 and "s2" in problems[0]


def test_schema_exports():
    schema = EDL.model_json_schema()
    assert "segments" in schema["properties"]
    assert "Zoom" in schema["$defs"]
