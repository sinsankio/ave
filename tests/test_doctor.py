import shutil

import pytest

from ave import doctor
from ave.config import Inference, Observability, ProjectConfig

API = ProjectConfig(name="x", inference=Inference(backend="api"))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ffmpeg version n4.3.1 Copyright (c) 2000-2020", (4, 3)),
        ("ffmpeg version 6.1.1-3ubuntu5 Copyright", (6, 1)),
        ("ffmpeg version 7.1 Copyright", (7, 1)),
        ("ffmpeg version N-112233-gabcdef", None),
        ("", None),
    ],
)
def test_parse_ffmpeg_version(text, expected):
    assert doctor.parse_ffmpeg_version(text) == expected


def test_ffmpeg_missing():
    c = doctor.check_ffmpeg("definitely-not-ffmpeg-xyz")
    assert c.status == "fail" and "not found" in c.detail


def test_ffmpeg_too_old(fake_bin):
    c = doctor.check_ffmpeg(str(fake_bin("ffmpeg", "ffmpeg version n4.3.1 Copyright")))
    assert c.status == "fail" and "need >= 6.0" in c.detail


def test_ffmpeg_ok(fake_bin):
    c = doctor.check_ffmpeg(str(fake_bin("ffmpeg", "ffmpeg version 6.1.1-3ubuntu5")))
    assert c.status == "ok" and c.detail.startswith("6.1")


def test_ffmpeg_unreadable_version(fake_bin):
    c = doctor.check_ffmpeg(str(fake_bin("ffmpeg", "hello")))
    assert c.status == "fail" and "cannot read version" in c.detail


def test_nvenc_absent_is_warning(fake_bin):
    c = doctor.check_nvenc(str(fake_bin("ffmpeg", " V..... libx264  H.264")), deep=False)
    assert c.status == "warn" and not c.required


def test_nvenc_listed(fake_bin):
    c = doctor.check_nvenc(str(fake_bin("ffmpeg", " V..... h264_nvenc  NVIDIA")), deep=False)
    assert c.status == "ok"


def test_anthropic_skipped_for_claude_code_backend():
    c = doctor.check_anthropic(None, online=False)
    assert c.status == "skip" and not c.required


def test_anthropic_credentials_for_api_backend(monkeypatch):
    assert doctor.check_anthropic(API, online=False).status == "fail"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    c = doctor.check_anthropic(API, online=False)
    assert c.status == "ok" and "ANTHROPIC_API_KEY" in c.detail
    assert "sk-test" not in c.detail


def test_claude_cli_found(monkeypatch, fake_bin):
    monkeypatch.setenv("AVE_CLAUDE", str(fake_bin("claude", "2.1.306 (Claude Code)")))
    c = doctor.check_claude(None)
    assert c.status == "ok" and c.required and "2.1.306" in c.detail


def test_claude_cli_missing(monkeypatch):
    monkeypatch.setenv("AVE_CLAUDE", "definitely-not-claude-xyz")
    c = doctor.check_claude(None)
    assert c.status == "fail" and c.required
    c = doctor.check_claude(API)
    assert c.status == "skip" and not c.required


def test_claude_cli_unreadable_version(monkeypatch, fake_bin):
    monkeypatch.setenv("AVE_CLAUDE", str(fake_bin("claude", "hello")))
    assert doctor.check_claude(None).status == "fail"
    assert doctor.check_claude(API).status == "warn"


def test_langsmith_check(monkeypatch):
    local = ProjectConfig(name="x")
    assert doctor.check_langsmith(local, online=False).status == "skip"
    ls = ProjectConfig(name="x", observability=Observability(provider="langsmith"))
    assert doctor.check_langsmith(ls, online=False).status == "fail"
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2-test")
    assert doctor.check_langsmith(ls, online=False).status == "ok"


def test_run_checks_skip_and_health(monkeypatch, fake_bin):
    monkeypatch.setenv("AVE_FFMPEG", str(fake_bin("ffmpeg", "ffmpeg version 7.0")))
    monkeypatch.setenv("AVE_FFPROBE", str(fake_bin("ffprobe", "ffprobe version 7.0")))
    monkeypatch.setenv("AVE_CLAUDE", str(fake_bin("claude", "2.1.306 (Claude Code)")))
    checks = doctor.run_checks(None, skip=frozenset({"gpu"}))
    by_name = {c.name: c for c in checks}
    assert by_name["claude"].status == "ok"
    assert by_name["anthropic"].status == "skip"  # not needed on the claude_code backend
    assert by_name["ffmpeg"].status == "ok"
    assert any(n.startswith("optional:") for n in by_name)
    assert doctor.healthy(checks)

    assert not doctor.healthy(doctor.run_checks(API, skip=frozenset({"gpu"})))  # no API key
    skipped = doctor.run_checks(None, skip=frozenset({"claude"}))
    assert {c.name: c.status for c in skipped}["claude"] == "skip"


@pytest.mark.ffmpeg
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_meets_minimum():
    c = doctor.check_ffmpeg("ffmpeg")
    assert c.status in ("ok", "warn"), c.detail
    assert doctor.check_ffprobe("ffprobe").status == "ok"
