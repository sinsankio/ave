"""Virtual camera: which source rectangle is on screen at each output instant.

All rects are in decoded-source pixels. Zoom moves interpolate centers linearly and sizes
in log space, so a zoom feels like a constant-speed push rather than a lurch at the end.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ave.edl import MAX_ZOOM, Box, Segment


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @classmethod
    def centered(cls, cx: float, cy: float, w: float, h: float) -> Rect:
        return cls(cx - w / 2, cy - h / 2, w, h)


def smoothstep(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return u * u * (3 - 2 * u)


def lerp_rect(a: Rect, b: Rect, u: float) -> Rect:
    w = math.exp(math.log(a.w) + (math.log(b.w) - math.log(a.w)) * u)
    h = math.exp(math.log(a.h) + (math.log(b.h) - math.log(a.h)) * u)
    return Rect.centered(a.cx + (b.cx - a.cx) * u, a.cy + (b.cy - a.cy) * u, w, h)


def clamp_into(r: Rect, src_w: float, src_h: float) -> Rect:
    """Shift (and if needed shrink) r so it lies inside the source frame."""
    w, h = min(r.w, src_w), min(r.h, src_h)
    x = min(max(r.cx - w / 2, 0.0), src_w - w)
    y = min(max(r.cy - h / 2, 0.0), src_h - h)
    return Rect(x, y, w, h)


def base_window(src_w: int, src_h: int, aspect: float, reframe: str, focus_x: float) -> Rect:
    """Camera rect when not zoomed. fit: whole frame; fill: largest canvas-aspect crop."""
    if reframe == "fit":
        return Rect(0, 0, src_w, src_h)
    if src_w / src_h > aspect:
        w, h = src_h * aspect, float(src_h)
    else:
        w, h = float(src_w), src_w / aspect
    return clamp_into(Rect.centered(focus_x * src_w, src_h / 2, w, h), src_w, src_h)


def zoom_window(
    box: Box, base: Rect, src_w: int, src_h: int, aspect: float, reframe: str = "fit"
) -> Rect:
    """Rect for a zoom box: grown toward the canvas aspect, capped at MAX_ZOOM, kept in frame.

    fit keeps the whole box visible (letterboxing if the frame can't supply the aspect);
    fill keeps the canvas aspect exactly, even if that trims a very wide/tall box.
    """
    w, h = box.w * src_w, box.h * src_h
    if w / h < aspect:
        w = h * aspect
    else:
        h = w / aspect
    # never magnify more than MAX_ZOOM relative to the unzoomed view
    scale = max(1.0, (base.w / MAX_ZOOM) / w, (base.h / MAX_ZOOM) / h)
    w, h = w * scale, h * scale
    if reframe == "fit":
        w, h = min(w, src_w), min(h, src_h)
    else:
        shrink = min(1.0, base.w / w, base.h / h)
        w, h = w * shrink, h * shrink
    cx = (box.x + box.w / 2) * src_w
    cy = (box.y + box.h / 2) * src_h
    return clamp_into(Rect.centered(cx, cy, w, h), src_w, src_h)


@dataclass(frozen=True)
class ZoomMove:
    start: float  # output seconds, segment-local
    end: float
    ease: float
    target: Rect


class Camera:
    def __init__(self, seg: Segment, src_w: int, src_h: int, aspect: float, reframe: str):
        self.base = base_window(src_w, src_h, aspect, reframe, seg.focus_x)
        self.moves = [
            ZoomMove(
                start=seg.to_output(z.start),
                end=seg.to_output(z.end),
                # an ease longer than half the zoom would never reach the target
                ease=min(z.ease, (z.end - z.start) / seg.speed / 2),
                target=zoom_window(z.box, self.base, src_w, src_h, aspect, reframe),
            )
            for z in sorted(seg.zooms, key=lambda z: z.start)
        ]

    def at(self, t: float) -> Rect:
        for m in self.moves:
            if m.start <= t <= m.end:
                if m.ease > 0 and t < m.start + m.ease:
                    return lerp_rect(self.base, m.target, smoothstep((t - m.start) / m.ease))
                if m.ease > 0 and t > m.end - m.ease:
                    return lerp_rect(
                        m.target, self.base, smoothstep((t - (m.end - m.ease)) / m.ease)
                    )
                return m.target
        return self.base


def placement(window: Rect, canvas_w: int, canvas_h: int) -> tuple[float, float, float]:
    """Scale and offset that fit `window` inside the canvas, centered: (scale, ox, oy)."""
    s = min(canvas_w / window.w, canvas_h / window.h)
    return s, (canvas_w - window.w * s) / 2, (canvas_h - window.h * s) / 2


def affine(window: Rect, canvas_w: int, canvas_h: int) -> np.ndarray:
    s, ox, oy = placement(window, canvas_w, canvas_h)
    return np.array([[s, 0, ox - window.x * s], [0, s, oy - window.y * s]], dtype=np.float32)


def covers_canvas(window: Rect, canvas_w: int, canvas_h: int) -> bool:
    s, ox, oy = placement(window, canvas_w, canvas_h)
    return ox <= 0.5 and oy <= 0.5
