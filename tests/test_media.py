"""Pure renderer/media helpers: no ffmpeg needed."""

import pytest

from ave.config import Platform
from ave.edl import EDL
from ave.media import Tools, pick_encoder, video_encoder_args
from ave.render import Canvas, atempo_chain
from ave.sheet import sample_times


@pytest.mark.parametrize(
    ("speed", "chain"),
    [(1, "atempo=1"), (4, "atempo=4"), (16, "atempo=16"), (0.5, "atempo=0.5"),
     (0.25, "atempo=0.5,atempo=0.5")],
)  # fmt: skip
def test_atempo_chain(speed, chain):
    assert atempo_chain(speed) == chain


def test_encoder_args():
    assert video_encoder_args("libx264", False, 30)[:2] == ["-c:v", "libx264"]
    assert "ultrafast" in video_encoder_args("libx264", True, 30)
    assert "p5" in video_encoder_args("h264_nvenc", False, 30)
    assert "-g" in video_encoder_args("h264_nvenc", True, 30)


def test_canvas_proxy_halves_and_stays_even():
    e = EDL.model_validate(
        {"sources": {"main": "x.mp4"}, "segments": [{"id": "a", "start": 0, "end": 1}]}
    )
    full = Canvas.for_platform(Platform.REELS, e, proxy=False)
    proxy = Canvas.for_platform(Platform.REELS, e, proxy=True)
    assert (full.width, full.height) == (1080, 1920)
    assert (proxy.width, proxy.height) == (540, 960)
    assert proxy.layout.width == 540


def test_sample_times():
    assert sample_times(10, 4) == [1.25, 3.75, 6.25, 8.75]
    assert sample_times(10, 0) == [5.0]


def test_pick_encoder_cpu_never_probes():
    assert pick_encoder("cpu", Tools(ffmpeg="definitely-not-ffmpeg")) == "libx264"


def test_pick_encoder_auto_falls_back_without_nvenc():
    assert pick_encoder("auto", Tools(ffmpeg="definitely-not-ffmpeg")) == "libx264"
