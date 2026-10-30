"""Cheap per-second signals from a screen recording: motion, scene cuts, idle stretches,
where on screen things change, and silence. Computed on small grayscale frames."""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ave.media import MediaError, MediaInfo, Tools

SAMPLE_FPS = 4
SAMPLE_WIDTH = 320
PIXEL_DELTA = 12  # gray-level change that counts as "changed" (ignores compression noise)
# Cut and idle detection use a coarse block grid: a new page changes many blocks even when the
# app chrome and background stay the same, while typing or a spinner changes one or two.
BLOCKS = (64, 36)
BLOCK_DELTA = 4.0  # mean gray-level change of a block that counts as changed
CUT_FRACTION = 0.04  # share of blocks changing between samples that marks a scene cut
LOCAL_FRACTION = 0.25  # pixel share above which a change is page-wide, not a place to look
CUT_MIN_GAP = 0.75  # seconds between cuts
IDLE_FRACTION = 0.5 / (BLOCKS[0] * BLOCKS[1])  # i.e. no block changed: the screen is static
IDLE_MIN = 1.5  # seconds of stillness to report an idle range
WINDOW = 2.0  # seconds per activity window
SILENCE_DB = -40
SILENCE_MIN = 0.8


@dataclass
class Activity:
    start: float
    end: float
    energy: float  # mean share of changed blocks per sample
    boxes: list[dict]  # normalized {x, y, w, h} regions where pixels changed, largest first


@dataclass
class Signals:
    duration: float
    sample_fps: float
    motion: list[float]  # share of blocks changed since the previous sample; motion[i] is at i/fps
    scenes: list[float]  # cut times (seconds), always starting with 0.0
    idle: list[tuple[float, float]]
    activity: list[Activity]
    has_audio: bool
    silence: list[tuple[float, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["motion"] = [round(m, 5) for m in self.motion]
        return d


def gray_frames(info: MediaInfo, tools: Tools, fps: float = SAMPLE_FPS, width: int = SAMPLE_WIDTH):
    """Yield (t, frame) for small grayscale frames sampled at `fps`."""
    width = min(width, info.width) // 2 * 2
    height = max(2, round(info.height * width / info.width / 2) * 2)
    cmd = [
        tools.ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(info.path),
        "-an",
        "-vf",
        f"fps={fps},scale={width}:{height},format=gray",
        "-f",
        "rawvideo",
        "-",
    ]
    size = width * height
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        assert proc.stdout is not None
        i = 0
        try:
            while True:
                buf = proc.stdout.read(size)
                if len(buf) < size:
                    break
                yield i / fps, np.frombuffer(buf, np.uint8).reshape(height, width)
                i += 1
        finally:
            proc.stdout.close()
            rc = proc.wait()
        if rc != 0 and i == 0:
            err.seek(0)
            msg = err.read().decode(errors="replace")[-300:]
            raise MediaError(f"decoding {info.path} failed: {msg}")


MIN_REGION = 0.0005  # smallest changed region reported, as a share of the frame


def _boxes(mask_counts: np.ndarray, limit: int = 3) -> list[dict]:
    """Up to `limit` separate changed regions, largest first, as normalized boxes."""
    mask = (mask_counts > 0).astype(np.uint8)
    if not mask.any():
        return []
    mask = cv2.dilate(mask, np.ones((5, 5), np.uint8))  # join the strokes of one widget
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    h, w = mask.shape
    regions = sorted(stats[1:], key=lambda st: -st[cv2.CC_STAT_AREA])
    out = []
    for st in regions[:limit]:
        if st[cv2.CC_STAT_AREA] < MIN_REGION * w * h:
            continue
        x, y = st[cv2.CC_STAT_LEFT], st[cv2.CC_STAT_TOP]
        bw, bh = st[cv2.CC_STAT_WIDTH], st[cv2.CC_STAT_HEIGHT]
        out.append(
            {
                "x": round(x / w, 4),
                "y": round(y / h, 4),
                "w": round(bw / w, 4),
                "h": round(bh / h, 4),
            }
        )
    return out


def runs_below(values: list[float], threshold: float, fps: float, min_len: float):
    """(start, end) seconds of runs where values stay below threshold for at least min_len."""
    out, start = [], None
    for i, v in enumerate([*values, threshold + 1]):
        if v < threshold and start is None:
            start = i
        elif v >= threshold and start is not None:
            if (i - start) / fps >= min_len:
                out.append((round(start / fps, 3), round(i / fps, 3)))
            start = None
    return out


def detect_cuts(motion: list[float], fps: float) -> list[float]:
    cuts = [0.0]
    for i, m in enumerate(motion):
        t = i / fps
        if m >= CUT_FRACTION and t - cuts[-1] >= CUT_MIN_GAP:
            cuts.append(round(t, 3))
    return cuts


def detect_silence(video: Path, tools: Tools, duration: float) -> list[tuple[float, float]]:
    cmd = [
        tools.ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-i",
        str(video),
        "-vn",
        "-af",
        f"silencedetect=n={SILENCE_DB}dB:d={SILENCE_MIN}",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", proc.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", proc.stderr)]
    out = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else duration
        out.append((round(max(0.0, s), 3), round(min(e, duration), 3)))
    return out


def extract_signals(info: MediaInfo, tools: Tools, fps: float = SAMPLE_FPS) -> Signals:
    motion: list[float] = []
    activity: list[Activity] = []
    prev = prev_blocks = None
    win_counts = None
    win_energy: list[float] = []
    win_start = 0.0
    per_window = max(1, round(WINDOW * fps))

    def close_window(end: float) -> None:
        if win_counts is None or not win_energy:
            return
        energy = float(np.mean(win_energy))
        boxes = _boxes(win_counts) if energy >= IDLE_FRACTION else []
        activity.append(Activity(round(win_start, 3), round(end, 3), round(energy, 5), boxes))

    for t, frame in gray_frames(info, tools, fps):
        blocks = cv2.resize(frame, BLOCKS, interpolation=cv2.INTER_AREA).astype(np.float32)
        if prev is None:
            prev, prev_blocks = frame, blocks
            win_counts = np.zeros(frame.shape, np.uint16)
            motion.append(0.0)
            continue
        frac = float((np.abs(blocks - prev_blocks) > BLOCK_DELTA).mean())
        prev_blocks = blocks
        motion.append(frac)
        changed = np.abs(frame.astype(np.int16) - prev.astype(np.int16)) > PIXEL_DELTA
        if changed.mean() < LOCAL_FRACTION:  # a page-wide change says nothing about *where*
            win_counts += changed
        win_energy.append(frac)
        if len(win_energy) >= per_window:
            close_window(t)
            win_counts = np.zeros(frame.shape, np.uint16)
            win_energy = []
            win_start = t
        prev = frame
    close_window(info.duration)
    if prev is None:
        raise MediaError(f"{info.path}: no frames decoded")

    silence = detect_silence(info.path, tools, info.duration) if info.has_audio else []
    return Signals(
        duration=info.duration,
        sample_fps=fps,
        motion=motion,
        scenes=detect_cuts(motion, fps),
        idle=runs_below(motion, IDLE_FRACTION, fps, IDLE_MIN),
        activity=activity,
        has_audio=info.has_audio,
        silence=silence if info.has_audio else [(0.0, round(info.duration, 3))],
    )
