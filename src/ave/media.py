"""ffmpeg/ffprobe helpers: binary resolution, probing, encoder selection."""

from __future__ import annotations

import functools
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Literal

import numpy as np

from ave.config import FFmpeg


class MediaError(Exception):
    """ffmpeg/ffprobe failed or a file is not usable media."""


@dataclass(frozen=True)
class Tools:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"

    @classmethod
    def from_config(cls, cfg: FFmpeg | None = None) -> Tools:
        cfg = cfg or FFmpeg()
        return cls(
            ffmpeg=os.environ.get("AVE_FFMPEG") or cfg.path,
            ffprobe=os.environ.get("AVE_FFPROBE") or cfg.ffprobe,
        )


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    codec: str

    def to_dict(self) -> dict:
        return asdict(self) | {"path": str(self.path)}


def run_ffmpeg(tools: Tools, args: list[str], timeout: float | None = None) -> None:
    cmd = [tools.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except OSError as e:
        raise MediaError(f"cannot run {tools.ffmpeg}: {e}") from e
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg failed ({proc.returncode}): {_tail(proc.stderr)}")


def _tail(text: str, lines: int = 6) -> str:
    return "\n".join(text.strip().splitlines()[-lines:]) or "no output"


def probe(path: Path, tools: Tools | None = None) -> MediaInfo:
    tools = tools or Tools()
    if not path.is_file():
        raise MediaError(f"no such file: {path}")
    cmd = [
        tools.ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except OSError as e:
        raise MediaError(f"cannot run {tools.ffprobe}: {e}") from e
    if proc.returncode != 0:
        raise MediaError(f"ffprobe failed on {path}: {_tail(proc.stderr)}")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError(f"{path} has no video stream")
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    try:
        fps = float(Fraction(rate)) if rate != "0/0" else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    width, height = int(video["width"]), int(video["height"])
    rotation = _rotation(video)
    if rotation in (90, 270):
        width, height = height, width
    return MediaInfo(
        path=path,
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        codec=video.get("codec_name", "?"),
    )


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            return abs(int(side["rotation"])) % 360
    return abs(int(stream.get("tags", {}).get("rotate", 0))) % 360


EncoderName = Literal["h264_nvenc", "libx264"]


@functools.cache
def nvenc_works(ffmpeg: str) -> bool:
    """True when h264_nvenc is compiled in and can actually open a GPU session."""
    try:
        listed = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=15
        )
        if "h264_nvenc" not in listed.stdout:
            return False
        test = subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "color=c=black:s=256x256:d=0.1", "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return False
    return test.returncode == 0


def pick_encoder(choice: str, tools: Tools) -> EncoderName:
    if choice == "cpu":
        return "libx264"
    if choice == "nvenc":
        if not nvenc_works(tools.ffmpeg):
            raise MediaError("encoder=nvenc requested but h264_nvenc does not work here")
        return "h264_nvenc"
    return "h264_nvenc" if nvenc_works(tools.ffmpeg) else "libx264"


def video_encoder_args(encoder: EncoderName, proxy: bool, fps: int) -> list[str]:
    gop = ["-g", str(fps * 2)]
    if encoder == "h264_nvenc":
        quality = ["-preset", "p1", "-cq", "30"] if proxy else ["-preset", "p5", "-cq", "21"]
        return ["-c:v", "h264_nvenc", "-rc", "vbr", "-b:v", "0", *quality, *gop]
    quality = (
        ["-preset", "ultrafast", "-crf", "30"] if proxy else ["-preset", "medium", "-crf", "20"]
    )
    return ["-c:v", "libx264", *quality, *gop]


AUDIO_ARGS = ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
# identical container params on every clip so the concat demuxer can stream-copy
CLIP_ARGS = ["-pix_fmt", "yuv420p", "-video_track_timescale", "90000", "-movflags", "+faststart"]


def grab_frame(info: MediaInfo, t: float, tools: Tools) -> np.ndarray:
    """One BGR frame at source time t, at the source's size."""

    w, h = info.width // 2 * 2, info.height // 2 * 2
    cmd = [tools.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}",
           "-i", str(info.path), "-frames:v", "1", "-vf", f"scale={w}:{h}",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    if proc.returncode != 0 or len(proc.stdout) < w * h * 3:
        raise MediaError(f"cannot grab a frame at {t:.2f}s from {info.path}")
    return np.frombuffer(proc.stdout[: w * h * 3], np.uint8).reshape(h, w, 3)
