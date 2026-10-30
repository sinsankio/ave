from pathlib import Path

import pytest

from ave.config import (
    PRESETS,
    ConfigError,
    Platform,
    ProjectConfig,
    dump_config,
    find_project_file,
    get_key,
    load_project,
    set_key,
)


def test_defaults():
    cfg = ProjectConfig(name="Demo")
    assert cfg.platforms == [Platform.LINKEDIN]
    assert cfg.max_duration == 60
    assert cfg.observability.provider == "local"
    assert cfg.observability.log_images == "thumbnails"
    assert cfg.inference.backend == "claude_code"
    assert (cfg.models.planner, cfg.models.worker) == ("opus", "sonnet")
    assert cfg.brand.linkedin_url == "https://www.linkedin.com/in/sinsankio"


def test_every_platform_has_preset():
    assert set(PRESETS) == set(Platform)
    for preset in PRESETS.values():
        assert preset.target_min < preset.target_max


@pytest.mark.parametrize(
    "data",
    [
        {"name": "  "},
        {"name": "x", "platforms": ["tiktok"]},
        {"name": "x", "max_duration": 0},
        {"name": "x", "observability": {"provider": "datadog"}},
        {"name": "x", "unknown_key": 1},
        {"name": "x", "inference": {"backend": "openai"}},
    ],
)
def test_invalid_config_rejected(data):
    with pytest.raises(ValueError):
        ProjectConfig.model_validate(data)


def test_dump_and_load_roundtrip(tmp_path: Path):
    cfg = ProjectConfig(name="Demo", videos=[Path("a.mp4")], docs=["docs/*.md"], max_duration=30)
    dump_config(cfg, tmp_path / "project.yaml")
    project = load_project(tmp_path)
    assert project.config == cfg
    assert project.root == tmp_path.resolve()
    assert project.runs_dir == tmp_path.resolve() / "runs"


def test_find_project_file_searches_parents(tmp_path: Path):
    dump_config(ProjectConfig(name="Demo"), tmp_path / "project.yaml")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_project_file(nested) == (tmp_path / "project.yaml").resolve()


def test_load_errors(tmp_path: Path):
    with pytest.raises(ConfigError, match="does not exist"):
        load_project(tmp_path)
    (tmp_path / "project.yaml").write_text("name: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_project(tmp_path)
    (tmp_path / "project.yaml").write_text("max_duration: 10\n")
    with pytest.raises(ConfigError, match="name"):
        load_project(tmp_path)


def test_doc_files_expands_globs_and_dedupes(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    for n in ("a.md", "b.md"):
        (tmp_path / "docs" / n).write_text("# x")
    (tmp_path / "README.md").write_text("# readme")
    cfg = ProjectConfig(name="Demo", docs=["docs/*.md", "docs/a.md", "README.md", "missing.md"])
    dump_config(cfg, tmp_path / "project.yaml")
    files = load_project(tmp_path).doc_files()
    assert [f.name for f in files] == ["a.md", "b.md", "README.md"]


def test_get_and_set_key():
    cfg = ProjectConfig(name="Demo")
    assert get_key(cfg, "observability.provider") == "local"
    cfg2 = set_key(cfg, "platforms", "[reels, facebook]")
    assert cfg2.platforms == [Platform.REELS, Platform.FACEBOOK]
    assert set_key(cfg, "max_duration", "30").max_duration == 30
    assert cfg.max_duration == 60  # original untouched


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("nope", "1", "unknown config key"),
        ("brand.nope", "1", "unknown config key"),
        ("name.inner", "1", "unknown config key"),
        ("max_duration", "0", "invalid value"),
        ("platforms", "[tiktok]", "invalid value"),
    ],
)
def test_set_key_errors(key, value, match):
    with pytest.raises(ConfigError, match=match):
        set_key(ProjectConfig(name="Demo"), key, value)


def test_doc_files_absolute_home_and_recursive_globs(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    (home / "repo" / "docs" / "deep").mkdir(parents=True)
    (home / "repo" / "README.md").write_text("# r")
    (home / "repo" / "docs" / "deep" / "x.md").write_text("# x")
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "proj"
    proj.mkdir()
    cfg = ProjectConfig(
        name="Demo",
        docs=[str(home / "repo" / "*.md"), "~/repo/docs/**/*.md", "/nonexistent/*.md"],
    )
    dump_config(cfg, proj / "project.yaml")
    files = load_project(proj).doc_files()
    assert [f.name for f in files] == ["README.md", "x.md"]


def test_load_env_file(tmp_path: Path, monkeypatch):
    f = tmp_path / "env"
    f.write_text(
        "# secrets\nexport AVE_TEST_A='one'\nAVE_TEST_B=two\nAVE_TEST_C=keep\nnot a pair\n"
    )
    monkeypatch.setenv("AVE_ENV_FILE", str(f))
    monkeypatch.setenv("AVE_TEST_C", "already")
    monkeypatch.delenv("AVE_TEST_A", raising=False)
    monkeypatch.delenv("AVE_TEST_B", raising=False)
    from ave.config import load_env_file

    assert load_env_file() == ["AVE_TEST_A", "AVE_TEST_B"]
    import os

    assert os.environ["AVE_TEST_A"] == "one" and os.environ["AVE_TEST_B"] == "two"
    assert os.environ["AVE_TEST_C"] == "already"  # never overrides the real environment
    for k in ("AVE_TEST_A", "AVE_TEST_B"):
        monkeypatch.delenv(k)


def test_load_env_file_missing(tmp_path: Path, monkeypatch):
    from ave.config import load_env_file

    monkeypatch.setenv("AVE_ENV_FILE", str(tmp_path / "nope"))
    assert load_env_file() == []


def test_set_key_null_survives_reload(tmp_path: Path):
    dump_config(ProjectConfig(name="Demo"), tmp_path / "project.yaml")
    project = load_project(tmp_path)
    project.config = set_key(project.config, "writing.skill", "null")
    project.save()
    assert load_project(tmp_path).config.writing.skill is None
