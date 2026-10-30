import numpy as np
import pytest

from ave.camera import (
    Camera,
    Rect,
    affine,
    base_window,
    clamp_into,
    covers_canvas,
    lerp_rect,
    smoothstep,
    zoom_window,
)
from ave.edl import MAX_ZOOM, Box, Segment

W, H = 1920, 1080
LANDSCAPE, PORTRAIT = 16 / 9, 9 / 16


def approx_rect(a: Rect, b: Rect, tol: float = 1e-6) -> bool:
    return all(
        abs(x - y) <= tol for x, y in zip((a.x, a.y, a.w, a.h), (b.x, b.y, b.w, b.h), strict=True)
    )


def inside(r: Rect, w=W, h=H) -> bool:
    eps = 1e-6
    return r.x >= -eps and r.y >= -eps and r.x + r.w <= w + eps and r.y + r.h <= h + eps


def test_smoothstep():
    assert smoothstep(-1) == 0 and smoothstep(0) == 0
    assert smoothstep(0.5) == 0.5
    assert smoothstep(1) == 1 and smoothstep(2) == 1


def test_lerp_rect_endpoints_and_log_size():
    a, b = Rect(0, 0, 1600, 900), Rect(400, 225, 400, 225)
    assert approx_rect(lerp_rect(a, b, 0), a)
    assert approx_rect(lerp_rect(a, b, 1), b)
    mid = lerp_rect(a, b, 0.5)
    assert mid.w == pytest.approx(800)  # geometric mean of 1600 and 400


def test_clamp_into_shifts_and_shrinks():
    assert clamp_into(Rect(-100, -50, 400, 200), W, H) == Rect(0, 0, 400, 200)
    assert clamp_into(Rect(1800, 1000, 400, 200), W, H) == Rect(1520, 880, 400, 200)
    big = clamp_into(Rect(0, 0, 4000, 4000), W, H)
    assert (big.w, big.h) == (W, H)


def test_base_window():
    assert base_window(W, H, PORTRAIT, "fit", 0.5) == Rect(0, 0, W, H)
    fill = base_window(W, H, PORTRAIT, "fill", 0.5)
    assert fill.h == H and fill.w == pytest.approx(H * PORTRAIT)
    assert fill.cx == pytest.approx(W / 2)
    left = base_window(W, H, PORTRAIT, "fill", 0.0)
    assert left.x == 0  # focus at the edge clamps inside the frame
    same = base_window(W, H, LANDSCAPE, "fill", 0.5)
    assert approx_rect(same, Rect(0, 0, W, H))


@pytest.mark.parametrize("reframe", ["fit", "fill"])
@pytest.mark.parametrize("aspect", [LANDSCAPE, PORTRAIT, 4 / 5])
@pytest.mark.parametrize(
    "box", [Box(x=0.6, y=0.1, w=0.3, h=0.2), Box(x=0, y=0, w=0.05, h=0.05), Box(x=0, y=0, w=1, h=1)]
)
def test_zoom_window_properties(reframe, aspect, box):
    base = base_window(W, H, aspect, reframe, 0.5)
    r = zoom_window(box, base, W, H, aspect, reframe)
    assert inside(r)
    assert base.w / r.w <= MAX_ZOOM + 1e-6  # never magnifies past the cap
    if reframe == "fill":
        assert r.w / r.h == pytest.approx(aspect, rel=1e-3)


def test_zoom_window_fit_keeps_wide_box_visible_on_portrait():
    box = Box(x=0.6, y=0.1, w=0.3, h=0.2)
    r = zoom_window(box, Rect(0, 0, W, H), W, H, PORTRAIT, "fit")
    assert r.x <= box.x * W + 1e-6 and r.x + r.w >= (box.x + box.w) * W - 1e-6


def test_zoom_window_centers_on_box():
    box = Box(x=0.4, y=0.4, w=0.2, h=0.2)
    r = zoom_window(box, Rect(0, 0, W, H), W, H, LANDSCAPE, "fit")
    assert r.cx == pytest.approx(W / 2) and r.cy == pytest.approx(H / 2)


def test_camera_timeline():
    s = Segment.model_validate({
        "id": "s", "start": 10, "end": 20, "speed": 2,
        "zooms": [{"start": 12, "end": 18, "ease": 0.5,
                   "box": {"x": .5, "y": .5, "w": .25, "h": .25}}],
    })  # fmt: skip
    cam = Camera(s, W, H, LANDSCAPE, "fit")
    move = cam.moves[0]
    assert (move.start, move.end) == (1.0, 4.0)  # source 12..18 at 2x
    assert cam.at(0.5) == cam.base
    assert cam.at(2.5) == move.target
    mid_in = cam.at(1.25)
    assert move.target.w < mid_in.w < cam.base.w
    assert cam.at(4.5) == cam.base


def test_camera_ease_clamped_for_short_zoom():
    s = Segment.model_validate({
        "id": "s", "start": 0, "end": 10,
        "zooms": [{"start": 1, "end": 1.4, "ease": 2, "box": {"x": .5, "y": .5, "w": .2, "h": .2}}],
    })  # fmt: skip
    cam = Camera(s, W, H, LANDSCAPE, "fit")
    assert cam.moves[0].ease == pytest.approx(0.2)
    assert approx_rect(cam.at(1.2), cam.moves[0].target, 1e-3)


def test_affine_maps_window_to_canvas():
    win = Rect(480, 270, 960, 540)
    m = affine(win, 1920, 1080)
    corners = np.array([[win.x, win.y, 1], [win.x + win.w, win.y + win.h, 1]]).T
    mapped = (m @ corners).T
    assert np.allclose(mapped, [[0, 0], [1920, 1080]])
    assert covers_canvas(win, 1920, 1080)


def test_fit_on_portrait_letterboxes():
    win = Rect(0, 0, W, H)
    assert not covers_canvas(win, 1080, 1920)
    m = affine(win, 1080, 1920)
    top_left = m @ np.array([0, 0, 1])
    assert top_left[0] == pytest.approx(0) and top_left[1] > 0
