"""Pick the frames worth looking at, save them, and tile them into labeled sheets.

Vision agents read the sheets (cheap overview); OCR and zoom checks read the full frames.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from ave.analysis.signals import Signals
from ave.config import Brand
from ave.media import MediaError, MediaInfo, Tools
from ave.overlays import Fonts

MAX_GAP = 2.5  # seconds between keyframes inside a busy scene
SETTLE = 0.4  # seconds after a cut before sampling (page still painting)
MAX_KEYFRAMES = 150
FRAME_WIDTH = 1280
SHEET_COLS, SHEET_ROWS = 3, 3
THUMB_WIDTH = 640
GRID = 10  # grid lines per axis drawn on sheet thumbnails (every 0.1 of the frame)


@dataclass
class Keyframe:
    index: int
    t: float
    file: str  # relative to the analysis dir
    sheet: str | None = None
    cell: int | None = None  # 1-based position on its sheet

    def to_dict(self) -> dict:
        return asdict(self)


def _inside(t: float, ranges: list[tuple[float, float]]) -> tuple[float, float] | None:
    for a, b in ranges:
        if a <= t <= b:
            return (a, b)
    return None


def select_times(
    sig: Signals, max_gap: float = MAX_GAP, max_count: int = MAX_KEYFRAMES
) -> list[float]:
    """One frame shortly after each cut, then every max_gap seconds; one frame per idle stretch."""
    bounds = [*sig.scenes, sig.duration]
    times: list[float] = []
    for a, b in zip(bounds, bounds[1:], strict=False):
        if b - a <= 0:
            continue
        t = min(a + SETTLE, (a + b) / 2)
        while t < b - 1e-6:
            idle = _inside(t, sig.idle)
            if idle is not None:
                times.append(round((max(idle[0], a) + min(idle[1], b)) / 2, 3))
                t = min(idle[1], b) + SETTLE  # skip the rest of a static stretch
                continue
            times.append(round(t, 3))
            t += max_gap
    times = sorted(set(t for t in times if 0 <= t < sig.duration))
    if len(times) > max_count:
        step = len(times) / max_count
        times = [times[int(i * step)] for i in range(max_count)]
    return times


def extract_frames(
    info: MediaInfo, times: list[float], out_dir: Path, tools: Tools, width: int = FRAME_WIDTH
) -> list[Keyframe]:
    out_dir.mkdir(parents=True, exist_ok=True)
    width = min(width, info.width) // 2 * 2
    frames = []
    for i, t in enumerate(times, 1):
        name = f"kf-{i:04d}.jpg"
        cmd = [
            tools.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{t:.3f}",
            "-i",
            str(info.path),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:-2",
            "-q:v",
            "3",
            str(out_dir / name),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0 or not (out_dir / name).is_file():
            raise MediaError(f"cannot extract frame at {t:.2f}s: {proc.stderr.strip()[-200:]}")
        frames.append(Keyframe(i, t, f"{out_dir.name}/{name}"))
    return frames


def fmt_time(t: float) -> str:
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def _grid(im: Image.Image) -> None:
    d = ImageDraw.Draw(im, "RGBA")
    w, h = im.size
    for k in range(1, GRID):
        x, y = w * k / GRID, h * k / GRID
        d.line([(x, 0), (x, h)], fill=(128, 128, 128, 90), width=1)
        d.line([(0, y), (w, y)], fill=(128, 128, 128, 90), width=1)


def build_sheets(frames: list[Keyframe], analysis_dir: Path, sheet_dir: Path) -> list[dict]:
    """Tile keyframes 3x3 with `#index  mm:ss.ss` labels and a light 10x10 grid.

    The grid lets an agent describe a region as fractions of the frame (x 0.6-0.9, y 0.1-0.3).
    """
    sheet_dir.mkdir(parents=True, exist_ok=True)
    per = SHEET_COLS * SHEET_ROWS
    font = Fonts.for_brand(Brand()).get(22, bold=True)
    sheets = []
    for s, start in enumerate(range(0, len(frames), per), 1):
        chunk = frames[start : start + per]
        thumbs = []
        for kf in chunk:
            with Image.open(analysis_dir / kf.file) as im:
                th = im.convert("RGB")
                th.thumbnail((THUMB_WIDTH, THUMB_WIDTH))
                _grid(th)
                thumbs.append(th)
        tw = max(t.width for t in thumbs)
        th_h = max(t.height for t in thumbs)
        label, gap = 34, 10
        cols = min(SHEET_COLS, len(thumbs))
        rows = -(-len(thumbs) // SHEET_COLS)
        sheet = Image.new(
            "RGB", (cols * (tw + gap) + gap, rows * (th_h + label + gap) + gap), (20, 20, 24)
        )
        d = ImageDraw.Draw(sheet)
        name = f"sheet-{s:03d}.png"
        for c, (kf, im) in enumerate(zip(chunk, thumbs, strict=True)):
            x = gap + (c % SHEET_COLS) * (tw + gap)
            y = gap + (c // SHEET_COLS) * (th_h + label + gap)
            sheet.paste(im, (x, y + label))
            d.text((x + 4, y + 4), f"#{kf.index}  {fmt_time(kf.t)}", font=font, fill=(255, 220, 90))
            kf.sheet, kf.cell = f"{sheet_dir.name}/{name}", c + 1
        sheet.save(sheet_dir / name)
        sheets.append(
            {
                "file": f"{sheet_dir.name}/{name}",
                "start": chunk[0].t,
                "end": chunk[-1].t,
                "keyframes": [k.index for k in chunk],
            }
        )
    return sheets
