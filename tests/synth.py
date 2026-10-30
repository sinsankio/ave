"""Synthetic screen recording of a tiny app, with known events, for analysis tests.

Timeline (seconds), 1280x720 @ 10 fps:
    0-4    "Sign in" screen, email typed one character every 0.25 s   (typing)
    4-6    "Loading..." with a spinning dot                            (loading, cut at 4)
    6-10   Dashboard: "Revenue $12,400", "Export PDF" button       (cut at 6, idle ~6.2-10)
    10-11  button pressed (color change)
    11-16  toast "Report ready" bottom-right; "Max 100 rows"        (idle ~11.2-16)
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H, FPS, DURATION = 1280, 720, 10, 16
BG, FG, BLUE, PRESSED, GREEN = (
    (243, 244, 246),
    (17, 24, 39),
    (10, 102, 194),
    (6, 60, 120),
    (22, 163, 74),
)
EMAIL = "sineth@example.com"

DOCS = """# Smart Report

Smart Report turns your sales data into a shareable PDF report in one click.

## Sign in

Sign in with your work email on the **Sign in** page.

## Dashboard

The **Dashboard** shows revenue for the current month.

## Export to PDF

Press `Export PDF` to download the report. Exports up to 500 rows.
A "Report ready" message confirms the download.

## Dark mode

Switch to dark mode from settings.
"""


def _font(size: int, bold: bool = True):
    for path in (
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def frame(t: float) -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    big, mid = _font(56), _font(36, bold=False)
    if t < 4:
        d.text((480, 140), "Sign in", font=big, fill=FG)
        d.rectangle((400, 300, 880, 370), outline=FG, width=3)
        typed = EMAIL[: min(len(EMAIL), int(t / 0.25))]
        d.text((420, 312), typed, font=mid, fill=FG)
    elif t < 6:
        d.text((500, 300), "Loading...", font=big, fill=FG)
        a = t * 2 * math.pi
        cx, cy = 640 + 60 * math.cos(a), 450 + 60 * math.sin(a)
        d.ellipse((cx - 14, cy - 14, cx + 14, cy + 14), fill=BLUE)
    else:
        d.rectangle((0, 0, W, 90), fill=(255, 255, 255))
        d.text((40, 16), "Dashboard", font=big, fill=FG)
        d.text((80, 220), "Revenue $12,400", font=big, fill=FG)
        d.text((80, 320), "Max 100 rows", font=mid, fill=FG)
        color = PRESSED if 10 <= t < 11 else BLUE
        d.rounded_rectangle((900, 200, 1200, 280), radius=12, fill=color)
        d.text((930, 212), "Export PDF", font=_font(40), fill=(255, 255, 255))
        if t >= 11:
            d.rounded_rectangle((820, 560, 1240, 660), radius=14, fill=GREEN)
            d.text((860, 584), "Report ready", font=_font(40), fill=(255, 255, 255))
    return im


def make_demo(out: Path, ffmpeg: str = "ffmpeg", audio: bool = True) -> Path:
    cmd = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{W}x{H}",
        "-r",
        str(FPS),
        "-i",
        "-",
    ]
    if audio:
        cmd += ["-f", "lavfi", "-t", str(DURATION), "-i", "sine=f=330", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-shortest", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    for i in range(DURATION * FPS):
        proc.stdin.write(frame(i / FPS).tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed to write the synthetic demo")
    return out


def make_project_files(root: Path) -> None:
    make_demo(root / "demo.mp4")
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "README.md").write_text(DOCS)
