from pathlib import Path

import pytest
import yaml

from ave.cli import EXIT_USAGE
from ave.observability import read_trace
from tests.conftest import parse_json


def test_version(invoke):
    result = invoke("--version")
    assert result.exit_code == 0
    assert "ave 0.1.0" in result.stdout


def test_init_writes_project(project_dir: Path):
    data = yaml.safe_load((project_dir / "project.yaml").read_text())
    assert data["name"] == "Test Demo"
    assert data["inference"]["backend"] == "claude_code"
    assert data["platforms"] == ["linkedin", "reels"]
    assert data["max_duration"] == 45
    assert data["docs"] == ["docs/*.md"]
    assert data["observability"]["provider"] == "local"


def test_init_warns_about_missing_video(tmp_path: Path, invoke):
    result = invoke("-p", str(tmp_path), "init", "--yes", "--video", "nope.mp4")
    assert result.exit_code == 0
    assert "video not found yet: nope.mp4" in result.output


def test_init_refuses_overwrite(project_dir: Path, invoke):
    result = invoke("-p", str(project_dir), "init", "--yes")
    assert result.exit_code == EXIT_USAGE
    assert "exists" in result.output
    result = invoke("-p", str(project_dir), "init", "--yes", "--force", "--name", "New")
    assert result.exit_code == 0
    assert yaml.safe_load((project_dir / "project.yaml").read_text())["name"] == "New"


def test_init_interactive(tmp_path: Path, runner):
    from ave.cli import app

    answers = "My Demo\nclip.mp4\nREADME.md\nreels\n30\nnone\n"
    result = runner.invoke(app, ["-p", str(tmp_path), "init"], input=answers)
    assert result.exit_code == 0, result.output
    data = yaml.safe_load((tmp_path / "project.yaml").read_text())
    assert data["name"] == "My Demo"
    assert data["platforms"] == ["reels"]
    assert data["max_duration"] == 30
    assert data["observability"]["provider"] == "none"


def test_init_rejects_bad_platform(tmp_path: Path, invoke):
    result = invoke("-p", str(tmp_path), "init", "--yes", "--platform", "tiktok")
    assert result.exit_code == EXIT_USAGE


def test_missing_project_is_usage_error(tmp_path: Path, invoke, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke("runs")
    assert result.exit_code == EXIT_USAGE
    assert "ave init" in result.output


def test_config_get_set_show(project_dir: Path, invoke):
    p = str(project_dir)
    assert invoke("-p", p, "config", "get", "observability.provider").stdout.strip() == "local"
    result = invoke("-p", p, "config", "set", "platforms", "[facebook]")
    assert result.exit_code == 0
    assert parse_json(invoke("-p", p, "config", "get", "platforms").stdout) == ["facebook"]
    assert "Test Demo" in invoke("-p", p, "config", "show").stdout


@pytest.mark.parametrize(("key", "value"), [("nope", "1"), ("max_duration", "0")])
def test_config_set_invalid(project_dir: Path, invoke, key, value):
    before = (project_dir / "project.yaml").read_text()
    result = invoke("-p", str(project_dir), "config", "set", key, value)
    assert result.exit_code == EXIT_USAGE
    assert (project_dir / "project.yaml").read_text() == before


def test_runs_lifecycle(project_dir: Path, invoke):
    p = str(project_dir)
    assert "no runs yet" in invoke("-p", p, "runs").stdout
    assert invoke("-p", p, "status").exit_code == EXIT_USAGE

    created = parse_json(invoke("-p", p, "runs", "new", "--json").stdout)
    assert created["id"] == "r-0001" and created["status"] == "created"
    invoke("-p", p, "runs", "new")

    listed = parse_json(invoke("-p", p, "runs", "--json").stdout)
    assert [r["id"] for r in listed] == ["r-0001", "r-0002"]
    assert parse_json(invoke("-p", p, "runs", "list", "--json").stdout) == listed

    status = parse_json(invoke("-p", p, "status", "--json").stdout)
    assert status["id"] == "r-0002" and status["history"] == []
    assert parse_json(invoke("-p", p, "runs", "show", "r-0001", "--json").stdout)["id"] == "r-0001"
    assert "next: ave analyze" in invoke("-p", p, "status").stdout
    assert invoke("-p", p, "status", "r-0099").exit_code == EXIT_USAGE


def test_runs_new_writes_trace(project_dir: Path, invoke):
    invoke("-p", str(project_dir), "runs", "new")
    (rec,) = read_trace(project_dir / "runs" / "r-0001" / "trace.jsonl")
    assert rec["name"] == "run:r-0001"
    assert rec["metadata"]["ave_run_id"] == "r-0001"


def test_doctor_json(project_dir: Path, invoke, fake_bin, monkeypatch):
    monkeypatch.setenv("AVE_FFMPEG", str(fake_bin("ffmpeg", "ffmpeg version 7.0")))
    monkeypatch.setenv("AVE_FFPROBE", str(fake_bin("ffprobe", "ffprobe version 7.0")))
    monkeypatch.setenv("AVE_CLAUDE", str(fake_bin("claude", "2.1.306 (Claude Code)")))
    result = invoke("-p", str(project_dir), "doctor", "--json", "--skip", "gpu")
    assert result.exit_code == 0, result.output
    report = parse_json(result.stdout)
    assert report["healthy"] is True
    assert {c["name"] for c in report["checks"]} >= {"python", "ffmpeg", "ffprobe", "claude"}

    monkeypatch.setenv("AVE_CLAUDE", "definitely-not-claude-xyz")
    result = invoke("-p", str(project_dir), "doctor", "--json", "--skip", "gpu")
    assert result.exit_code == 1
    assert parse_json(result.stdout)["healthy"] is False


def test_init_backend_option(tmp_path: Path, invoke):
    result = invoke("-p", str(tmp_path), "init", "--yes", "--backend", "api")
    assert result.exit_code == 0
    assert "ANTHROPIC_API_KEY" in result.output
    data = yaml.safe_load((tmp_path / "project.yaml").read_text())
    assert data["inference"]["backend"] == "api"


def test_doctor_rejects_unknown_skip(invoke):
    assert invoke("doctor", "--skip", "bogus").exit_code == EXIT_USAGE


def test_doctor_table_without_project(tmp_path: Path, invoke, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke("doctor", "--skip", "anthropic")
    assert "no project" in result.stdout
