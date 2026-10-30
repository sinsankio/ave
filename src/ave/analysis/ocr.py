"""On-screen text from keyframes (RapidOCR, optional extra `ocr`).

Screen recordings repeat the same screen for many keyframes, so a frame that barely differs
from the last one actually read reuses its result instead of running OCR again.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from ave.analysis.keyframes import Keyframe

MIN_CONFIDENCE = 0.6
REUSE_BELOW = 0.01  # share of changed pixels under which the previous OCR result is reused


class OCRUnavailable(Exception):
    """rapidocr/onnxruntime are not installed (`uv sync --extra ocr`)."""


@dataclass
class TextHit:
    text: str
    conf: float
    box: dict  # normalized {x, y, w, h}


@dataclass
class FrameText:
    index: int
    t: float
    items: list[TextHit] = field(default_factory=list)
    reused_from: int | None = None


def available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        import rapidocr  # noqa: F401
    except ImportError:
        return False
    return True


class OCR:
    def __init__(self) -> None:
        try:
            from rapidocr import RapidOCR
        except ImportError as e:
            raise OCRUnavailable("OCR needs the `ocr` extra: uv sync --extra ocr") from e
        # rapidocr logs every model load at INFO; keep the CLI output clean
        logging.disable(logging.INFO)
        try:
            self._engine = RapidOCR()
        finally:
            logging.disable(logging.NOTSET)

    def read(self, image: np.ndarray) -> list[TextHit]:
        """image: RGB uint8."""
        h, w = image.shape[:2]
        logging.disable(logging.INFO)
        try:
            res = self._engine(image[..., ::-1])  # engine expects BGR
        finally:
            logging.disable(logging.NOTSET)
        if res is None or res.txts is None:
            return []
        hits = []
        for text, conf, box in zip(res.txts, res.scores, res.boxes, strict=False):
            if conf < MIN_CONFIDENCE or not text.strip():
                continue
            pts = np.asarray(box, dtype=np.float32)
            x0, y0 = pts.min(axis=0)
            x1, y1 = pts.max(axis=0)
            hits.append(
                TextHit(
                    text.strip(),
                    round(float(conf), 4),
                    {
                        "x": round(float(x0) / w, 4),
                        "y": round(float(y0) / h, 4),
                        "w": round(float(x1 - x0) / w, 4),
                        "h": round(float(y1 - y0) / h, 4),
                    },
                )
            )
        return hits


def _small_gray(path: Path) -> np.ndarray:
    with Image.open(path) as im:
        return np.asarray(im.convert("L").resize((160, 90)), dtype=np.int16)


def read_keyframes(
    frames: list[Keyframe], analysis_dir: Path, engine: OCR | None = None
) -> list[FrameText]:
    engine = engine or OCR()
    out: list[FrameText] = []
    last_small = None
    last: FrameText | None = None
    for kf in frames:
        path = analysis_dir / kf.file
        small = _small_gray(path)
        if last is not None and last_small is not None:
            changed = float((np.abs(small - last_small) > 12).mean())
            if changed < REUSE_BELOW:
                src = last.reused_from or last.index
                out.append(FrameText(kf.index, kf.t, last.items, reused_from=src))
                continue
        with Image.open(path) as im:
            rgb = np.asarray(im.convert("RGB"))
        ft = FrameText(kf.index, kf.t, engine.read(rgb))
        out.append(ft)
        last, last_small = ft, small
    return out


def to_json(results: list[FrameText]) -> list[dict]:
    return [asdict(r) for r in results]
