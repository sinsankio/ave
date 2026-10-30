"""Edit Decision List: the single description of an edit that the renderer executes.

Times on segments, zooms and texts are SOURCE seconds (what you see in the recording).
The renderer maps them to output time: out = (src - segment.start) / segment.speed.
Boxes are normalized to the source frame (0..1).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ave.media import MediaInfo

MAX_ZOOM = 3.0
FAST_FORWARD_BADGE_MIN_SPEED = 1.5
SOURCE_TOLERANCE = 0.05  # seconds a segment may overrun the probed source duration


class EDLError(Exception):
    """EDL file missing, unparsable or inconsistent."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Box(_Model):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _inside_frame(self) -> Box:
        if self.x + self.w > 1 + 1e-6 or self.y + self.h > 1 + 1e-6:
            raise ValueError("box must lie inside the frame (x+w <= 1, y+h <= 1)")
        return self


class Zoom(_Model):
    start: float = Field(ge=0, description="source seconds")
    end: float = Field(description="source seconds")
    box: Box = Field(description="region to zoom into, normalized to the source frame")
    ease: float = Field(default=0.6, ge=0, le=3, description="output seconds to ease in/out")

    @model_validator(mode="after")
    def _ordered(self) -> Zoom:
        if self.end <= self.start:
            raise ValueError("zoom end must be after start")
        return self


TextStyle = Literal["title", "caption"]


class Text(_Model):
    text: str = Field(min_length=1, max_length=160)
    start: float = Field(ge=0, description="source seconds")
    end: float = Field(description="source seconds")
    style: TextStyle = Field(
        default="caption", description="title: feature name card; caption: behavior line"
    )
    position: Literal["top", "bottom"] | None = Field(
        default=None, description="default: title top-left, caption bottom-center"
    )

    @model_validator(mode="after")
    def _ordered(self) -> Text:
        if self.end <= self.start:
            raise ValueError("text end must be after start")
        return self


class Segment(_Model):
    id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")
    source: str = "main"
    start: float = Field(ge=0)
    end: float
    speed: float = Field(default=1.0, ge=0.25, le=16)
    audio: Literal["auto", "keep", "mute"] = Field(
        default="auto", description="auto keeps audio unless the segment is fast-forwarded"
    )
    focus_x: float = Field(
        default=0.5, ge=0, le=1, description="horizontal center of the crop when reframe=fill"
    )
    badge: bool | None = Field(default=None, description="show speed badge; default when sped up")
    fade_in: float = Field(default=0, ge=0, le=2)
    fade_out: float = Field(default=0, ge=0, le=2)
    zooms: list[Zoom] = Field(default_factory=list)
    texts: list[Text] = Field(default_factory=list)
    feature: str | None = Field(default=None, description="catalog feature id shown (planning)")
    note: str | None = Field(default=None, max_length=300, description="why this segment is here")

    @property
    def source_duration(self) -> float:
        return self.end - self.start

    @property
    def duration(self) -> float:
        """Output seconds."""
        return self.source_duration / self.speed

    def to_output(self, src_t: float) -> float:
        return (src_t - self.start) / self.speed

    @property
    def keeps_audio(self) -> bool:
        if self.audio == "auto":
            return self.speed <= FAST_FORWARD_BADGE_MIN_SPEED
        return self.audio == "keep"

    @property
    def shows_badge(self) -> bool:
        return self.badge if self.badge is not None else self.speed >= FAST_FORWARD_BADGE_MIN_SPEED

    @model_validator(mode="after")
    def _consistent(self) -> Segment:
        if self.end <= self.start:
            raise ValueError(f"segment {self.id}: end must be after start")
        for kind, items in (("zoom", self.zooms), ("text", self.texts)):
            for it in items:
                if it.start < self.start - 1e-6 or it.end > self.end + 1e-6:
                    raise ValueError(
                        f"segment {self.id}: {kind} {it.start}-{it.end} is outside "
                        f"the segment {self.start}-{self.end}"
                    )
        zooms = sorted(self.zooms, key=lambda z: z.start)
        for a, b in zip(zooms, zooms[1:], strict=False):
            if b.start < a.end:
                raise ValueError(
                    f"segment {self.id}: zooms {a.start}-{a.end} and {b.start} overlap"
                )
        if self.fade_in + self.fade_out > self.duration:
            raise ValueError(f"segment {self.id}: fades are longer than the segment")
        return self


class Hero(_Model):
    source: str = Field(description="EDL source name")
    at: float = Field(ge=0, description="source seconds of the frame shown behind the banner")


class Banner(_Model):
    title: str = Field(min_length=1, max_length=80)
    subtitle: str | None = Field(default=None, max_length=160)
    lines: list[str] = Field(default_factory=list, max_length=4)
    cta: str | None = Field(default=None, max_length=60, description="call-to-action pill")
    hero: Hero | None = Field(default=None, description="blurred recording frame as background")
    duration: float = Field(default=3.0, ge=1, le=8)


class EDL(_Model):
    version: Literal[1] = 1
    sources: dict[str, Path] = Field(
        min_length=1, description="name -> video path (relative to EDL)"
    )
    fps: int = Field(default=30, ge=12, le=60)
    reframe: Literal["fit", "fill"] = Field(
        default="fit",
        description="fit: whole frame on a background when aspects differ; fill: crop to canvas",
    )
    background: Literal["blur", "color"] = "blur"
    start_banner: Banner | None = None
    end_banner: Banner | None = None
    segments: list[Segment] = Field(min_length=1)

    @model_validator(mode="after")
    def _refs(self) -> EDL:
        ids = [s.id for s in self.segments]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate segment ids: {', '.join(dupes)}")
        for s in self.segments:
            if s.source not in self.sources:
                raise ValueError(f"segment {s.id}: unknown source {s.source!r}")
        for name, b in (("start_banner", self.start_banner), ("end_banner", self.end_banner)):
            if b and b.hero and b.hero.source not in self.sources:
                raise ValueError(f"{name}: unknown hero source {b.hero.source!r}")
        return self

    @property
    def duration(self) -> float:
        banners = sum(b.duration for b in (self.start_banner, self.end_banner) if b)
        return banners + sum(s.duration for s in self.segments)


def load_edl(path: Path) -> EDL:
    if not path.is_file():
        raise EDLError(f"no such EDL file: {path}")
    try:
        text = path.read_text()
        data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        raise EDLError(f"{path}: cannot parse: {e}") from e
    try:
        return EDL.model_validate(data or {})
    except ValidationError as e:
        raise EDLError(f"{path}: {e}") from e


def resolve_sources(edl: EDL, base: Path) -> dict[str, Path]:
    return {k: (v if v.is_absolute() else base / v).expanduser() for k, v in edl.sources.items()}


def check_against_media(edl: EDL, media: dict[str, MediaInfo]) -> list[str]:
    """Problems that need the probed sources: segments past the end of the recording."""
    problems = []
    for name, b in (("start_banner", edl.start_banner), ("end_banner", edl.end_banner)):
        if b and b.hero and b.hero.at >= media[b.hero.source].duration:
            problems.append(f"{name}: hero frame at {b.hero.at:.2f}s is past the end of the video")
    for s in edl.segments:
        info = media[s.source]
        if s.end > info.duration + SOURCE_TOLERANCE:
            problems.append(
                f"segment {s.id}: ends at {s.end:.2f}s but {s.source} is {info.duration:.2f}s long"
            )
    return problems


EXAMPLE = """\
# ave EDL (edit decision list). Times are source seconds; boxes are 0..1 of the frame.
version: 1
sources:
  main: demo.mp4
fps: 30
reframe: fit          # fit | fill
background: blur      # blur | color (brand primary)
start_banner:
  title: Smart Invoice
  subtitle: Turns a photo of a receipt into a ready-to-send invoice
  hero: {source: main, at: 38.0}   # blurred frame behind the title (optional)
  duration: 3
segments:
  - id: login
    start: 2.0
    end: 14.0
    speed: 4          # fast-forward: audio muted, speed badge shown
  - id: export
    start: 31.5
    end: 42.0
    texts:
      - {text: Export to PDF, start: 31.5, end: 42.0, style: title}
      - {text: "One click, the invoice is ready to send", start: 33.0, end: 40.0}
    zooms:
      - start: 34.0
        end: 39.0
        box: {x: 0.55, y: 0.10, w: 0.40, h: 0.30}
end_banner:
  title: Built by Sineth Sankalpa
  lines: [linkedin.com/in/sinsankio]
  cta: Follow for more builds
  duration: 3.5
"""
