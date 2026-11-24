"""Bootstrap the default ``~/.doudou`` layout (config, data, workspace, skills)."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from doudou.config.paths import doudou_home

#: Written only when the user has no memory file yet; never contains personal notes.
DEFAULT_MEMORY_FILE = """# 助手记忆

<!-- 这个文件会每轮注入系统提示词；remember 工具只写这里。 -->
"""


class InitError(RuntimeError):
    """Initialization failed because a path is invalid or a template is missing."""


@dataclass(frozen=True, slots=True)
class InitResult:
    """Paths and file operations produced by :func:`init_home`."""

    home: Path
    config_dir: Path
    data_dir: Path
    workspace_dir: Path
    skills_dir: Path
    created: tuple[Path, ...]
    skipped: tuple[Path, ...]


def init_home(home: Path | str | None = None, *, force: bool = False) -> InitResult:
    """Create the runtime layout and copy default config/skills into it.

    Existing files are kept unless ``force`` is true; this makes repeated
    ``doudou init`` calls safe. Skills are copied file-by-file and never delete
    user-added skills.
    """
    target = Path(home).expanduser().resolve() if home is not None else doudou_home()
    if target.exists() and not target.is_dir():
        raise InitError(f"初始化目录已存在但不是目录：{target}")

    template_root = _template_root()
    config_dir = target / "config"
    data_dir = target / "data"
    workspace_dir = target / "workspace"
    skills_dir = target / "skills"
    for directory in (
        target,
        config_dir,
        config_dir / "prompts",
        data_dir,
        workspace_dir,
        skills_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    created: list[Path] = []
    skipped: list[Path] = []
    replacements = {"~/.doudou": target.as_posix()}

    _copy_text(
        template_root.joinpath("config", "config.example.yaml"),
        config_dir / "config.yaml",
        replacements=replacements,
        force=force,
        created=created,
        skipped=skipped,
    )
    _copy_text(
        template_root.joinpath("config", "prompts", "system.md"),
        config_dir / "prompts" / "system.md",
        replacements={},
        force=force,
        created=created,
        skipped=skipped,
    )
    _copy_text(
        template_root.joinpath(".env.example"),
        target / ".env.example",
        replacements=replacements,
        force=force,
        created=created,
        skipped=skipped,
    )
    # Secrets are user data: only seed ``.env`` when it does not exist, even with --force.
    _copy_text(
        template_root.joinpath(".env.example"),
        target / ".env",
        replacements=replacements,
        force=False,
        created=created,
        skipped=skipped,
    )
    _copy_tree(
        template_root.joinpath("skills"),
        skills_dir,
        force=force,
        created=created,
        skipped=skipped,
    )

    # Memory is user data too; never overwrite it, even with --force.
    memory_file = config_dir / "AGENTS.md"
    if memory_file.exists():
        skipped.append(memory_file)
    else:
        memory_file.write_text(DEFAULT_MEMORY_FILE, encoding="utf-8", newline="\n")
        created.append(memory_file)

    return InitResult(
        home=target,
        config_dir=config_dir,
        data_dir=data_dir,
        workspace_dir=workspace_dir,
        skills_dir=skills_dir,
        created=tuple(created),
        skipped=tuple(skipped),
    )


def _template_root() -> Traversable:
    """Packaged templates first; fall back to the repository checkout for dev runs."""
    packaged = files("doudou").joinpath("templates")
    if packaged.joinpath("config", "config.example.yaml").is_file():
        return packaged

    repo = Path(__file__).resolve().parents[2]
    if (repo / "config" / "config.example.yaml").is_file():
        return repo
    raise InitError("找不到初始化模板（doudou/templates 或仓库 config/config.example.yaml）。")


def _copy_text(
    source: Traversable,
    target: Path,
    *,
    replacements: dict[str, str],
    force: bool,
    created: list[Path],
    skipped: list[Path],
) -> None:
    if target.exists() and not force:
        skipped.append(target)
        return
    text = source.read_text(encoding="utf-8")
    for old, new in replacements.items():
        text = text.replace(old, new)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    created.append(target)


def _copy_tree(
    source: Traversable,
    target: Path,
    *,
    force: bool,
    created: list[Path],
    skipped: list[Path],
) -> None:
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            _copy_tree(
                child,
                target / child.name,
                force=force,
                created=created,
                skipped=skipped,
            )
        return
    if not source.is_file():
        return
    if target.exists() and not force:
        skipped.append(target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    created.append(target)


__all__ = ["DEFAULT_MEMORY_FILE", "InitError", "InitResult", "init_home"]
