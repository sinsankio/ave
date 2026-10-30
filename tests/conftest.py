from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ave.cli import app


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """No real credentials, no network tracing, wide output, and a throwaway HOME."""
    for var in list(os.environ):
        if var.startswith(("ANTHROPIC_", "LANGSMITH_", "LANGCHAIN_", "AVE_")):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("NO_COLOR", "1")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def invoke(runner: CliRunner):
    def _invoke(*args: str):
        return runner.invoke(app, list(args), catch_exceptions=False)

    return _invoke


@pytest.fixture
def project_dir(tmp_path: Path, invoke) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "features.md").write_text("# Export\nExports a PDF.\n")
    result = invoke(
        "-p", str(tmp_path), "init", "--yes", "--name", "Test Demo",
        "--video", "demo.mp4", "--docs", "docs/*.md",
        "--platform", "linkedin", "--platform", "reels", "--max-duration", "45",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    return tmp_path


@pytest.fixture
def fake_bin(tmp_path: Path):
    """Create an executable that prints fixed text, standing in for ffmpeg/ffprobe."""

    def _make(name: str, stdout: str) -> Path:
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        p = bindir / name
        p.write_text(f"#!/bin/sh\ncat <<'OUT'\n{stdout}\nOUT\n")
        p.chmod(0o755)
        return p

    return _make


def parse_json(text: str):
    return json.loads(text)
