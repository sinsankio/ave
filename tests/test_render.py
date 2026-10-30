"""End-to-end renderer tests on synthetic sources. Need a real ffmpeg (CI job test-ffmpeg)."""

import json
import shutil
from pathlib import Path

import numpy as np
import pytest
import yaml

from ave.config import Brand, Observability, Platform
from ave.edl import EDL
from ave.media import Tools, probe, run_ffmpeg
from ave.observability import Tracer, read_trace
from ave.render import Canvas, Renderer, RenderError, export
from ave.sheet import contact_sheet, frame_at
from tests.conftest import parse_json

pytestmark = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]


# --- end to end ------------------------------------------------------------------------

SRC_W, SRC_H = 640, 360


@pytest.fixture(scope="module")
def media_dir(tmp_path_factory) -> Path:
    """demo.mp4: 8 s, left half red, right half blue, 440 Hz tone. mute.mp4: same, no audio."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("media")
    tools = Tools()
    half = f"s={SRC_W // 2}x{SRC_H}:r=30:d=8"
    video = ["-f", "lavfi", "-i", f"color=red:{half}", "-f", "lavfi", "-i", f"color=blue:{half}"]
    enc = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    run_ffmpeg(tools, [*video, "-f", "lavfi", "-i", "sine=f=440:d=8", "-filter_complex",
                       "[0][1]hstack[v]", "-map", "[v]", "-map", "2:a", *enc, "-c:a", "aac",
                       str(d / "demo.mp4")])  # fmt: skip
    run_ffmpeg(tools, [*video, "-filter_complex", "[0][1]hstack[v]", "-map", "[v]", *enc,
                       str(d / "mute.mp4")])  # fmt: skip
    return d


def make_edl(**kw) -> EDL:
    data = {
        "sources": {"main": "demo.mp4"},
        "start_banner": {"title": "Test Project", "subtitle": "Renderer test", "duration": 1},
        "segments": [
            {"id": "intro", "start": 0, "end": 2,
             "texts": [{"text": "Feature", "start": 0, "end": 2, "style": "title"}]},
            {"id": "fast", "start": 2, "end": 6, "speed": 4},
            {"id": "zoom", "start": 6, "end": 8,
             "zooms": [{"start": 6.2, "end": 7.8, "ease": 0.2,
                        "box": {"x": 0.65, "y": 0.35, "w": 0.25, "h": 0.25}}]},
        ],
        "end_banner": {"title": "Built by Sineth Sankalpa", "duration": 1},
    } | kw  # fmt: skip
    return EDL.model_validate(data)


def renderer(tmp_path: Path, trace: Path | None = None) -> Renderer:
    tracer = Tracer(Observability(provider="local" if trace else "none"), trace)
    return Renderer(tools=Tools(), encoder="libx264", brand=Brand(),
                    cache_dir=tmp_path / "cache", tracer=tracer)  # fmt: skip


def mean_bgr(video: Path, t: float) -> np.ndarray:
    rgb = np.asarray(frame_at(video, t, 160, Tools()), dtype=np.float32)
    return rgb[..., ::-1].reshape(-1, 3).mean(axis=0)


@pytest.mark.parametrize("platform", [Platform.LINKEDIN, Platform.REELS])
def test_export_end_to_end(media_dir: Path, tmp_path: Path, platform):
    e = make_edl()
    r = renderer(tmp_path, tmp_path / "trace.jsonl")
    (res,) = export(e, media_dir, [platform], tmp_path / "out", r, proxy=True)
    info = probe(res.path)
    canvas = Canvas.for_platform(platform, e, proxy=True)
    assert (info.width, info.height) == (canvas.width, canvas.height)
    assert info.duration == pytest.approx(e.duration, abs=0.15)  # 1 + 2 + 1 + 2 + 1
    assert info.has_audio
    assert res.clips == 5 and res.cached == 0
    names = [rec["name"] for rec in read_trace(tmp_path / "trace.jsonl")]
    assert "phase:export" in names and "tool:concat" in names
    assert sum(n.startswith("tool:render_segment") for n in names) == 3
    # nested under the phase even though clips render on worker threads
    recs = read_trace(tmp_path / "trace.jsonl")
    phase_id = next(x["id"] for x in recs if x["name"] == "phase:export")
    assert all(x["parent_id"] == phase_id for x in recs if x["name"].startswith("tool:render"))

    again = export(e, media_dir, [platform], tmp_path / "out", r, proxy=True)[0]
    assert again.cached == 5


def test_zoom_lands_on_box(media_dir: Path, tmp_path: Path):
    e = make_edl(start_banner=None, end_banner=None)
    (res,) = export(
        e, media_dir, [Platform.LINKEDIN], tmp_path / "out", renderer(tmp_path), proxy=True
    )
    # output timeline: intro 0-2, fast 2-3, zoom 3-5 (zoom hold ~3.4-4.6)
    before = mean_bgr(res.path, 0.5)
    held = mean_bgr(res.path, 4.0)
    assert before[2] > 80 and before[0] > 80  # both red and blue halves visible
    assert held[0] > 150 and held[2] < 40  # zoomed into the blue half only


def test_fit_on_portrait_has_background_bands(media_dir: Path, tmp_path: Path):
    e = make_edl(start_banner=None, end_banner=None, background="color")
    (res,) = export(
        e, media_dir, [Platform.REELS], tmp_path / "out", renderer(tmp_path), proxy=True
    )
    frame = np.asarray(frame_at(res.path, 0.5, 270, Tools()), dtype=np.float32)
    top_band = frame[:20].reshape(-1, 3).mean(axis=0)
    expected = np.array([10, 102, 194]) * 0.35  # brand primary, darkened (RGB)
    assert np.allclose(top_band, expected, atol=12)


def test_fill_reframe_crops(media_dir: Path, tmp_path: Path):
    e = make_edl(start_banner=None, end_banner=None, reframe="fill")
    e.segments[0].focus_x = 0.0  # left edge: only the red half
    (res,) = export(
        e, media_dir, [Platform.REELS], tmp_path / "out", renderer(tmp_path), proxy=True
    )
    m = mean_bgr(res.path, 1.0)
    assert m[2] > 150 and m[0] < 60


def test_source_without_audio(media_dir: Path, tmp_path: Path):
    e = make_edl(sources={"main": "mute.mp4"})
    (res,) = export(
        e, media_dir, [Platform.LINKEDIN], tmp_path / "out", renderer(tmp_path), proxy=True
    )
    assert probe(res.path).has_audio  # silence is muxed so every clip concatenates


def test_final_render_full_size(media_dir: Path, tmp_path: Path):
    e = make_edl(start_banner=None, end_banner=None, segments=make_edl().segments[:1])
    (res,) = export(e, media_dir, [Platform.LINKEDIN], tmp_path / "out", renderer(tmp_path))
    info = probe(res.path)
    assert (info.width, info.height) == (1920, 1080)
    assert res.path.name == "linkedin.mp4"


def test_duration_cap(media_dir: Path, tmp_path: Path):
    with pytest.raises(RenderError, match="cap"):
        export(make_edl(), media_dir, [Platform.LINKEDIN], tmp_path / "out", renderer(tmp_path),
               proxy=True, max_duration=3)  # fmt: skip


def test_segment_past_source_end(media_dir: Path, tmp_path: Path):
    e = make_edl(segments=[{"id": "late", "start": 5, "end": 12}])
    with pytest.raises(RenderError, match="8.00s long"):
        export(e, media_dir, [Platform.LINKEDIN], tmp_path / "out", renderer(tmp_path), proxy=True)


def test_missing_source(media_dir: Path, tmp_path: Path):
    e = make_edl(sources={"main": "nope.mp4"})
    with pytest.raises(RenderError, match="no such file"):
        export(e, media_dir, [Platform.LINKEDIN], tmp_path / "out", renderer(tmp_path), proxy=True)


def test_contact_sheet(media_dir: Path, tmp_path: Path):
    out = contact_sheet(
        media_dir / "demo.mp4", tmp_path / "sheet.png", n=6, cols=3, thumb_width=160
    )
    from PIL import Image

    with Image.open(out) as im:
        assert im.width == 3 * (160 + 8) + 8


# --- CLI -------------------------------------------------------------------------------


@pytest.fixture
def cli_project(tmp_path: Path, media_dir: Path, invoke) -> Path:
    shutil.copy(media_dir / "demo.mp4", tmp_path / "demo.mp4")
    data = make_edl().model_dump(mode="json", exclude_none=True)
    (tmp_path / "edl.yaml").write_text(yaml.safe_dump(data))
    res = invoke("-p", str(tmp_path), "init", "--yes", "--name", "CLI", "--platform", "linkedin")
    assert res.exit_code == 0
    return tmp_path


def test_cli_probe_validate_export_preview(cli_project: Path, invoke):
    p = str(cli_project)
    info = parse_json(invoke("-p", p, "probe", str(cli_project / "demo.mp4"), "--json").stdout)
    assert info["width"] == SRC_W and info["has_audio"] is True

    report = parse_json(
        invoke("-p", p, "edl", "validate", str(cli_project / "edl.yaml"), "--json").stdout
    )
    assert report["ok"] is True and report["duration"] == pytest.approx(7)

    res = invoke("-p", p, "export", "--edl", str(cli_project / "edl.yaml"), "--proxy",
                 "--encoder", "cpu", "--json")  # fmt: skip
    assert res.exit_code == 0, res.output
    out = json.loads(res.stdout)
    assert out["encoder"] == "libx264"
    assert Path(out["results"][0]["path"]).is_file()

    res = invoke("-p", p, "preview", "sheet", "--edl", str(cli_project / "edl.yaml"), "--no-open",
                 "--frames", "4", "--encoder", "cpu")  # fmt: skip
    assert res.exit_code == 0, res.output
    assert Path(res.stdout.strip()).is_file()

    res = invoke("-p", p, "preview", "final", "--edl", str(cli_project / "edl.yaml"), "--no-open")
    assert res.exit_code == 2  # final not exported yet


def test_cli_validate_reports_problems(cli_project: Path, invoke):
    bad = cli_project / "bad.yaml"
    bad.write_text(yaml.safe_dump({"sources": {"main": "demo.mp4"},
                                   "segments": [{"id": "a", "start": 0, "end": 30}]}))  # fmt: skip
    res = invoke("-p", str(cli_project), "edl", "validate", str(bad), "--json")
    assert res.exit_code == 1
    problems = parse_json(res.stdout)["problems"]
    assert any("long" in p for p in problems)


def test_banners_with_hero_and_photo(media_dir: Path, tmp_path: Path):
    from PIL import Image

    photo = tmp_path / "me.png"
    Image.new("RGB", (120, 160), (240, 180, 60)).save(photo)
    e = make_edl(
        segments=make_edl().segments[:1],
        start_banner={
            "title": "Test",
            "subtitle": "Hero frame behind",
            "duration": 1.5,
            "hero": {"source": "main", "at": 1.0},
        },
        end_banner={
            "title": "Built by Sineth Sankalpa",
            "lines": ["linkedin.com/in/sinsankio"],
            "cta": "Follow for more builds",
            "duration": 1.5,
        },
    )
    r = renderer(tmp_path)
    r.brand = r.brand.model_copy(update={"photo": photo})
    (res,) = export(e, media_dir, [Platform.LINKEDIN], tmp_path / "out", r, proxy=True)
    assert probe(res.path).duration == pytest.approx(5.0, abs=0.15)
    hero_frame = mean_bgr(res.path, 0.9)
    plain_end = mean_bgr(res.path, 4.4)  # end banner has no hero: brand gradient only
    assert hero_frame[2] > plain_end[2] + 15  # the red half of the source shows through
    again = export(e, media_dir, [Platform.LINKEDIN], tmp_path / "out", r, proxy=True)[0]
    assert again.cached == 3
    photo.write_bytes(photo.read_bytes() + b"\0")  # photo changed: end banner re-renders
    import os

    os.utime(photo, None)
    third = export(e, media_dir, [Platform.LINKEDIN], tmp_path / "out", r, proxy=True)[0]
    assert third.cached == 2
