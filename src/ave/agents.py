"""Install ave's Claude Code assets into a project: sub-agents, the /ave-edit command,
the observability hooks and a permission rule for `ave` commands.

Files are rendered from templates in ave/assets/claude. A file the user has edited since
install is left alone unless forced.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

from ave.config import Project

MARKER = "<!-- installed by ave; `ave agents install --force` overwrites local edits -->"
HOOK_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "SubagentStart",
    "SubagentStop",
    "Stop",
]
PERMISSION = "Bash(ave:*)"

Status = Literal["current", "missing", "outdated", "modified"]


def hook_command() -> str:
    return f'"{sys.executable}" -m ave hook'


def _templates() -> dict[str, str]:
    """Relative target path (under .claude/) -> template text."""
    root = resources.files("ave") / "assets" / "claude"
    out = {}
    for sub in ("agents", "commands"):
        for f in sorted((root / sub).iterdir(), key=lambda p: p.name):
            if f.name.endswith(".md"):
                out[f"{sub}/{f.name}"] = f.read_text(encoding="utf-8")
    return out


def render(template: str, project: Project) -> str:
    m = project.config.models
    skill = project.config.writing.skill
    skills_line = f"skills:\n  - {skill}\n" if skill else ""
    text = (
        template.replace("{{planner_model}}", m.planner)
        .replace("{{worker_model}}", m.worker)
        .replace("{{skills_line}}", skills_line)
    )
    return f"{text.rstrip()}\n\n{MARKER}\n"


@dataclass
class AssetStatus:
    path: Path
    status: Status


def _previous_renders(path: Path) -> bool:
    """True when the file carries our marker (ours, maybe from an older ave)."""
    return path.is_file() and MARKER in path.read_text(encoding="utf-8")


def status(project: Project) -> list[AssetStatus]:
    base = project.root / ".claude"
    out = []
    for rel, tpl in _templates().items():
        path = base / rel
        want = render(tpl, project)
        if not path.is_file():
            st: Status = "missing"
        elif path.read_text(encoding="utf-8") == want:
            st = "current"
        elif _previous_renders(path):
            st = "outdated"
        else:
            st = "modified"
        out.append(AssetStatus(path, st))
    settings = base / "settings.json"
    st = (
        "current"
        if settings.is_file() and _settings_current(_read_settings(settings))
        else "missing"
    )
    out.append(AssetStatus(settings, st))
    return out


def _read_settings(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON; fix it before installing hooks") from e
    return data if isinstance(data, dict) else {}


def _is_ours(entry: dict) -> bool:
    return any("-m ave hook" in h.get("command", "") for h in entry.get("hooks", []))


def _settings_current(data: dict) -> bool:
    hooks = data.get("hooks", {})
    cmd = hook_command()
    for ev in HOOK_EVENTS:
        ours = [e for e in hooks.get(ev, []) if _is_ours(e)]
        if len(ours) != 1 or ours[0]["hooks"][0].get("command") != cmd:
            return False
    return PERMISSION in data.get("permissions", {}).get("allow", [])


def merge_settings(data: dict) -> dict:
    """Add/refresh ave's hooks and permission rule; keep everything else as it is."""
    hooks = data.setdefault("hooks", {})
    entry = {
        "hooks": [{"type": "command", "command": hook_command(), "timeout": 10, "async": True}]
    }
    for ev in HOOK_EVENTS:
        others = [e for e in hooks.get(ev, []) if not _is_ours(e)]
        hooks[ev] = [*others, dict(entry, matcher="*") if "Tool" in ev else dict(entry)]
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    if PERMISSION not in allow:
        allow.append(PERMISSION)
    return data


def install(project: Project, force: bool = False) -> list[AssetStatus]:
    base = project.root / ".claude"
    results = []
    for rel, tpl in _templates().items():
        path = base / rel
        want = render(tpl, project)
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current == want:
            results.append(AssetStatus(path, "current"))
            continue
        if current is not None and not force and not _previous_renders(path):
            results.append(AssetStatus(path, "modified"))  # user's edits win
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(want, encoding="utf-8")
        results.append(AssetStatus(path, "current"))
    settings = base / "settings.json"
    data = merge_settings(_read_settings(settings))
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2) + "\n")
    results.append(AssetStatus(settings, "current"))
    return results
