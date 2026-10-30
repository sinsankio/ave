"""Project configuration (project.yaml) schema, loading and dotted-key editing."""

from __future__ import annotations

import glob
import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PROJECT_FILE = "project.yaml"


ENV_FILE = Path("~/.config/ave/env")


def load_env_file(path: Path | None = None) -> list[str]:
    """Load KEY=VALUE lines (e.g. LANGSMITH_API_KEY) into os.environ without overriding.

    Keeps secrets out of project files and shell profiles; `ave` commands and the Claude Code
    hooks both call this, so a running session picks up a key added later.
    Path: $AVE_ENV_FILE or ~/.config/ave/env. Returns the names that were set.
    """
    file = Path(os.environ.get("AVE_ENV_FILE") or path or ENV_FILE).expanduser()
    if not file.is_file():
        return []
    loaded = []
    for line in file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


class ConfigError(Exception):
    """Raised when project.yaml is missing, unreadable or invalid."""


class Platform(StrEnum):
    LINKEDIN = "linkedin"
    REELS = "reels"
    FACEBOOK = "facebook"


class PlatformPreset(BaseModel):
    model_config = ConfigDict(frozen=True)

    aspect: str
    width: int
    height: int
    target_min: float
    target_max: float
    # fractions of the frame kept clear of platform UI (captions, buttons, profile row)
    safe_top: float = 0.06
    safe_bottom: float = 0.08
    safe_side: float = 0.05


PRESETS: dict[Platform, PlatformPreset] = {
    Platform.LINKEDIN: PlatformPreset(
        aspect="16:9", width=1920, height=1080, target_min=45, target_max=60
    ),
    Platform.REELS: PlatformPreset(
        aspect="9:16",
        width=1080,
        height=1920,
        target_min=30,
        target_max=45,
        safe_top=0.12,
        safe_bottom=0.22,
        safe_side=0.06,
    ),
    Platform.FACEBOOK: PlatformPreset(
        aspect="4:5", width=1080, height=1350, target_min=30, target_max=60
    ),
}


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Brand(_Section):
    author_name: str = "Sineth Sankalpa"
    linkedin_url: str = "https://www.linkedin.com/in/sinsankio"
    primary_color: str = "#0A66C2"
    accent_color: str = "#F5F5F5"
    logo: Path | None = None
    photo: Path | None = Field(default=None, description="Square-ish photo for the end card")
    font: Path | None = Field(default=None, description="Bold .ttf/.otf for titles and banners")
    font_regular: Path | None = Field(default=None, description=".ttf/.otf for captions")

    @field_validator("primary_color", "accent_color")
    @classmethod
    def _hex_color(cls, v: str) -> str:
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", v):
            raise ValueError("color must look like #RRGGBB")
        return v.upper()


class Observability(_Section):
    provider: Literal["langsmith", "local", "none"] = "local"
    project: str = "ai-video-editor"
    log_images: Literal["full", "thumbnails", "none"] = "thumbnails"


class Writing(_Section):
    """On-screen copy and the LinkedIn post (copywriter agent)."""

    skill: str | None = Field(
        default="sineth-writing-style", description="Claude Code skill the copywriter loads"
    )
    linkedin_post: bool = True


class Inference(_Section):
    """Where agent reasoning runs.

    claude_code: agents are Claude Code sub-agents (.claude/agents/) driven by /ave-edit,
    using the user's own Claude subscription. ave only does deterministic work.
    api: agents call the Anthropic API from Python (needs ANTHROPIC_API_KEY).
    """

    backend: Literal["claude_code", "api"] = "claude_code"
    claude_path: str = "claude"


class Models(_Section):
    """Models for the agents. On the claude_code backend use Claude Code aliases
    (opus, sonnet, haiku, inherit) or full model ids; on api, full model ids."""

    planner: str = "opus"  # pacing editor, alignment: judgment-heavy
    worker: str = "sonnet"  # doc and vision analysts: many reads


class FFmpeg(_Section):
    path: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    encoder: Literal["auto", "nvenc", "cpu"] = "auto"


class ProjectConfig(_Section):
    name: str
    videos: list[Path] = Field(default_factory=list)
    docs: list[str] = Field(default_factory=list, description="Paths or globs of .md docs")
    platforms: list[Platform] = Field(default_factory=lambda: [Platform.LINKEDIN])
    max_duration: float = Field(default=60.0, gt=0, le=600)
    brand: Brand = Field(default_factory=Brand)
    observability: Observability = Field(default_factory=Observability)
    inference: Inference = Field(default_factory=Inference)
    writing: Writing = Field(default_factory=Writing)
    models: Models = Field(default_factory=Models)
    ffmpeg: FFmpeg = Field(default_factory=FFmpeg)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return v.strip()


class Project:
    """A loaded project: config plus the directory it lives in."""

    def __init__(self, root: Path, config: ProjectConfig):
        self.root = root
        self.config = config

    @property
    def file(self) -> Path:
        return self.root / PROJECT_FILE

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def state_dir(self) -> Path:
        return self.root / ".ave"

    def resolve(self, p: Path | str) -> Path:
        p = Path(p).expanduser()
        return p if p.is_absolute() else self.root / p

    def doc_files(self) -> list[Path]:
        """Expand docs entries (paths or globs; relative, absolute or ~) into existing files.

        `**` matches any depth. Results are de-duplicated and keep entry order.
        """
        seen: dict[Path, None] = {}
        for entry in self.config.docs:
            if _is_glob(entry):
                pattern = str(self.resolve(entry))  # absolute, with ~ expanded
                matches = [Path(m) for m in sorted(glob.glob(pattern, recursive=True))]
            else:
                matches = [self.resolve(entry)]
            for m in matches:
                if m.is_file():
                    seen.setdefault(m.resolve(), None)
        return list(seen)

    def save(self) -> None:
        dump_config(self.config, self.file)


def _is_glob(s: str) -> bool:
    return any(c in s for c in "*?[")


def find_project_file(start: Path | None = None) -> Path | None:
    """Look for project.yaml in start and its parents."""
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        candidate = d / PROJECT_FILE
        if candidate.is_file():
            return candidate
    return None


def load_project(path: Path | None = None) -> Project:
    """Load project.yaml from an explicit file/dir, or search upward from cwd."""
    if path is not None:
        file = path / PROJECT_FILE if path.is_dir() else path
    else:
        file = find_project_file()
        if file is None:
            raise ConfigError(f"no {PROJECT_FILE} found here or in parent dirs; run `ave init`")
    if not file.is_file():
        raise ConfigError(f"{file} does not exist; run `ave init`")
    try:
        data = yaml.safe_load(file.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{file}: invalid YAML: {e}") from e
    try:
        config = ProjectConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"{file}: {e}") from e
    return Project(file.parent.resolve(), config)


def dump_config(config: ProjectConfig, file: Path) -> None:
    # keep explicit nulls: dropping them would bring back non-null defaults on reload
    data = config.model_dump(mode="json")
    file.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def get_key(config: ProjectConfig, key: str) -> Any:
    node: Any = config.model_dump(mode="json")
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"unknown config key: {key}")
        node = node[part]
    return node


def set_key(config: ProjectConfig, key: str, raw_value: str) -> ProjectConfig:
    """Return a new validated config with `key` set. Value is parsed as YAML (so lists work)."""
    data = config.model_dump(mode="json")
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            raise ConfigError(f"unknown config key: {key}")
        node = node[part]
    if parts[-1] not in node:
        raise ConfigError(f"unknown config key: {key}")
    try:
        node[parts[-1]] = yaml.safe_load(raw_value)
    except yaml.YAMLError as e:
        raise ConfigError(f"cannot parse value {raw_value!r}: {e}") from e
    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"invalid value for {key}: {e}") from e
