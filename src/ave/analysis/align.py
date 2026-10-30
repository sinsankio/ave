"""Deterministic evidence for alignment: where each feature's UI cues appear in OCR text.

The alignment agent makes the final call; this gives it timestamps and boxes to start from.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

from ave.schemas import Catalog

MATCH_THRESHOLD = 0.82
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def norm(s: str) -> str:
    return _NON_ALNUM.sub(" ", s.lower()).strip()


def match_score(cue: str, text: str) -> float:
    """1.0 when the cue appears whole-word inside the OCR line; else fuzzy similarity."""
    c, t = norm(cue), norm(text)
    if not c or not t:
        return 0.0
    if f" {c} " in f" {t} ":
        return 1.0
    if len(c) < 4:  # short cues only count as exact matches
        return 0.0
    best = SequenceMatcher(None, c, t).ratio()
    words = t.split()
    n = len(c.split())
    for i in range(max(1, len(words) - n + 1)):  # compare against same-length word windows
        best = max(best, SequenceMatcher(None, c, " ".join(words[i : i + n])).ratio())
    return round(best, 3)


@dataclass
class CueHit:
    video: str
    keyframe: int
    t: float
    cue: str
    text: str
    score: float
    box: dict


@dataclass
class FeatureEvidence:
    feature_id: str
    hits: list[CueHit] = field(default_factory=list)
    ranges: list[dict] = field(default_factory=list)  # {video, start, end, keyframes}


def merge_ranges(
    hits: list[CueHit], keyframe_times: dict[str, list[float]], pad: float = 1.0
) -> list[dict]:
    """Group hits on consecutive keyframes into time ranges."""
    out: list[dict] = []
    for video in sorted({h.video for h in hits}):
        times = keyframe_times[video]
        idx = sorted({h.keyframe for h in hits if h.video == video})
        group: list[int] = []
        for k in [*idx, None]:
            if k is not None and (not group or k == group[-1] + 1):
                group.append(k)
                continue
            if group:
                start = times[group[0] - 1]
                end = times[group[-1]] if group[-1] < len(times) else times[group[-1] - 1] + pad
                out.append(
                    {
                        "video": video,
                        "start": round(max(0.0, start - pad / 2), 3),
                        "end": round(end, 3),
                        "keyframes": group,
                    }
                )
            group = [k] if k is not None else []
    return out


def cue_evidence(catalog: Catalog, ocr: dict[str, list[dict]]) -> list[FeatureEvidence]:
    """ocr: video name -> list of FrameText dicts (index, t, items[{text, conf, box}])."""
    keyframe_times = {v: [f["t"] for f in frames] for v, frames in ocr.items()}
    out = []
    for feat in catalog.features:
        cues = list(dict.fromkeys([*feat.ui_cues, feat.name]))
        ev = FeatureEvidence(feat.id)
        for video, frames in ocr.items():
            for fr in frames:
                best: CueHit | None = None
                for item in fr["items"]:
                    for cue in cues:
                        s = match_score(cue, item["text"])
                        if s >= MATCH_THRESHOLD and (best is None or s > best.score):
                            best = CueHit(
                                video, fr["index"], fr["t"], cue, item["text"], s, item["box"]
                            )
                if best:
                    ev.hits.append(best)
        ev.ranges = merge_ranges(ev.hits, keyframe_times)
        out.append(ev)
    return out


def to_json(evidence: list[FeatureEvidence]) -> list[dict]:
    return [asdict(e) for e in evidence]
