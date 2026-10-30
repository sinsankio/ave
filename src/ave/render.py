"""EDL -> video. Each segment and banner becomes a cached clip; clips are then concatenated.

Per clip: ffmpeg decodes the source range already retimed to the output frame rate, Python
composes every frame (virtual camera, background, overlays, fades) and pipes raw BGR frames
into an ffmpeg encoder that also muxes the retimed audio (or silence).
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ave.camera import Camera, affine, base_window, covers_canvas
from ave.config import PRESETS, Brand, Platform
from ave.edl import EDL, Banner, EDLError, Segment, check_against_media, resolve_sources
from ave.media import (
    AUDIO_ARGS,
    CLIP_ARGS,
    EncoderName,
    MediaError,
    MediaInfo,
    Tools,
    grab_frame,
    probe,
    run_ffmpeg,
    video_encoder_args,
)
from ave.observability import Tracer
from ave.overlays import (
    Fonts,
    Layout,
    Overlay,
    banner_background,
    banner_layers,
    fade_opacity,
    hex_to_rgb,
    speed_badge,
    text_overlay,
)

RENDER_VERSION = "3"  # bump when output for the same inputs changes, to invalidate caches
BANNER_FADE = 0.4  # fade to black at the end of a banner
BANNER_FADE_IN = 0.25
BANNER_PUSH = 0.03  # banner slowly scales up by this fraction over its duration
LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"


class RenderError(Exception):
    """The EDL cannot be rendered as asked (bad sources, over the duration cap, ffmpeg failure)."""


def _even(n: float) -> int:
    return max(2, int(round(n)) // 2 * 2)


@dataclass(frozen=True)
class Canvas:
    platform: Platform
    width: int
    height: int
    fps: int
    layout: Layout
    reframe: str
    background: str
    proxy: bool

    @property
    def aspect(self) -> float:
        return self.width / self.height

    @classmethod
    def for_platform(cls, platform: Platform, edl: EDL, proxy: bool) -> Canvas:
        preset = PRESETS[platform]
        scale = 0.5 if proxy else 1.0
        w, h = _even(preset.width * scale), _even(preset.height * scale)
        return cls(platform, w, h, edl.fps, Layout.from_preset(preset, w, h),
                   edl.reframe, edl.background, proxy)  # fmt: skip


@dataclass
class Renderer:
    tools: Tools
    encoder: EncoderName
    brand: Brand
    cache_dir: Path
    tracer: Tracer
    fonts: Fonts = field(init=False)

    def __post_init__(self) -> None:
        self.fonts = Fonts.for_brand(self.brand)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # --- caching ---------------------------------------------------------------------

    def _key(self, kind: str, spec: dict, canvas: Canvas, source: Path | None = None) -> str:
        src = None
        if source is not None:
            st = source.stat()
            src = [str(source.resolve()), st.st_size, st.st_mtime_ns]
        payload = {
            "v": RENDER_VERSION,
            "kind": kind,
            "spec": spec,
            "src": src,
            "canvas": [canvas.width, canvas.height, canvas.fps, canvas.reframe,
                       canvas.background, canvas.proxy, canvas.layout.__dict__],
            "encoder": self.encoder,
            "brand": self.brand.model_dump(mode="json"),
            "fonts": [self.fonts.bold, self.fonts.regular],
        }  # fmt: skip
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:20]

    # --- clips -----------------------------------------------------------------------

    def segment_clip(
        self, seg: Segment, src: Path, info: MediaInfo, canvas: Canvas
    ) -> tuple[Path, bool]:
        # planning notes do not change pixels, so they stay out of the cache key
        spec = seg.model_dump(mode="json", exclude={"feature", "note"})
        key = self._key("segment", spec, canvas, src)
        out = self.cache_dir / f"seg-{seg.id}-{key}.mp4"
        if out.is_file():
            return out, True
        inputs = {
            "segment": seg.model_dump(mode="json"),
            "canvas": f"{canvas.width}x{canvas.height}",
        }
        with self.tracer.span(f"tool:render_segment[{seg.id}]", kind="tool", inputs=inputs) as sp:
            n = max(1, round(seg.duration * canvas.fps))
            frames = self._segment_frames(seg, src, info, canvas, n)
            if seg.keeps_audio and info.has_audio:
                audio_in = [
                    "-ss",
                    f"{seg.start:.3f}",
                    "-t",
                    f"{seg.source_duration:.3f}",
                    "-i",
                    str(src),
                ]
                audio_filter = f"{atempo_chain(seg.speed)},aresample=48000,apad"
            else:
                audio_in, audio_filter = _silence(n / canvas.fps), "apad"
            self._encode(frames, n, canvas, audio_in, audio_filter, out)
            sp.set_outputs(frames=n, file=out.name)
        return out, False

    def banner_clip(
        self, kind: str, banner: Banner, canvas: Canvas, hero: tuple[Path, MediaInfo] | None = None
    ) -> tuple[Path, bool]:
        photo = self.brand.photo if kind == "end" and self.brand.photo else None
        spec = banner.model_dump(mode="json") | {"photo": _stat(photo)}
        key = self._key(f"banner-{kind}", spec, canvas, hero[0] if hero else None)
        out = self.cache_dir / f"banner-{kind}-{key}.mp4"
        if out.is_file():
            return out, True
        inputs = banner.model_dump(mode="json")
        with self.tracer.span(f"tool:render_banner[{kind}]", kind="tool", inputs=inputs) as sp:
            n = max(1, round(banner.duration * canvas.fps))
            hero_bgr = (
                grab_frame(hero[1], banner.hero.at, self.tools) if hero and banner.hero else None
            )
            frames = self._banner_frames(banner, canvas, n, hero_bgr, photo)
            self._encode(frames, n, canvas, _silence(n / canvas.fps), "apad", out)
            sp.set_outputs(frames=n, file=out.name, hero=hero_bgr is not None)
        return out, False

    # --- frame generation -----------------------------------------------------------

    def _segment_frames(
        self, seg: Segment, src: Path, info: MediaInfo, canvas: Canvas, n: int
    ) -> Iterator[np.ndarray]:
        scale = min(1.0, 0.5 if canvas.proxy else 1.0)
        dw, dh = _even(info.width * scale), _even(info.height * scale)
        cam = Camera(seg, dw, dh, canvas.aspect, canvas.reframe)
        overlays: list[tuple[Overlay, float, float]] = [
            (text_overlay(t.text, t.style, canvas.layout, self.fonts, self.brand, t.position),
             seg.to_output(t.start), seg.to_output(t.end))
            for t in seg.texts
        ]  # fmt: skip
        if seg.shows_badge:
            badge = speed_badge(seg.speed, canvas.layout, self.fonts, self.brand)
            overlays.append((badge, 0.0, seg.duration))
        bg_rect = base_window(dw, dh, canvas.aspect, "fill", 0.5)
        bg_color = np.array(hex_to_rgb(self.brand.primary_color)[::-1], dtype=np.float32) * 0.35

        vf = f"setpts=(PTS-STARTPTS)/{seg.speed},fps={canvas.fps},scale={dw}:{dh}"
        cmd = [self.tools.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
               "-ss", f"{seg.start:.3f}", "-t", f"{seg.source_duration:.3f}", "-i", str(src),
               "-an", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]  # fmt: skip
        frame_bytes = dw * dh * 3
        W, H = canvas.width, canvas.height
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
            last: np.ndarray | None = None
            try:
                for i in range(n):
                    buf = proc.stdout.read(frame_bytes) if proc.stdout else b""
                    if len(buf) == frame_bytes:
                        last = np.frombuffer(buf, np.uint8).reshape(dh, dw, 3)
                    elif last is None:
                        break  # decoder produced nothing at all
                    # short reads: hold the last frame so the clip keeps its exact length
                    t = i / canvas.fps
                    win = cam.at(t)
                    m = affine(win, W, H)
                    if covers_canvas(win, W, H):
                        out = cv2.warpAffine(last, m, (W, H), flags=cv2.INTER_LINEAR,
                                             borderMode=cv2.BORDER_REPLICATE)  # fmt: skip
                    else:
                        out = self._background(last, bg_rect, bg_color, canvas)
                        cv2.warpAffine(last, m, (W, H), dst=out, flags=cv2.INTER_LINEAR,
                                       borderMode=cv2.BORDER_TRANSPARENT)  # fmt: skip
                    for ov, a, b in overlays:
                        ov.blend(out, fade_opacity(t, a, b))
                    fade = _fade(t, seg.duration, seg.fade_in, seg.fade_out)
                    if fade < 1:
                        out = (out.astype(np.float32) * fade).astype(np.uint8)
                    yield out
            finally:
                if proc.stdout:
                    proc.stdout.close()
                rc = proc.wait()
            if last is None:
                err.seek(0)
                msg = err.read().decode(errors="replace").strip()[-500:]
                raise RenderError(f"segment {seg.id}: decoder produced no frames (rc={rc}) {msg}")

    def _background(self, frame: np.ndarray, rect, color: np.ndarray, canvas: Canvas) -> np.ndarray:
        W, H = canvas.width, canvas.height
        if canvas.background == "color":
            return np.full((H, W, 3), color.astype(np.uint8), np.uint8)
        x, y, w, h = (int(round(v)) for v in (rect.x, rect.y, rect.w, rect.h))
        small = cv2.resize(frame[y : y + h, x : x + w], (max(8, W // 12), max(8, H // 12)),
                           interpolation=cv2.INTER_AREA)  # fmt: skip
        small = cv2.GaussianBlur(small, (0, 0), 3)
        big = cv2.resize(small, (W, H), interpolation=cv2.INTER_LINEAR)
        return (big.astype(np.float32) * 0.55).astype(np.uint8)

    def _banner_frames(
        self,
        banner: Banner,
        canvas: Canvas,
        n: int,
        hero_bgr: np.ndarray | None = None,
        photo: Path | None = None,
    ) -> Iterator[np.ndarray]:
        photo_im = None
        if photo is not None:
            from PIL import Image

            with Image.open(photo) as im:
                photo_im = im.convert("RGB")
        layers = banner_layers(banner, canvas.layout, self.fonts, self.brand, photo_im)
        base = banner_background(canvas.layout, self.brand, hero_bgr)
        W, H = canvas.width, canvas.height
        for i in range(n):
            t = i / canvas.fps
            s = 1 + BANNER_PUSH * (i / max(1, n - 1))  # slow push-in on the background only
            m = np.array([[s, 0, (1 - s) * W / 2], [0, s, (1 - s) * H / 2]], dtype=np.float32)
            out = cv2.warpAffine(
                base, m, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
            )
            for layer in layers:
                layer.blend(out, t)
            fade = _fade(t, banner.duration, BANNER_FADE_IN, BANNER_FADE)
            if fade < 1:
                out = (out.astype(np.float32) * fade).astype(np.uint8)
            yield out

    # --- encoding --------------------------------------------------------------------

    def _encode(
        self,
        frames: Iterator[np.ndarray],
        n: int,
        canvas: Canvas,
        audio_in: list[str],
        audio_filter: str,
        out: Path,
    ) -> None:
        duration = n / canvas.fps
        tmp = out.with_name(f".{out.stem}.{os.getpid()}.tmp.mp4")
        cmd = [self.tools.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{canvas.width}x{canvas.height}",
               "-r", str(canvas.fps), "-i", "-", *audio_in,
               "-map", "0:v:0", "-map", "1:a:0", "-af", audio_filter,
               *video_encoder_args(self.encoder, canvas.proxy, canvas.fps), *AUDIO_ARGS,
               *CLIP_ARGS, "-r", str(canvas.fps), "-t", f"{duration:.4f}", str(tmp)]  # fmt: skip
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err
            )
            assert proc.stdin is not None
            try:
                for frame in frames:
                    proc.stdin.write(np.ascontiguousarray(frame).tobytes())
                proc.stdin.close()
            except BrokenPipeError:
                pass  # encoder died; its exit code and stderr explain why
            except BaseException:
                proc.kill()
                proc.wait()
                tmp.unlink(missing_ok=True)
                raise
            rc = proc.wait()
            if rc != 0:
                err.seek(0)
                tmp.unlink(missing_ok=True)
                msg = err.read().decode(errors="replace").strip()[-800:]
                raise RenderError(f"encoder failed for {out.name} ({rc}): {msg}")
        os.replace(tmp, out)


def _stat(path: Path | None) -> list | None:
    if path is None or not path.is_file():
        return None
    st = path.stat()
    return [str(path.resolve()), st.st_size, st.st_mtime_ns]


def _fade(t: float, duration: float, fade_in: float, fade_out: float) -> float:
    f = 1.0
    if fade_in > 0:
        f = min(f, t / fade_in)
    if fade_out > 0:
        f = min(f, (duration - t) / fade_out)
    return max(0.0, min(1.0, f))


def _silence(duration: float) -> list[str]:
    return ["-f", "lavfi", "-t", f"{duration:.4f}", "-i", "anullsrc=r=48000:cl=stereo"]


def atempo_chain(speed: float) -> str:
    """atempo accepts 0.5..100 per instance; chain instances for slower speeds."""
    parts = []
    s = speed
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    parts.append(f"atempo={s:.6g}")
    return ",".join(parts)


def concat(clips: list[Path], out: Path, tools: Tools, proxy: bool) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, dir=out.parent) as f:
        for c in clips:
            escaped = str(c.resolve()).replace("'", r"'\''")
            f.write(f"file '{escaped}'\n")
        listing = Path(f.name)
    audio = ["-c:a", "copy"] if proxy else ["-af", LOUDNORM, *AUDIO_ARGS]
    tmp = out.with_name(f".{out.stem}.tmp.mp4")
    try:
        run_ffmpeg(tools, ["-f", "concat", "-safe", "0", "-i", str(listing), "-map", "0:v",
                           "-map", "0:a", "-c:v", "copy", *audio, "-movflags", "+faststart",
                           str(tmp)])  # fmt: skip
        os.replace(tmp, out)
    finally:
        listing.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)


# --- export ----------------------------------------------------------------------------


@dataclass
class ExportResult:
    platform: str
    path: Path
    duration: float
    size_bytes: int
    clips: int
    cached: int
    seconds: float

    def to_dict(self) -> dict:
        return self.__dict__ | {"path": str(self.path)}


def prepare(edl: EDL, edl_dir: Path, tools: Tools) -> dict[str, MediaInfo]:
    """Probe every source and check the EDL against them."""
    sources = resolve_sources(edl, edl_dir)
    media: dict[str, MediaInfo] = {}
    for name, path in sources.items():
        try:
            media[name] = probe(path, tools)
        except MediaError as e:
            raise RenderError(f"source {name!r}: {e}") from e
    problems = check_against_media(edl, media)
    if problems:
        raise RenderError("; ".join(problems))
    return media


def export(
    edl: EDL,
    edl_dir: Path,
    platforms: list[Platform],
    out_dir: Path,
    renderer: Renderer,
    *,
    proxy: bool = False,
    max_duration: float | None = None,
    workers: int = 2,
    on_progress: Callable[[str], None] = lambda _: None,
) -> list[ExportResult]:
    if max_duration is not None and edl.duration > max_duration + 1e-6:
        raise RenderError(
            f"edit is {edl.duration:.1f}s but the cap is {max_duration:g}s "
            "(shorten it, raise max_duration, or pass --allow-overflow)"
        )
    tracer = renderer.tracer
    results = []
    inputs = {
        "platforms": [p.value for p in platforms],
        "proxy": proxy,
        "duration": round(edl.duration, 3),
        "encoder": renderer.encoder,
    }
    with tracer.span("phase:export", kind="phase", inputs=inputs) as phase:
        try:
            media = prepare(edl, edl_dir, renderer.tools)
        except EDLError as e:
            raise RenderError(str(e)) from e
        sources = resolve_sources(edl, edl_dir)
        for platform in platforms:
            started = time.monotonic()
            canvas = Canvas.for_platform(platform, edl, proxy)
            jobs: list[Callable[[], tuple[Path, bool]]] = []

            def hero(b):
                return (sources[b.hero.source], media[b.hero.source]) if b.hero else None

            if edl.start_banner:
                jobs.append(
                    lambda b=edl.start_banner, c=canvas: renderer.banner_clip(
                        "start", b, c, hero(b)
                    )
                )
            for seg in edl.segments:
                jobs.append(lambda s=seg, c=canvas: renderer.segment_clip(
                    s, sources[s.source], media[s.source], c))  # fmt: skip
            if edl.end_banner:
                jobs.append(
                    lambda b=edl.end_banner, c=canvas: renderer.banner_clip("end", b, c, hero(b))
                )
            on_progress(
                f"{platform.value}: rendering {len(jobs)} clips at {canvas.width}x{canvas.height}"
            )
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                # copy the context per job so spans nest under phase:export
                futures = [pool.submit(contextvars.copy_context().run, j) for j in jobs]
                clips = [f.result() for f in futures]
            suffix = ".proxy.mp4" if proxy else ".mp4"
            out = out_dir / f"{platform.value}{suffix}"
            with tracer.span("tool:concat", kind="tool", inputs={"clips": len(clips)}):
                concat([c for c, _ in clips], out, renderer.tools, proxy)
            info = probe(out, renderer.tools)
            res = ExportResult(
                platform=platform.value,
                path=out,
                duration=round(info.duration, 3),
                size_bytes=out.stat().st_size,
                clips=len(clips),
                cached=sum(1 for _, hit in clips if hit),
                seconds=round(time.monotonic() - started, 2),
            )
            on_progress(
                f"{platform.value}: {out} ({res.duration:.1f}s, {res.cached}/{res.clips} cached)"
            )
            results.append(res)
        phase.set_outputs(results=[r.to_dict() for r in results])
    return results
