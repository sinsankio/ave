import numpy as np
import pytest
from PIL import Image

from ave.config import PRESETS, Brand, Platform
from ave.edl import Banner
from ave.overlays import (
    Fonts,
    Layout,
    Overlay,
    _renders,
    banner_background,
    banner_layers,
    fade_opacity,
    hex_to_rgb,
    in_badge,
    speed_badge,
    text_overlay,
    wrap,
)

BRAND = Brand()
FONTS = Fonts.for_brand(BRAND)


def layout(platform=Platform.LINKEDIN) -> Layout:
    p = PRESETS[platform]
    return Layout.from_preset(p, p.width, p.height)


def test_hex_to_rgb():
    assert hex_to_rgb("#0A66C2") == (10, 102, 194)


def test_wrap_respects_width():
    font = FONTS.get(40, bold=False)
    text = "One click and the report is ready to send to the whole team today"
    lines = wrap(text, font, 400)
    assert len(lines) > 1
    assert " ".join(lines) == text
    assert all(font.getlength(line) <= 400 for line in lines if " " in line)
    assert wrap("Supercalifragilistic", font, 10) == ["Supercalifragilistic"]


def test_fade_opacity():
    assert fade_opacity(0.5, 1, 3) == 0
    assert fade_opacity(1.0, 1, 3) == 0
    assert fade_opacity(1.1, 1, 3) == pytest.approx(0.5)
    assert fade_opacity(2.0, 1, 3) == 1
    assert fade_opacity(3.5, 1, 3) == 0
    assert fade_opacity(1.05, 1, 1.1) == pytest.approx(1.0)  # short span: fade shrinks


def test_overlay_blend_math_and_clipping():
    im = Image.new("RGBA", (4, 2), (255, 0, 0, 128))  # half-transparent red
    ov = Overlay.from_image(im, -2, 0)  # half off the left edge
    frame = np.zeros((4, 4, 3), np.uint8)
    ov.blend(frame)
    assert frame[0, 0].tolist() == pytest.approx([0, 0, 128], abs=1)  # BGR
    assert frame[0, 2].tolist() == [0, 0, 0]
    assert frame[3, 0].tolist() == [0, 0, 0]
    before = frame.copy()
    ov.blend(frame, opacity=0)
    assert (frame == before).all()
    Overlay.from_image(im, 10, 10).blend(frame)  # fully outside: no error


@pytest.mark.parametrize("platform", list(Platform))
def test_text_positions_stay_in_safe_zone(platform):
    lay = layout(platform)
    title = text_overlay("Export to PDF", "title", lay, FONTS, BRAND)
    caption = text_overlay("One click and the report is ready", "caption", lay, FONTS, BRAND)
    tw, th = title.size
    cw, ch = caption.size
    assert title.x == lay.margin_x and title.y == lay.top
    assert caption.y + ch == lay.bottom
    assert caption.x == (lay.width - cw) // 2
    assert caption.x >= lay.margin_x and caption.x + cw <= lay.width - lay.margin_x
    moved = text_overlay("Export to PDF", "title", lay, FONTS, BRAND, position="bottom")
    assert moved.y + moved.size[1] == lay.bottom
    up = text_overlay("x", "caption", lay, FONTS, BRAND, position="top")
    assert up.y == lay.top


def test_long_caption_wraps_inside_canvas():
    lay = layout(Platform.REELS)
    ov = text_overlay("word " * 40, "caption", lay, FONTS, BRAND)
    w, h = ov.size
    assert w <= lay.width - 2 * lay.margin_x
    assert h > FONTS.get(round(lay.unit * 0.04), bold=False).size * 2


def test_speed_badge_top_right():
    lay = layout()
    b = speed_badge(4, lay, FONTS, BRAND)
    assert b.x + b.size[0] == lay.width - lay.margin_x
    assert b.y == lay.top


def test_renders_detects_missing_glyph():
    font = FONTS.get(30, bold=True)
    assert _renders(font, "A")


@pytest.mark.parametrize("platform", list(Platform))
def test_banner_layers_stack_inside_safe_zone(platform):
    lay = layout(platform)
    b = Banner(
        title="Built by Sineth Sankalpa",
        subtitle="x " * 30,
        lines=["linkedin.com/in/sinsankio"],
        cta="Follow for more builds",
    )
    photo = Image.new("RGB", (300, 200), (200, 120, 40))
    layers = banner_layers(b, lay, FONTS, BRAND, photo)
    assert len(layers) == 5  # photo, title, subtitle, linkedin line, cta
    ys = [lyr.overlay.y for lyr in layers]
    assert ys == sorted(ys)  # stacked top to bottom
    assert [lyr.delay for lyr in layers] == sorted(lyr.delay for lyr in layers)
    first, last = layers[0].overlay, layers[-1].overlay
    assert first.y >= lay.top and last.y + last.size[1] <= lay.bottom
    for lyr in layers:
        w = lyr.overlay.size[0]
        assert abs(lyr.overlay.x + w / 2 - lay.width / 2) <= 1  # centered
    assert layers[0].overlay.size[0] == layers[0].overlay.size[1]  # round avatar


def test_banner_layer_reveal():
    lay = layout()
    (title,) = banner_layers(Banner(title="Hello"), lay, FONTS, BRAND)
    bg = banner_background(lay, BRAND)
    before = bg.copy()
    title.blend(before, 0.0)
    assert (before == bg).all()  # not yet revealed
    done = bg.copy()
    title.blend(done, title.delay + 1)
    assert (done != bg).any()


def test_banner_background_with_hero():
    lay = layout(Platform.REELS)
    hero = np.zeros((400, 800, 3), np.uint8)
    hero[:, 400:] = 255
    bg = banner_background(lay, BRAND, hero)
    assert bg.shape == (lay.height, lay.width, 3)
    assert bg[: lay.height // 10].mean() > bg[-lay.height // 10 :].mean() * 0.8
    plain = banner_background(lay, BRAND)
    assert plain.shape == bg.shape


def test_in_badge():
    im = in_badge(40)
    assert im.size == (40, 40) and im.getpixel((20, 2))[3] == 255
