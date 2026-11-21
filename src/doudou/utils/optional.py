"""Optional dependency detection and actionable installation hints.

The project ships several optional capabilities (Feishu, local voice, browser
control).  Their third-party dependencies live in ``[project.optional-dependencies]``
so a base install stays small and works on headless/platform-limited machines.
This module centralises the "is it installed?" check and the error message so
users see ``pip install "doudou-agent[voice]"`` instead of a bare ``ModuleNotFoundError``.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence

#: Distribution name used by pip/uv.  Keep this in sync with ``[project].name``.
DISTRIBUTION_NAME = "doudou-agent"


class MissingDependencyError(RuntimeError):
    """An optional capability was enabled but its dependencies are not installed."""

    def __init__(self, *, feature: str, missing: Sequence[str], extra: str) -> None:
        self.feature = feature
        self.missing = tuple(missing)
        self.extra = extra
        super().__init__(self._message())

    def _message(self) -> str:
        return f"{self.feature}缺少可选依赖：{', '.join(self.missing)}。{install_hint(self.extra)}"


def install_hint(extra: str) -> str:
    """Return a copy-pasteable install command for one optional feature."""
    return (
        f'请安装：pip install "{DISTRIBUTION_NAME}[{extra}]"（开发环境：uv sync --extra {extra}）'
    )


def module_available(name: str) -> bool:
    """Whether a top-level module can be imported, without importing it."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        # find_spec can fail for a broken parent package; treat it as unavailable.
        return False


def missing_modules(modules: Sequence[str]) -> list[str]:
    """Return the subset of ``modules`` that is not installed."""
    return [name for name in modules if not module_available(name)]


def require_modules(*, feature: str, modules: Sequence[str], extra: str) -> None:
    """Raise :class:`MissingDependencyError` if any of ``modules`` is missing."""
    missing = missing_modules(modules)
    if missing:
        raise MissingDependencyError(feature=feature, missing=missing, extra=extra)


__all__ = [
    "DISTRIBUTION_NAME",
    "MissingDependencyError",
    "install_hint",
    "missing_modules",
    "module_available",
    "require_modules",
]
