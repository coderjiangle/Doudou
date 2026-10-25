"""Environment variables → config paths.

命名规则只有一条：``DOUDOU_`` + ``config.yaml`` 的路径，层级用下划线连接::

    model.primary             → DOUDOU_MODEL_PRIMARY
    model.max_tokens          → DOUDOU_MODEL_MAX_TOKENS
    tools.web_search.provider → DOUDOU_TOOLS_WEB_SEARCH_PROVIDER
    model.api_key             → DOUDOU_MODEL_API_KEY

字段名本身也带下划线（``max_tokens``、``base_url``），所以**不能**直接让
pydantic-settings 用 ``_`` 当嵌套分隔符（它会把 ``max_tokens`` 拆成
``max.tokens``）。这里从 :class:`AppConfig` 展开出全部叶子路径，生成
「环境变量名 → 点号路径」的精确映射。

优先级：进程环境变量 > ``.env`` > ``config.yaml``。没有历史包袱 ——
旧的无前缀变量（``OPENAI_API_KEY``）和双下划线写法（``DOUDOU_MODEL__PRIMARY``）
都不再读取。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

from dotenv import dotenv_values
from pydantic import BaseModel

from doudou.config.schema import AppConfig
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

PREFIX = "DOUDOU_"

#: 不属于 ``AppConfig``、但同样用 ``DOUDOU_`` 前缀的启动参数。
META_ENV: dict[str, str] = {
    "DOUDOU_CONFIG_DIR": "config_dir",
    "DOUDOU_CONFIG_FILE": "config_file",
    "DOUDOU_LOCAL_CONFIG_FILE": "local_config_file",
    "DOUDOU_API_TOKEN": "api_token",
}


def _unwrap(annotation: Any) -> Any:
    """去掉 ``Optional[...]`` / ``X | None`` 外壳。"""
    if get_origin(annotation) in (Union, UnionType):
        args = [item for item in get_args(annotation) if item is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _flatten(model: type[BaseModel], prefix: str = "") -> Iterator[tuple[str, Any]]:
    """展开一个配置模型的所有叶子路径。"""
    for name, field in model.model_fields.items():
        annotation = _unwrap(field.annotation)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            yield from _flatten(annotation, f"{prefix}{name}.")
        else:
            yield f"{prefix}{name}", annotation


def _build_paths() -> dict[str, str]:
    paths: dict[str, str] = {}
    for path, _ in _flatten(AppConfig):
        name = f"{PREFIX}{path.upper().replace('.', '_')}"
        if name in paths:
            raise RuntimeError(f"环境变量名冲突：{name} 同时映射到 {paths[name]} 和 {path}")
        paths[name] = path
    return paths


#: ``DOUDOU_MODEL_PRIMARY`` → ``model.primary``。
CONFIG_ENV_PATHS: dict[str, str] = _build_paths()

#: 路径 → 类型，用于决定列表/字典要不要按 JSON 解析。
ANNOTATIONS: dict[str, Any] = dict(_flatten(AppConfig))


def read_env(*, env_file: Path | str | None = None, use_env: bool = True) -> dict[str, str]:
    """读取 ``.env`` 与进程环境变量，进程环境变量优先。"""
    if not use_env:
        return {}

    values: dict[str, str] = {}
    path = Path(env_file) if env_file is not None else Path(".env")
    if path.is_file():
        values.update(
            {key: value for key, value in dotenv_values(path).items() if value is not None}
        )
    values.update(os.environ)
    return values


def build_overrides(values: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """把环境变量整理成 ``(meta, 嵌套配置)``。"""
    meta: dict[str, str] = {}
    overrides: dict[str, Any] = {}

    known = set(CONFIG_ENV_PATHS) | set(META_ENV)
    for name in values:
        if name.startswith(PREFIX) and name not in known:
            logger.warning("config.env_unknown", variable=name)

    for name, path in CONFIG_ENV_PATHS.items():
        value = values.get(name)
        if value is not None and value != "":
            _set_path(overrides, path, _coerce(value, ANNOTATIONS[path]))

    for name, key in META_ENV.items():
        value = values.get(name)
        if value is not None and value != "":
            meta[key] = value

    return meta, overrides


def _coerce(value: str, annotation: Any) -> Any:
    """列表/字典字段按 JSON 解析；其余交给 pydantic 转换。"""
    annotation = _unwrap(annotation)
    if get_origin(annotation) in (list, dict, set, tuple):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _set_path(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = data
    for part in parts[:-1]:
        nested = current.get(part)
        if not isinstance(nested, dict):
            nested = {}
            current[part] = nested
        current = nested
    current[parts[-1]] = value


__all__ = [
    "ANNOTATIONS",
    "CONFIG_ENV_PATHS",
    "META_ENV",
    "PREFIX",
    "build_overrides",
    "read_env",
]
