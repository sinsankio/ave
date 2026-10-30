"""Split Markdown docs into heading-scoped sections with line numbers and UI-cue candidates.

The doc analyst agent reads these sections (and can cite them by id); the cue candidates
(inline code, bold text, quoted labels) give OCR matching something concrete to look for.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_CUES = [
    re.compile(r"`([^`\n]{2,60})`"),  # `Export PDF`
    re.compile(r"\*\*([^*\n]{2,60})\*\*"),  # **Export PDF**
    re.compile(r"__([^_\n]{2,60})__"),
    re.compile(r"[\"“]([^\"”\n]{2,40})[\"”]"),  # "Export PDF"
]
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)")


@dataclass
class Section:
    id: str
    file: str
    heading_path: list[str]
    level: int
    line_start: int  # 1-based, inclusive
    line_end: int
    text: str
    cues: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "section"


def extract_cues(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for rx in _CUES:
        for m in rx.finditer(text):
            cue = m.group(1).strip()
            if cue and not cue.startswith(("http://", "https://")):
                seen.setdefault(cue, None)
    return list(seen)


def split_markdown(path: Path, root: Path) -> list[Section]:
    rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    start, level, in_fence = 1, 0, False
    used: dict[str, int] = {}

    def close(end: int) -> None:
        body = "\n".join(lines[start - 1 : end]).strip()
        if not body:
            return
        path_titles = [t for _, t in stack] or [Path(rel).stem]
        base = f"{slug(Path(rel).stem)}#{slug(path_titles[-1])}"
        used[base] = used.get(base, 0) + 1
        sid = base if used[base] == 1 else f"{base}-{used[base]}"
        sections.append(
            Section(
                id=sid,
                file=rel,
                heading_path=path_titles,
                level=level,
                line_start=start,
                line_end=end,
                text=body,
                cues=extract_cues(body),
                images=[m.group(2) for m in _IMAGE.finditer(body)],
            )
        )

    for i, line in enumerate(lines, 1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        m = None if in_fence else _HEADING.match(line)
        if not m:
            continue
        close(i - 1)
        lvl, title = len(m.group(1)), m.group(2).strip()
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        stack.append((lvl, title))
        start, level = i, lvl
    close(len(lines))
    return sections
