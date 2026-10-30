"""Environment checks for `ave doctor`."""

from __future__ import annotations

import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Literal

from ave.clients import anthropic_credentials_source
from ave.config import ProjectConfig

CheckStatus = Literal["ok", "warn", "fail", "skip"]
MIN_FFMPEG = (6, 0)

# optional packages needed from later milestones: import name -> (purpose, milestone)
OPTIONAL_DEPS = {
    "rapidocr": ("on-screen text OCR (`uv sync --extra ocr`)", "M2"),
    "onnxruntime": ("OCR inference engine (`uv sync --extra ocr`)", "M2"),
    "playwright": ("banner rendering", "M3"),
}

CHECK_NAMES = (
    "python", "ffmpeg", "ffprobe", "nvenc", "gpu", "claude", "anthropic", "langsmith", "optional",
)  # fmt: skip


@dataclass
class Check:
    name: str
    status: CheckStatus
    detail: str
    required: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def _run(cmd: list[str], timeout: float = 15) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def parse_ffmpeg_version(text: str) -> tuple[int, int] | None:
    m = re.search(r"version\s+n?(\d+)\.(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def check_python() -> Check:
    ok = sys.version_info >= (3, 13)
    return Check("python", "ok" if ok else "fail", platform.python_version())


def check_ffmpeg(binary: str) -> Check:
    path = shutil.which(binary)
    if path is None:
        return Check(
            "ffmpeg", "fail", f"{binary!r} not found; install with `sudo apt install ffmpeg`"
        )
    proc = _run([path, "-version"])
    version = parse_ffmpeg_version(proc.stdout if proc else "")
    if version is None:
        return Check("ffmpeg", "fail", f"{path}: cannot read version")
    vstr = f"{version[0]}.{version[1]}"
    if version < MIN_FFMPEG:
        need = ".".join(map(str, MIN_FFMPEG))
        return Check("ffmpeg", "fail", f"{path} is {vstr}, need >= {need}")
    detail = f"{vstr} at {path}"
    if path.startswith("/snap/"):
        return Check("ffmpeg", "warn", f"{detail} (snap: may not read files outside $HOME)")
    return Check("ffmpeg", "ok", detail)


def check_ffprobe(binary: str) -> Check:
    path = shutil.which(binary)
    if path is None:
        return Check("ffprobe", "fail", f"{binary!r} not found")
    return Check("ffprobe", "ok", path)


def check_nvenc(binary: str, deep: bool) -> Check:
    path = shutil.which(binary)
    if path is None:
        return Check("nvenc", "skip", "ffmpeg missing", required=False)
    proc = _run([path, "-hide_banner", "-encoders"])
    if not proc or "h264_nvenc" not in proc.stdout:
        return Check("nvenc", "warn", "h264_nvenc not in this build; CPU (libx264) fallback", False)
    if not deep:
        return Check("nvenc", "ok", "h264_nvenc available (use --deep to test-encode)", False)
    probe = _run(
        [path, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         "color=c=black:s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"],
        timeout=30,
    )  # fmt: skip
    if probe and probe.returncode == 0:
        return Check("nvenc", "ok", "h264_nvenc test encode passed", False)
    err = (probe.stderr.strip().splitlines() or ["no output"])[-1] if probe else "timed out"
    return Check("nvenc", "warn", f"h264_nvenc test encode failed: {err}", False)


def check_gpu() -> Check:
    if shutil.which("nvidia-smi") is None:
        return Check("gpu", "warn", "nvidia-smi not found; GPU features off", required=False)
    proc = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
    if not proc or proc.returncode != 0 or not proc.stdout.strip():
        return Check("gpu", "warn", "nvidia-smi failed", required=False)
    return Check("gpu", "ok", proc.stdout.strip().splitlines()[0], required=False)


def _backend(cfg: ProjectConfig | None) -> str:
    return cfg.inference.backend if cfg else "claude_code"


def check_claude(cfg: ProjectConfig | None) -> Check:
    """Claude Code CLI, required when agents run as Claude Code sub-agents."""
    required = _backend(cfg) == "claude_code"
    binary = os.environ.get("AVE_CLAUDE") or (cfg.inference.claude_path if cfg else "claude")
    path = shutil.which(binary)
    if path is None:
        status = "fail" if required else "skip"
        return Check("claude", status, f"{binary!r} not found; install Claude Code", required)
    proc = _run([path, "--version"])
    m = re.search(r"(\d+\.\d+\.\d+)", proc.stdout if proc else "")
    if m is None:
        return Check(
            "claude", "fail" if required else "warn", f"{path}: cannot read version", required
        )
    # login state is not probed: that would spend subscription usage
    return Check("claude", "ok", f"Claude Code {m.group(1)} at {path}", required)


def check_anthropic(cfg: ProjectConfig | None, online: bool) -> Check:
    if _backend(cfg) != "api":
        return Check("anthropic", "skip", "inference.backend is claude_code", required=False)
    source = anthropic_credentials_source()
    if source is None:
        return Check(
            "anthropic", "fail", "no credentials: set ANTHROPIC_API_KEY or `ant auth login`"
        )
    if not online:
        return Check("anthropic", "ok", f"credentials from {source}")
    import anthropic

    model = (
        cfg.models.planner
        if cfg and cfg.models.planner.startswith("claude-")
        else "claude-opus-5-5"
    )
    try:
        anthropic.Anthropic().models.retrieve(model)
    except anthropic.APIError as e:
        return Check("anthropic", "fail", f"{source}: {model} lookup failed: {e}")
    return Check("anthropic", "ok", f"{source}: {model} reachable")


def check_langsmith(cfg: ProjectConfig | None, online: bool) -> Check:
    wanted = cfg is not None and cfg.observability.provider == "langsmith"
    has_key = bool(os.environ.get("LANGSMITH_API_KEY"))
    if not wanted:
        return Check("langsmith", "skip", "provider is not langsmith", required=False)
    if not has_key:
        return Check(
            "langsmith", "fail", "observability.provider=langsmith but LANGSMITH_API_KEY unset"
        )
    if not online:
        return Check("langsmith", "ok", f"key set, project {cfg.observability.project!r}")
    import langsmith

    try:
        next(iter(langsmith.Client().list_projects(limit=1)), None)
    except Exception as e:
        return Check("langsmith", "fail", f"API unreachable: {e}")
    return Check("langsmith", "ok", f"API reachable, project {cfg.observability.project!r}")


def check_optional() -> list[Check]:
    out = []
    for mod, (purpose, milestone) in OPTIONAL_DEPS.items():
        found = importlib.util.find_spec(mod) is not None
        detail = purpose if found else f"{purpose}: not installed (needed from {milestone})"
        out.append(Check(f"optional:{mod}", "ok" if found else "skip", detail, required=False))
    return out


def run_checks(
    cfg: ProjectConfig | None,
    *,
    online: bool = False,
    deep: bool = False,
    skip: frozenset[str] = frozenset(),
) -> list[Check]:
    ffmpeg = cfg.ffmpeg.path if cfg else "ffmpeg"
    ffprobe = cfg.ffmpeg.ffprobe if cfg else "ffprobe"
    env_ffmpeg = os.environ.get("AVE_FFMPEG")
    ffmpeg = env_ffmpeg or ffmpeg
    steps = {
        "python": lambda: [check_python()],
        "ffmpeg": lambda: [check_ffmpeg(ffmpeg)],
        "ffprobe": lambda: [check_ffprobe(os.environ.get("AVE_FFPROBE") or ffprobe)],
        "nvenc": lambda: [check_nvenc(ffmpeg, deep)],
        "gpu": lambda: [check_gpu()],
        "claude": lambda: [check_claude(cfg)],
        "anthropic": lambda: [check_anthropic(cfg, online)],
        "langsmith": lambda: [check_langsmith(cfg, online)],
        "optional": check_optional,
    }
    results: list[Check] = []
    for name, step in steps.items():
        if name in skip:
            results.append(Check(name, "skip", "skipped by --skip", required=False))
        else:
            results.extend(step())
    return results


def healthy(checks: list[Check]) -> bool:
    return not any(c.required and c.status == "fail" for c in checks)
