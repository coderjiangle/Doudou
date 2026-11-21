"""Default runtime layout rooted at ``~/doudou``.

The packaged/installed app keeps user state outside the current working
directory so that running ``doudou`` from any project does not scatter
``config/``, ``data/`` and ``workspace/`` around.  ``DOUDOU_HOME`` can point the
whole layout somewhere else; ``doudou init --home`` writes a custom config with
the chosen root.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Override the whole layout (process environment; also usable in tests).
HOME_ENV = "DOUDOU_HOME"
DEFAULT_HOME = "~/doudou"
#: Optional explicit env-file path; otherwise ``./.env`` wins over the home one.
ENV_FILE_ENV = "DOUDOU_ENV_FILE"


def doudou_home() -> Path:
    """Resolve the user's Doudou home directory."""
    raw = os.environ.get(HOME_ENV) or DEFAULT_HOME
    return Path(raw).expanduser().resolve()


def home_path(*parts: str) -> str:
    """Return a POSIX-style path under :func:`doudou_home` for config values."""
    return doudou_home().joinpath(*parts).as_posix()


def default_config_dir() -> Path:
    return doudou_home() / "config"


def default_data_dir() -> Path:
    return doudou_home() / "data"


def default_workspace_dir() -> Path:
    return doudou_home() / "workspace"


def default_skills_dir() -> Path:
    return doudou_home() / "skills"


def default_database_url() -> str:
    """Default SQLite URL under ``<home>/data`` (absolute, no ``~`` in URL)."""
    database = default_data_dir() / "doudou.db"
    return f"sqlite+aiosqlite:///{database.as_posix()}"


def default_env_file() -> Path:
    """Pick the env file: explicit env override > cwd ``.env`` > home ``.env``.

    The cwd-first rule keeps the repository development workflow intact, while
    an installed app can keep everything in ``~/doudou``.
    """
    explicit = os.environ.get(ENV_FILE_ENV)
    if explicit:
        return Path(explicit).expanduser().resolve()
    cwd_env = Path(".env")
    if cwd_env.is_file():
        return cwd_env
    return doudou_home() / ".env"


__all__ = [
    "DEFAULT_HOME",
    "ENV_FILE_ENV",
    "HOME_ENV",
    "default_config_dir",
    "default_data_dir",
    "default_database_url",
    "default_env_file",
    "default_skills_dir",
    "default_workspace_dir",
    "doudou_home",
    "home_path",
]
