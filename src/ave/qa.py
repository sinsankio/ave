"""Deterministic QA of an EDL as rendered for each platform.

Checks that need no eyes: the duration cap and platform targets, flash cuts, caption reading
time, whether zoomed text ends up large and sharp enough, and whether a title or caption sits on
top of on-screen text (OCR boxes pushed through the virtual camera). Also picks the moments a
reviewer should look at and tiles them into labeled sheets.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw

from ave.camera import Camera, Rect, placement
from ave.config import PRESETS, Brand, Platform
from ave.edl import EDL, Segment
from ave.media import Tools
from ave.overlays import Fonts, text_overlay
from ave.render import Canvas
from ave.sheet import frame_at

Severity = Literal["info", "warn", "error"]

FLASH_SECONDS = 0.8  # output segments shorter than this read as a glitch
READ_BASE, READ_PER_WORD = 0.6, 0.3  # seconds a caption needs on screen
MIN_TEXT_FRACTION = 0.018  # zoomed on-screen text should be at least this share of canvas height
MAX_UPSCALE = 2.5  # source pixels magnified more than this look soft
COVER_FRACTION = 0.3  # an overlay hiding this much of an OCR box counts as covering it


@dataclass
class Issue:
    id: str
    severity: Severity
    kind: str
    message: str
    platform: str | None = None
    segment: str | None = None
    at: float | None = None  # output seconds
    suggestion: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Timeline:
    """Output start time of every segment (after the start banner)."""

    starts: dict[str, float] = field(default_factory=dict)

    @classmethod
    def of(cls, edl: EDL) -> Timeline:
        t = edl.start_banner.duration if edl.start_banner else 0.0
        starts = {}
        for s in edl.segments:
            starts[s.id] = t
            t += s.duration
        return cls(starts)


def _nearest_ocr(frames: list[dict], t: float, window: tuple[float, float]) -> dict | None:
    inside = [f for f in frames if window[0] - 0.5 <= f["t"] <= window[1] + 0.5]
    return min(inside, key=lambda f: abs(f["t"] - t)) if inside else None


def _to_canvas(box: dict, win: Rect, src_w: int, src_h: int, cw: int, ch: int) -> Rect:
    s, ox, oy = placement(win, cw, ch)
    x = (box["x"] * src_w - win.x) * s + ox
    y = (box["y"] * src_h - win.y) * s + oy
    return Rect(x, y, box["w"] * src_w * s, box["h"] * src_h * s)


def _overlap(a: Rect, b: Rect) -> float:
    w = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    h = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    return max(0.0, w) * max(0.0, h)


class Checker:
    def __init__(
        self,
        edl: EDL,
        brand: Brand,
        source_sizes: dict[str, tuple[int, int]],
        ocr: dict[str, list[dict]],
        cap: float | None,
    ):
        self.edl = edl
        self.brand = brand
        self.fonts = Fonts.for_brand(brand)
        self.sizes = source_sizes
        self.ocr = ocr
        self.cap = cap
        self.issues: list[Issue] = []
        self.timeline = Timeline.of(edl)

    def _add(self, severity: Severity, kind: str, message: str, **kw) -> None:
        self.issues.append(Issue(f"Q{len(self.issues) + 1:02d}", severity, kind, message, **kw))

    def run(self, platforms: list[Platform]) -> list[Issue]:
        self._duration(platforms)
        for seg in self.edl.segments:
            self._segment(seg)
        for p in platforms:
            canvas = Canvas.for_platform(p, self.edl, proxy=False)
            for seg in self.edl.segments:
                self._zoom(seg, canvas)
                self._covering(seg, canvas)
        return self.issues

    # --- whole edit ------------------------------------------------------------------

    def _duration(self, platforms: list[Platform]) -> None:
        d = self.edl.duration
        if self.cap is not None and d > self.cap + 1e-6:
            self._add(
                "error",
                "duration",
                f"edit is {d:.1f}s, over the {self.cap:g}s cap",
                suggestion="cut or speed up the lowest-importance segments",
            )
        for p in platforms:
            pr = PRESETS[p]
            if not pr.target_min <= d <= pr.target_max:
                where = "short" if d < pr.target_min else "long"
                self._add(
                    "info",
                    "target",
                    f"{d:.1f}s is {where} for {p.value} ({pr.target_min:g}-{pr.target_max:g}s)",
                    platform=p.value,
                )

    # --- per segment -----------------------------------------------------------------

    def _segment(self, seg: Segment) -> None:
        start = self.timeline.starts[seg.id]
        if seg.duration < FLASH_SECONDS:
            self._add(
                "warn",
                "flash",
                f"{seg.id} is on screen for only {seg.duration:.2f}s",
                segment=seg.id,
                at=start,
                suggestion="lower the speed, lengthen it, or cut it",
            )
        for t in seg.texts:
            shown = (t.end - t.start) / seg.speed
            words = len(t.text.split())
            need = READ_BASE + READ_PER_WORD * words
            if t.style == "caption" and shown < need:
                self._add(
                    "warn",
                    "reading",
                    f"{seg.id}: caption '{t.text}' is shown {shown:.1f}s; "
                    f"{words} words need about {need:.1f}s",
                    segment=seg.id,
                    at=start + seg.to_output(t.start),
                    suggestion="shorten the caption or show it longer (slower speed)",
                )

    def _zoom(self, seg: Segment, canvas: Canvas) -> None:
        if not seg.zooms or seg.source not in self.sizes:
            return
        sw, sh = self.sizes[seg.source]
        cam = Camera(seg, sw, sh, canvas.aspect, canvas.reframe)
        start = self.timeline.starts[seg.id]
        for z, move in zip(sorted(seg.zooms, key=lambda z: z.start), cam.moves, strict=True):
            hold = (move.start + move.end) / 2
            s, _, _ = placement(move.target, canvas.width, canvas.height)
            at = round(start + hold, 2)
            if s > MAX_UPSCALE:
                self._add(
                    "warn",
                    "zoom-soft",
                    f"{seg.id}: zoom magnifies source pixels {s:.1f}x "
                    f"on {canvas.platform.value}; it will look soft",
                    platform=canvas.platform.value,
                    segment=seg.id,
                    at=at,
                    suggestion="widen the zoom box",
                )
            frame = _nearest_ocr(
                self.ocr.get(seg.source, []), z.start + (z.end - z.start) / 2, (z.start, z.end)
            )
            if frame is None:
                continue
            inside = [
                it
                for it in frame["items"]
                if it["box"]["x"] >= z.box.x - 0.01
                and it["box"]["y"] >= z.box.y - 0.01
                and it["box"]["x"] + it["box"]["w"] <= z.box.x + z.box.w + 0.01
                and it["box"]["y"] + it["box"]["h"] <= z.box.y + z.box.h + 0.01
            ]
            if not inside:
                continue
            smallest = min(it["box"]["h"] * sh * s for it in inside)
            want = MIN_TEXT_FRACTION * canvas.height
            if smallest < want:
                self._add(
                    "warn",
                    "zoom-small",
                    f"{seg.id}: text inside the zoom is {smallest:.0f}px "
                    f"tall on {canvas.platform.value} (want ≥ {want:.0f}px)",
                    platform=canvas.platform.value,
                    segment=seg.id,
                    at=at,
                    suggestion="tighten the zoom box around the text that matters",
                )

    def _covering(self, seg: Segment, canvas: Canvas) -> None:
        frames = self.ocr.get(seg.source)
        if not frames or not seg.texts or seg.source not in self.sizes:
            return
        sw, sh = self.sizes[seg.source]
        cam = Camera(seg, sw, sh, canvas.aspect, canvas.reframe)
        start = self.timeline.starts[seg.id]
        for t in seg.texts:
            ov = text_overlay(t.text, t.style, canvas.layout, self.fonts, self.brand, t.position)
            ow, oh = ov.size
            rect = Rect(ov.x, ov.y, ow, oh)
            # the camera moves while a text is up: check its start, middle and end
            pad = min(0.2 * seg.speed, (t.end - t.start) / 4)
            hidden: dict[str, float] = {}
            for src_t in (t.start + pad, (t.start + t.end) / 2, t.end - pad):
                frame = _nearest_ocr(frames, src_t, (t.start, t.end))
                if frame is None:
                    continue
                win = cam.at(seg.to_output(src_t))
                for it in frame["items"]:
                    r = _to_canvas(it["box"], win, sw, sh, canvas.width, canvas.height)
                    area = r.w * r.h
                    if area > 0 and _overlap(rect, r) / area >= COVER_FRACTION:
                        hidden.setdefault(it["text"], src_t)
            if not hidden:
                continue
            first = min(hidden.values())
            shown = ", ".join(f"'{h}'" for h in list(hidden)[:3])
            current = t.position or ("top" if t.style == "title" else "bottom")
            other = "bottom" if current == "top" else "top"
            self._add(
                "warn",
                "covers-text",
                f"{seg.id}: {t.style} '{t.text}' covers on-screen text {shown} "
                f"on {canvas.platform.value}",
                platform=canvas.platform.value,
                segment=seg.id,
                at=round(start + seg.to_output(first), 2),
                suggestion=f"move it to the {other} (position: {other}) or zoom past it",
            )


def review_points(edl: EDL) -> list[tuple[float, str]]:
    """Output instants worth looking at: each banner, each segment's start, every text's
    middle, every zoom hold."""
    points: list[tuple[float, str]] = []
    tl = Timeline.of(edl)
    if edl.start_banner:
        points.append(
            (min(edl.start_banner.duration * 0.7, edl.start_banner.duration - 0.1), "start banner")
        )
    for s in edl.segments:
        t0 = tl.starts[s.id]
        points.append((t0 + min(0.3, s.duration / 2), f"{s.id} start"))
        for x in s.texts:
            points.append((t0 + s.to_output((x.start + x.end) / 2), f"{s.id} {x.style}"))
        for z in s.zooms:
            points.append((t0 + s.to_output((z.start + z.end) / 2), f"{s.id} zoom"))
    if edl.end_banner:
        end0 = edl.duration - edl.end_banner.duration
        points.append((end0 + edl.end_banner.duration * 0.7, "end banner"))
    seen: dict[float, str] = {}
    for t, label in sorted(points):
        key = round(t, 1)
        seen[key] = f"{seen[key]} · {label}" if key in seen else label
    return [(round(t, 2), label) for t, label in sorted(seen.items())]


def review_sheets(
    video: Path,
    points: list[tuple[float, str]],
    out_dir: Path,
    tools: Tools,
    cols: int = 3,
    per_sheet: int = 9,
    thumb: int = 640,
) -> list[Path]:
    """Tile frames of a rendered video at the review points, labeled `mm:ss.s  label`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    font = Fonts.for_brand(Brand()).get(20, bold=True)
    files = []
    for n, k in enumerate(range(0, len(points), per_sheet), 1):
        chunk = points[k : k + per_sheet]
        ims = [frame_at(video, t, thumb, tools) for t, _ in chunk]
        tw, th = max(i.width for i in ims), max(i.height for i in ims)
        label, gap = 32, 10
        c = min(cols, len(ims))
        rows = -(-len(ims) // cols)
        sheet = Image.new(
            "RGB", (c * (tw + gap) + gap, rows * (th + label + gap) + gap), (20, 20, 24)
        )
        d = ImageDraw.Draw(sheet)
        for i, ((t, text), im) in enumerate(zip(chunk, ims, strict=True)):
            x = gap + (i % cols) * (tw + gap)
            y = gap + (i // cols) * (th + label + gap)
            sheet.paste(im, (x, y + label))
            d.text(
                (x + 4, y + 5),
                f"{int(t // 60):02d}:{t % 60:04.1f}  {text}"[:60],
                font=font,
                fill=(255, 220, 90),
            )
        f = out_dir / f"review-{n:02d}.png"
        sheet.save(f)
        files.append(f)
    return files
