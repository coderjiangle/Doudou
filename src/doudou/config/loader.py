"""Merge defaults, YAML file and env overrides into a validated settings object.

优先级（右 > 左）::

    代码内默认值 → config/config.yaml → config/config.local.yaml → DOUDOU_* 环境变量

环境变量名 = ``DOUDOU_`` + ``config.yaml`` 的路径（下划线连接），例如
``DOUDOU_MODEL_PRIMARY``、``DOUDOU_MODEL_MAX_TOKENS``。映射逻辑见
:mod:`doudou.config.env`；旧的无前缀厂商变量仍兼容但会打弃用日志。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, ConfigDict

from doudou.config.env import build_overrides, read_env
from doudou.config.schema import AppConfig
from doudou.config.settings import Secrets
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_CONFIG_DIR = Path("config")
DEFAULT_CONFIG_NAME = "config.yaml"
DEFAULT_LOCAL_CONFIG_NAME = "config.local.yaml"


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并两个字典。

    ``override`` 中的标量与列表直接替换，字典则继续下钻合并。
    """
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def load_yaml(path: Path) -> dict[str, Any]:
    """读取 YAML 文件，空文件视为空字典。"""
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件根节点必须是映射（mapping）：{path}")
    return cast("dict[str, Any]", data)


def resolve_path(base: Path, value: str | Path) -> Path:
    """相对路径按 ``base`` 解析，绝对路径原样返回；结果一律规范化。"""
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


class Settings(BaseModel):
    """校验后、路径已解析的完整配置。"""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    app: AppConfig
    secrets: Secrets
    config_dir: Path
    config_file: Path | None = None
    loaded_files: tuple[Path, ...] = ()
    #: HTTP API 访问令牌（来自 ``DOUDOU_API_TOKEN``），非空即开启鉴权。
    api_token: str | None = None

    # ---------------------------------------------------------------- 派生路径

    @property
    def root_dir(self) -> Path:
        """工作区根目录（相对路径按当前工作目录解析）。"""
        return resolve_path(Path.cwd(), self.app.filesystem.root_dir)

    @property
    def system_prompt_path(self) -> Path | None:
        """系统提示词文件；未配置时返回 ``None``（使用内置默认提示词）。"""
        configured = self.app.agent.system_prompt_file
        if not configured:
            return None
        return resolve_path(self.config_dir, configured)

    @property
    def subagents_path(self) -> Path | None:
        configured = self.app.agent.subagents_file
        if not configured:
            return None
        return resolve_path(self.config_dir, configured)

    @property
    def skills_dirs(self) -> tuple[Path, ...]:
        """Skill 根目录，按配置顺序解析。"""
        return tuple(resolve_path(self.config_dir, item) for item in self.app.agent.skills)

    @property
    def mcp_config_path(self) -> Path:
        return resolve_path(self.config_dir, self.app.mcp.config_file)

    @property
    def memory_paths(self) -> tuple[Path, ...]:
        return tuple(resolve_path(Path.cwd(), item) for item in self.app.agent.memory_files)

    # ---------------------------------------------------------------- 展示

    def describe(self) -> dict[str, Any]:
        """脱敏后的配置快照，用于 ``doudou config show``。"""
        from doudou.security.secrets import redact

        payload: dict[str, Any] = {
            "config_dir": str(self.config_dir),
            "config_file": str(self.config_file) if self.config_file else None,
            "loaded_files": [str(item) for item in self.loaded_files],
            "root_dir": str(self.root_dir),
            "api_token": self.api_token,
            "app": self.app.model_dump(mode="json"),
        }
        # redact 是泛型递归函数（返回类型与入参同构），这里入参是 dict，故结果为 dict。
        return cast("dict[str, Any]", redact(payload))


def load_settings(
    *,
    config_dir: Path | str | None = None,
    config_file: Path | str | None = None,
    local_config_file: Path | str | None = None,
    env_file: Path | str | None = None,
    use_env: bool = True,
) -> Settings:
    """加载并校验配置。

    Args:
        config_dir: 配置目录，默认 ``./config``，可被 ``DOUDOU_CONFIG_DIR`` 覆盖。
        config_file: 主配置文件（相对 ``config_dir`` 或绝对路径），
            默认 ``config.yaml``，可被 ``DOUDOU_CONFIG_FILE`` 覆盖。
        local_config_file: 本地覆盖文件，默认 ``config.local.yaml``。
        env_file: ``.env`` 路径；默认读取当前工作目录下的 ``.env``。
        use_env: 是否读取环境变量（测试中可关闭）。
    """
    meta, env_overrides = build_overrides(read_env(env_file=env_file, use_env=use_env))

    resolved_config_dir = resolve_path(
        Path.cwd(), config_dir or meta.get("config_dir") or DEFAULT_CONFIG_DIR
    )

    raw: dict[str, Any] = {}
    loaded: list[Path] = []

    main_name = config_file or meta.get("config_file") or DEFAULT_CONFIG_NAME
    main_path = resolve_path(resolved_config_dir, main_name)
    if main_path.is_file():
        raw = deep_merge(raw, load_yaml(main_path))
        loaded.append(main_path)
    else:
        logger.debug("config.file_missing", path=str(main_path))

    local_name = local_config_file or meta.get("local_config_file") or DEFAULT_LOCAL_CONFIG_NAME
    local_path = resolve_path(resolved_config_dir, local_name)
    if local_path.is_file():
        raw = deep_merge(raw, load_yaml(local_path))
        loaded.append(local_path)

    raw = deep_merge(raw, env_overrides)
    app = AppConfig.model_validate(raw)

    return Settings(
        app=app,
        secrets=Secrets.from_config(app),
        config_dir=resolved_config_dir,
        config_file=main_path if main_path.is_file() else None,
        loaded_files=tuple(loaded),
        api_token=meta.get("api_token"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级缓存的配置实例。

    需要重新读取时调用 :func:`reset_settings_cache`。
    """
    return load_settings()


def reset_settings_cache() -> None:
    """清空配置缓存（测试与 ``--reload`` 场景用）。"""
    get_settings.cache_clear()


def env_var(name: str) -> str | None:
    """读取原始环境变量，供少量无法走配置模型的场景使用。"""
    value = os.environ.get(name)
    return value or None


__all__ = [
    "DEFAULT_CONFIG_DIR",
    "Settings",
    "deep_merge",
    "get_settings",
    "load_settings",
    "load_yaml",
    "reset_settings_cache",
    "resolve_path",
]
