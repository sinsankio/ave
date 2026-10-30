"""Contact sheets: a grid of timestamped frames from a video, for review and for vision agents."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from ave.config import Brand
from ave.media import MediaError, Tools, probe
from ave.overlays import Fonts


def frame_at(video: Path, t: float, width: int, tools: Tools) -> Image.Image:
    cmd = [tools.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}",
           "-i", str(video), "-frames:v", "1", "-vf", f"scale={width}:-2",
           "-f", "image2pipe", "-vcodec", "png", "-"]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    if proc.returncode != 0 or not proc.stdout:
        raise MediaError(f"cannot grab frame at {t:.2f}s from {video}")
    return Image.open(io.BytesIO(proc.stdout)).convert("RGB")


def sample_times(duration: float, n: int) -> list[float]:
    """n evenly spaced instants, centered in equal slices so the first/last frames aren't black."""
    n = max(1, n)
    return [duration * (i + 0.5) / n for i in range(n)]


def contact_sheet(
    video: Path,
    out: Path,
    *,
    n: int = 12,
    cols: int = 4,
    thumb_width: int = 480,
    tools: Tools | None = None,
) -> Path:
    tools = tools or Tools()
    info = probe(video, tools)
    times = sample_times(info.duration, n)
    thumbs = [frame_at(video, t, thumb_width, tools) for t in times]
    tw, th = thumbs[0].size
    label_h, gap = 28, 8
    cols = max(1, min(cols, len(thumbs)))
    rows = -(-len(thumbs) // cols)
    sheet = Image.new(
        "RGB", (cols * (tw + gap) + gap, rows * (th + label_h + gap) + gap), (24, 24, 28)
    )
    d = ImageDraw.Draw(sheet)
    font = Fonts.for_brand(Brand()).get(18, bold=True)
    for i, (t, im) in enumerate(zip(times, thumbs, strict=True)):
        x = gap + (i % cols) * (tw + gap)
        y = gap + (i // cols) * (th + label_h + gap)
        sheet.paste(im, (x, y + label_h))
        d.text(
            (x + 4, y + 4), f"{int(t // 60):02d}:{t % 60:05.2f}", font=font, fill=(235, 235, 235)
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out
