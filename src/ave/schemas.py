"""Contracts for what agents write. `ave validate <kind> FILE` checks a file against these,
plus cross-file rules (ids that must exist in the catalog, ranges inside the video, etc.)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ave.edl import EDL, Box

FeatureId = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,47}$", description="kebab-case id")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- doc analyst ---------------------------------------------------------------------


class Feature(_Model):
    id: str = FeatureId
    name: str = Field(min_length=1, max_length=60, description="Name as the docs call it")
    summary: str = Field(max_length=300, description="What it is, in plain words")
    behavior: str = Field(max_length=400, description="What happens when it is used")
    ui_cues: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Exact on-screen strings to look for: button labels, page titles, commands",
    )
    claims: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Specific facts/numbers the docs state (verbatim or near-verbatim)",
    )
    emphasis: int = Field(ge=1, le=5, description="How much the docs push this feature")
    sources: list[str] = Field(
        min_length=1, description="docs.json section ids, or recording:<video> without docs"
    )


class Catalog(_Model):
    project_name: str = Field(max_length=80)
    overview: str = Field(max_length=600, description="What the project is, from the docs only")
    audience: str | None = Field(default=None, max_length=200)
    features: list[Feature] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def _unique(self) -> Catalog:
        ids = [f.id for f in self.features]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate feature ids: {', '.join(dupes)}")
        return self


# --- vision analysts -----------------------------------------------------------------

Flag = Literal[
    "idle",
    "loading",
    "typing",
    "repetitive",
    "navigation",
    "setup",
    "error",
    "transition",
    "result",
]


class ZoomCandidate(_Model):
    at: float = Field(ge=0, description="seconds in the source video")
    box: Box
    reason: str = Field(max_length=200)


class VisionSegment(_Model):
    start: float = Field(ge=0)
    end: float
    description: str = Field(max_length=300, description="What is visibly happening")
    feature_id: str | None = Field(default=None, description="Catalog feature shown, if any")
    importance: int = Field(ge=1, le=5)
    flags: list[Flag] = Field(default_factory=list)
    zoom_candidates: list[ZoomCandidate] = Field(default_factory=list, max_length=5)
    evidence: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="What supports this: keyframe numbers, OCR text seen",
    )

    @model_validator(mode="after")
    def _ordered(self) -> VisionSegment:
        if self.end <= self.start:
            raise ValueError(f"segment {self.start}-{self.end}: end must be after start")
        for z in self.zoom_candidates:
            if not self.start <= z.at <= self.end:
                raise ValueError(
                    f"zoom candidate at {z.at} is outside segment {self.start}-{self.end}"
                )
        return self


class VisionReport(_Model):
    chunk: str
    video: str
    segments: list[VisionSegment] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_overlap(self) -> VisionReport:
        segs = sorted(self.segments, key=lambda s: s.start)
        for a, b in zip(segs, segs[1:], strict=False):
            if b.start < a.end - 1e-3:
                raise ValueError(f"segments {a.start}-{a.end} and {b.start}-{b.end} overlap")
        return self


# --- alignment -----------------------------------------------------------------------


class TimeRange(_Model):
    video: str
    start: float = Field(ge=0)
    end: float


class FeatureCoverage(_Model):
    feature_id: str
    status: Literal["both", "docs_only", "conflict"]
    ranges: list[TimeRange] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, max_length=10)
    conflict: str | None = Field(default=None, max_length=400)

    @model_validator(mode="after")
    def _consistent(self) -> FeatureCoverage:
        if self.status == "docs_only" and self.ranges:
            raise ValueError(f"{self.feature_id}: docs_only must not list video ranges")
        if self.status in ("both", "conflict") and not self.ranges:
            raise ValueError(f"{self.feature_id}: status {self.status} needs at least one range")
        if self.status == "conflict" and not self.conflict:
            raise ValueError(f"{self.feature_id}: describe the conflict")
        return self


class VideoOnly(_Model):
    video: str
    start: float = Field(ge=0)
    end: float
    description: str = Field(max_length=300)
    suggested_name: str | None = Field(default=None, max_length=60)
    confidence: float = Field(ge=0, le=1)


class Alignment(_Model):
    features: list[FeatureCoverage]
    video_only: list[VideoOnly] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list, max_length=10)


# --- plan ----------------------------------------------------------------------------


class Cut(_Model):
    video: str
    start: float = Field(ge=0)
    end: float
    reason: str = Field(max_length=200)


# --- copywriter ----------------------------------------------------------------------

TITLE_MAX_CHARS = 40
CAPTION_MAX_CHARS = 60
CAPTION_MAX_WORDS = 9


class CopyText(_Model):
    segment: str = Field(description="plan segment id")
    style: Literal["title", "caption"]
    text: str = Field(min_length=1, max_length=CAPTION_MAX_CHARS)
    sources: list[str] = Field(
        min_length=1,
        description="Fact ledger: catalog feature ids, 'ocr:<text seen>', 'vision:<chunk>@<sec>'",
    )


class BannerCopy(_Model):
    title: str = Field(min_length=1, max_length=50)
    subtitle: str | None = Field(default=None, max_length=110)
    lines: list[str] = Field(default_factory=list, max_length=3)
    cta: str | None = Field(default=None, max_length=40)
    hero_at: float | None = Field(
        default=None, ge=0, description="source seconds of the frame behind this banner"
    )
    sources: list[str] = Field(default_factory=list)


class Copy(_Model):
    start_banner: BannerCopy
    end_banner: BannerCopy
    texts: list[CopyText] = Field(description="Every on-screen title/caption, per segment")
    post: str | None = Field(default=None, max_length=3000, description="LinkedIn post text")
    notes: list[str] = Field(
        default_factory=list,
        max_length=12,
        description="Fidelity notes: interpretations to confirm, derived numbers, gaps",
    )

    @model_validator(mode="after")
    def _one_per_style(self) -> Copy:
        seen = [(t.segment, t.style) for t in self.texts]
        dupes = sorted({f"{a}/{b}" for a, b in seen if seen.count((a, b)) > 1})
        if dupes:
            raise ValueError(f"more than one text of the same style in: {', '.join(dupes)}")
        return self


class Plan(_Model):
    summary: str = Field(max_length=800, description="The story of the edit in a few sentences")
    target_duration: float = Field(gt=0)
    edl: EDL
    cuts: list[Cut] = Field(default_factory=list, description="Source ranges left out, and why")
    questions: list[str] = Field(
        default_factory=list, max_length=10, description="Decisions the user should make at Gate 1"
    )
    post: str | None = Field(
        default=None, max_length=3000, description="LinkedIn post (copywriter)"
    )
    copy_notes: list[str] = Field(default_factory=list, description="Copywriter fidelity notes")


# --- editing: QA critic and change requests ---------------------------------------


class CriticIssue(_Model):
    severity: Literal["info", "warn", "error"]
    kind: str = Field(max_length=40, description="e.g. covers-text, zoom-small, awkward-cut")
    message: str = Field(max_length=300)
    platform: str | None = None
    segment: str | None = None
    at: float | None = Field(default=None, ge=0, description="output seconds")
    suggestion: str | None = Field(default=None, max_length=300)
    evidence: list[str] = Field(default_factory=list, max_length=6)


class CriticReport(_Model):
    issues: list[CriticIssue]
    verdict: Literal["pass", "changes_needed"]
    summary: str = Field(max_length=600)


class ChangeOption(_Model):
    id: str = Field(pattern=r"^[A-E]$")
    label: str = Field(max_length=60)
    description: str = Field(max_length=400)
    tradeoffs: str = Field(max_length=300)
    edl: str | None = Field(
        default=None,
        description="Path (relative to the run dir) of the full EDL for this option;"
        " null means keep the current edit",
    )


class ChangeRequest(_Model):
    title: str = Field(max_length=100)
    problem: str = Field(max_length=600, description="What changed or went wrong, and why")
    cause: Literal["qa", "critic", "render", "plan", "user"]
    issues: list[str] = Field(default_factory=list, description="QA / critic issue ids")
    options: list[ChangeOption] = Field(min_length=2, max_length=4)
    recommended: str

    @model_validator(mode="after")
    def _options(self) -> ChangeRequest:
        ids = [o.id for o in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError("option ids must be unique")
        if self.recommended not in ids:
            raise ValueError(f"recommended {self.recommended!r} is not one of {ids}")
        return self


KINDS: dict[str, type[BaseModel]] = {
    "catalog": Catalog,
    "vision": VisionReport,
    "alignment": Alignment,
    "plan": Plan,
    "copy": Copy,
    "critic": CriticReport,
    "change": ChangeRequest,
    "edl": EDL,
}
