"""Text cards, speed badge and banners: rendered with Pillow, blended onto BGR frames."""

from __future__ import annotations

import functools
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ave.config import Brand, PlatformPreset
from ave.edl import Banner, TextStyle

FADE = 0.2  # output seconds for overlay fade in/out
_FONT_CANDIDATES = {
    "bold": ["Noto Sans:bold", "DejaVu Sans:bold", "Ubuntu Sans:bold", "sans:bold"],
    "regular": ["Noto Sans", "DejaVu Sans", "Ubuntu Sans", "sans"],
}

Font = ImageFont.FreeTypeFont | ImageFont.ImageFont


@functools.cache
def _system_font(weight: str) -> str | None:
    if shutil.which("fc-match") is None:
        return None
    for pattern in _FONT_CANDIDATES[weight]:
        try:
            out = subprocess.run(
                ["fc-match", "-f", "%{file}", pattern], capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out and Path(out).is_file() and out.lower().endswith((".ttf", ".otf", ".ttc")):
            return out
    return None


@dataclass(frozen=True)
class Fonts:
    bold: str | None
    regular: str | None

    @classmethod
    def for_brand(cls, brand: Brand) -> Fonts:
        bold = str(brand.font) if brand.font else _system_font("bold")
        regular = str(brand.font_regular) if brand.font_regular else _system_font("regular")
        return cls(bold=bold, regular=regular or bold)

    def get(self, size: int, bold: bool) -> Font:
        return _load_font(self.bold if bold else self.regular, size)


@functools.cache
def _load_font(path: str | None, size: int) -> Font:
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def wrap(text: str, font: Font, max_width: float) -> list[str]:
    """Greedy word wrap by rendered pixel width. A single over-long word gets its own line."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            trial = f"{line} {word}".strip()
            if not line or font.getlength(trial) <= max_width:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


@dataclass(frozen=True)
class Layout:
    """Canvas geometry and safe zones for one platform."""

    width: int
    height: int
    safe_top: float
    safe_bottom: float
    safe_side: float

    @classmethod
    def from_preset(cls, preset: PlatformPreset, width: int, height: int) -> Layout:
        return cls(width, height, preset.safe_top, preset.safe_bottom, preset.safe_side)

    @property
    def unit(self) -> int:
        return min(self.width, self.height)

    @property
    def margin_x(self) -> int:
        return round(self.width * self.safe_side)

    @property
    def top(self) -> int:
        return round(self.height * self.safe_top)

    @property
    def bottom(self) -> int:
        return round(self.height * (1 - self.safe_bottom))


@dataclass
class Overlay:
    """A premultiplied BGRA sprite placed at (x, y) on the canvas."""

    x: int
    y: int
    bgr: np.ndarray  # float32 HxWx3, premultiplied by alpha
    alpha: np.ndarray  # float32 HxWx1 in 0..1

    @classmethod
    def from_image(cls, im: Image.Image, x: int, y: int) -> Overlay:
        rgba = np.asarray(im.convert("RGBA"), dtype=np.float32) / 255.0
        alpha = rgba[..., 3:4]
        bgr = rgba[..., 2::-1] * alpha * 255.0
        return cls(x, y, bgr.astype(np.float32), alpha.astype(np.float32))

    @property
    def size(self) -> tuple[int, int]:
        return self.bgr.shape[1], self.bgr.shape[0]

    def blend(self, frame: np.ndarray, opacity: float = 1.0) -> None:
        """Alpha-blend onto a BGR uint8 frame in place, clipped to the frame bounds."""
        if opacity <= 0:
            return
        fh, fw = frame.shape[:2]
        w, h = self.size
        x0, y0 = max(self.x, 0), max(self.y, 0)
        x1, y1 = min(self.x + w, fw), min(self.y + h, fh)
        if x0 >= x1 or y0 >= y1:
            return
        sx, sy = x0 - self.x, y0 - self.y
        a = self.alpha[sy : sy + y1 - y0, sx : sx + x1 - x0] * opacity
        fg = self.bgr[sy : sy + y1 - y0, sx : sx + x1 - x0] * opacity
        region = frame[y0:y1, x0:x1].astype(np.float32)
        frame[y0:y1, x0:x1] = np.clip(region * (1 - a) + fg, 0, 255).astype(np.uint8)


def fade_opacity(t: float, start: float, end: float, fade: float = FADE) -> float:
    if t < start or t > end:
        return 0.0
    fade = min(fade, (end - start) / 2)
    if fade <= 0:
        return 1.0
    return min(1.0, (t - start) / fade, (end - t) / fade)


def _card(
    lines: list[str], font: Font, text_rgb, bg_rgba, pad_x: int, pad_y: int, gap: int, radius: int
) -> Image.Image:
    widths = [font.getlength(line) for line in lines]
    ascent, descent = font.getmetrics() if hasattr(font, "getmetrics") else (font.size, 0)
    line_h = ascent + descent
    w = int(max(widths) + 2 * pad_x)
    h = int(len(lines) * line_h + (len(lines) - 1) * gap + 2 * pad_y)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=bg_rgba)
    y = pad_y
    for line in lines:
        d.text((pad_x, y), line, font=font, fill=text_rgb)
        y += line_h + gap
    return im


TextPosition = Literal["top", "bottom"]


def text_overlay(
    text: str,
    style: TextStyle,
    layout: Layout,
    fonts: Fonts,
    brand: Brand,
    position: TextPosition | None = None,
) -> Overlay:
    """Title: brand-colored card, left-aligned. Caption: dark card, centered.

    Default positions: title top, caption bottom. Both stay inside the platform safe zone.
    """
    u = layout.unit
    max_w = layout.width - 2 * layout.margin_x
    if style == "title":
        font = fonts.get(round(u * 0.052), bold=True)
        pad_x, pad_y = round(u * 0.028), round(u * 0.016)
        lines = wrap(text, font, max_w * 0.8 - 2 * pad_x)
        im = _card(
            lines,
            font,
            (255, 255, 255),
            (*hex_to_rgb(brand.primary_color), 235),
            pad_x,
            pad_y,
            round(u * 0.008),
            round(u * 0.018),
        )
        x = layout.margin_x
        pos = position or "top"
    else:
        font = fonts.get(round(u * 0.04), bold=False)
        pad_x, pad_y = round(u * 0.026), round(u * 0.016)
        lines = wrap(text, font, max_w * 0.9 - 2 * pad_x)
        im = _card(
            lines,
            font,
            (255, 255, 255),
            (12, 12, 16, 190),
            pad_x,
            pad_y,
            round(u * 0.008),
            round(u * 0.016),
        )
        x = (layout.width - im.width) // 2
        pos = position or "bottom"
    y = layout.top if pos == "top" else layout.bottom - im.height
    return Overlay.from_image(im, x, y)


def speed_badge(speed: float, layout: Layout, fonts: Fonts, brand: Brand) -> Overlay:
    """Pill with "4×" and two drawn fast-forward arrows (no font glyph needed)."""
    u = layout.unit
    font = fonts.get(round(u * 0.032), bold=True)
    label = f"{speed:g}×" if _renders(font, "×") else f"{speed:g}x"
    pad = round(u * 0.014)
    ascent, descent = font.getmetrics() if hasattr(font, "getmetrics") else (font.size, 0)
    text_h = ascent + descent
    tri = round(text_h * 0.55)  # arrow height
    gap = round(pad * 0.8)
    w = int(2 * pad * 2 + font.getlength(label) + gap + 2 * tri * 0.8)
    h = text_h + 2 * pad
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle(
        (0, 0, w - 1, h - 1), radius=h // 2, fill=(*hex_to_rgb(brand.primary_color), 220)
    )
    d.text((pad * 2, pad), label, font=font, fill=(255, 255, 255))
    x0 = pad * 2 + font.getlength(label) + gap
    cy = h / 2
    for k in range(2):
        ax = x0 + k * tri * 0.8
        d.polygon(
            [(ax, cy - tri / 2), (ax + tri * 0.8, cy), (ax, cy + tri / 2)], fill=(255, 255, 255)
        )
    return Overlay.from_image(im, layout.width - layout.margin_x - im.width, layout.top)


def _renders(font: Font, ch: str) -> bool:
    """False when the font would draw its missing-glyph box for ch."""
    try:
        missing = bytes(font.getmask("\uffff"))
        return bytes(font.getmask(ch)) != missing
    except Exception:
        return False


BANNER_REVEAL = 0.45  # seconds for one text layer to fade and rise in
BANNER_STAGGER = 0.18  # delay between consecutive layers


@dataclass
class BannerLayer:
    overlay: Overlay
    delay: float  # seconds after the banner starts
    rise: int  # pixels the layer travels upward while revealing

    def blend(self, frame: np.ndarray, t: float) -> None:
        p = min(1.0, max(0.0, (t - self.delay) / BANNER_REVEAL))
        if p <= 0:
            return
        eased = 1 - (1 - p) ** 3  # ease-out cubic
        dy = round(self.rise * (1 - eased))
        Overlay(self.overlay.x, self.overlay.y + dy, self.overlay.bgr, self.overlay.alpha).blend(
            frame, eased
        )


def _line_height(font: Font) -> int:
    a, b = font.getmetrics() if hasattr(font, "getmetrics") else (font.size, 0)
    return a + b


def _text_block(lines: list[str], font: Font, color, max_w: int) -> Image.Image:
    """Centered multi-line text on a transparent sprite."""
    lh = _line_height(font)
    w = int(min(max_w, max(font.getlength(line) for line in lines))) + 4
    im = Image.new("RGBA", (w, lh * len(lines)), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for k, line in enumerate(lines):
        d.text(((w - font.getlength(line)) / 2, k * lh), line, font=font, fill=color)
    return im


def in_badge(size: int) -> Image.Image:
    """A small rounded square with "in", marking the LinkedIn line."""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 5, fill=(255, 255, 255, 255))
    font = _load_font(_system_font("bold"), round(size * 0.62))
    w = font.getlength("in")
    asc, desc = font.getmetrics() if hasattr(font, "getmetrics") else (size, 0)
    d.text(
        ((size - w) / 2, (size - asc - desc) / 2 - size * 0.02),
        "in",
        font=font,
        fill=(10, 102, 194),
    )
    return im


def _avatar(photo: Image.Image, size: int) -> Image.Image:
    sq = min(photo.size)
    left, top = (photo.width - sq) // 2, (photo.height - sq) // 2
    im = photo.convert("RGB").crop((left, top, left + sq, top + sq)).resize((size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    ring = ImageDraw.Draw(out)
    ring.ellipse((1, 1, size - 2, size - 2), outline=(255, 255, 255, 230), width=max(2, size // 40))
    return out


def _luminance(rgb: tuple[int, int, int]) -> float:
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def banner_layers(
    banner: Banner,
    layout: Layout,
    fonts: Fonts,
    brand: Brand,
    photo: Image.Image | None = None,
) -> list[BannerLayer]:
    """Title, subtitle, lines (LinkedIn line gets an "in" badge) and CTA pill, stacked and
    centered inside the safe zone, each revealed a little after the previous one."""
    u = layout.unit
    max_w = layout.width - 2 * max(layout.margin_x, round(layout.width * 0.08))
    accent = hex_to_rgb(brand.accent_color)
    sprites: list[tuple[Image.Image, int]] = []  # (image, gap above)
    if photo is not None:
        sprites.append((_avatar(photo, round(u * 0.2)), 0))
    title_font = fonts.get(round(u * 0.085), bold=True)
    title = _text_block(wrap(banner.title, title_font, max_w), title_font, (255, 255, 255), max_w)
    sprites.append((title, round(u * 0.03)))
    if banner.subtitle:
        f = fonts.get(round(u * 0.045), bold=False)
        sprites.append(
            (_text_block(wrap(banner.subtitle, f, max_w), f, accent, max_w), round(u * 0.03))
        )
    for line in banner.lines:
        f = fonts.get(round(u * 0.042), bold=True)
        text = _text_block(wrap(line, f, max_w), f, (255, 255, 255), max_w)
        if "linkedin" in line.lower():
            badge = in_badge(round(_line_height(f) * 0.95))
            pad = round(u * 0.018)
            row = Image.new(
                "RGBA", (badge.width + pad + text.width, max(badge.height, text.height))
            )
            row.alpha_composite(badge, (0, (row.height - badge.height) // 2))
            row.alpha_composite(text, (badge.width + pad, (row.height - text.height) // 2))
            text = row
        sprites.append((text, round(u * 0.025)))
    if banner.cta:
        f = fonts.get(round(u * 0.036), bold=True)
        ink = (17, 24, 39) if _luminance(accent) > 140 else (255, 255, 255)
        pill = _card(
            [banner.cta],
            f,
            ink,
            (*accent, 240),
            round(u * 0.035),
            round(u * 0.016),
            0,
            round(u * 0.05),
        )
        sprites.append((pill, round(u * 0.045)))

    gaps = [g for _, g in sprites][1:]
    total = sum(im.height for im, _ in sprites) + sum(gaps)
    y = round((layout.top + layout.bottom) / 2 - total / 2)
    layers = []
    for k, (im, gap) in enumerate(sprites):
        if k:
            y += gap
        x = (layout.width - im.width) // 2
        layers.append(
            BannerLayer(Overlay.from_image(im, x, y), 0.15 + k * BANNER_STAGGER, round(u * 0.035))
        )
        y += im.height
    return layers


def banner_background(
    layout: Layout, brand: Brand, hero_bgr: np.ndarray | None = None
) -> np.ndarray:
    """BGR background: a blurred, brand-tinted recording frame, or a brand gradient."""
    w, h, u = layout.width, layout.height, layout.unit
    primary = np.array(hex_to_rgb(brand.primary_color)[::-1], dtype=np.float32)  # BGR
    ramp = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    if hero_bgr is None:
        grad = primary * (1 - ramp) + primary * 0.35 * ramp
        return np.repeat(grad, w, axis=1).astype(np.uint8)
    import cv2

    fh, fw = hero_bgr.shape[:2]
    scale = max(w / fw, h / fh)  # cover the canvas
    rw, rh = max(w, round(fw * scale)), max(h, round(fh * scale))
    big = cv2.resize(hero_bgr, (rw, rh), interpolation=cv2.INTER_AREA)
    x0, y0 = (rw - w) // 2, (rh - h) // 2
    img = big[y0 : y0 + h, x0 : x0 + w].astype(np.float32)
    img = cv2.GaussianBlur(img, (0, 0), max(2.0, u * 0.012))
    tint = primary * (1 - ramp) * 0.55 + np.zeros(3, np.float32) * ramp  # brand at top, black below
    alpha = 0.55 + 0.25 * ramp  # darker toward the bottom so text stays readable
    return np.clip(img * (1 - alpha) + tint * alpha, 0, 255).astype(np.uint8)
