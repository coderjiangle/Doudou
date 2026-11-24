"""``doudou init`` bootstrap layout and idempotency tests (fully offline)."""

from __future__ import annotations

from pathlib import Path

from doudou.init import DEFAULT_MEMORY_FILE, init_home


def test_init_creates_default_layout(tmp_path: Path) -> None:
    result = init_home(tmp_path)

    assert result.home == tmp_path
    assert result.config_dir == tmp_path / "config"
    assert result.data_dir == tmp_path / "data"
    assert result.workspace_dir == tmp_path / "workspace"
    assert result.skills_dir == tmp_path / "skills"

    config = tmp_path / "config" / "config.yaml"
    assert config.is_file()
    text = config.read_text(encoding="utf-8")
    assert tmp_path.as_posix() in text
    assert "~/.doudou" not in text

    assert (tmp_path / "config" / "prompts" / "system.md").is_file()
    assert (tmp_path / "config" / "AGENTS.md").read_text(encoding="utf-8") == (DEFAULT_MEMORY_FILE)
    assert (tmp_path / ".env").is_file()
    assert tmp_path.as_posix() in (tmp_path / ".env").read_text(encoding="utf-8")
    assert (tmp_path / "skills" / "weather" / "SKILL.md").is_file()
    assert (tmp_path / "skills" / "browser-browsing" / "SKILL.md").is_file()


def test_init_does_not_copy_personal_memory(tmp_path: Path) -> None:
    init_home(tmp_path)

    memory = (tmp_path / "config" / "AGENTS.md").read_text(encoding="utf-8")

    assert "乐哥" not in memory


def test_init_keeps_existing_files_unless_forced(tmp_path: Path) -> None:
    init_home(tmp_path)
    config = tmp_path / "config" / "config.yaml"
    config.write_text("# custom\n", encoding="utf-8")

    second = init_home(tmp_path)

    assert config.read_text(encoding="utf-8") == "# custom\n"
    assert config in second.skipped

    init_home(tmp_path, force=True)

    assert "model:" in config.read_text(encoding="utf-8")


def test_init_force_never_overwrites_secrets_or_memory(tmp_path: Path) -> None:
    init_home(tmp_path)
    env_file = tmp_path / ".env"
    memory_file = tmp_path / "config" / "AGENTS.md"
    env_file.write_text("DOUDOU_MODEL_API_KEY=secret\n", encoding="utf-8")
    memory_file.write_text("- 用户偏好\n", encoding="utf-8")

    init_home(tmp_path, force=True)

    assert env_file.read_text(encoding="utf-8") == "DOUDOU_MODEL_API_KEY=secret\n"
    assert memory_file.read_text(encoding="utf-8") == "- 用户偏好\n"
