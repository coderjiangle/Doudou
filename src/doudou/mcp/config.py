"""Build the runtime MCP manifest from ``config.yaml``'s ``mcp`` section.

server 清单**写在 ``config/config.yaml`` 的 ``mcp`` 段里**（不再有单独的
``mcp.servers.yaml``），本模块负责把它变成 :class:`McpManifest`：

1. **``${ENV}`` 占位替换** —— config.yaml 可以进仓库，密钥留在 ``.env``；
   变量缺失时**直接报错**，而不是把占位符原样发出去换来一个 401；
2. **工具面与名称校验** —— 重名、白名单/黑名单冲突等在启动时就报出来
   （结构校验在 :mod:`doudou.config.schema` 已完成，这里只处理替换后的值）。

``McpServerSpec`` / ``McpDefaults`` 是配置结构的一部分，定义在
``config/schema.py``；这里再导出一次，让 MCP 内部只 import 本模块。

不做的事：这里只负责「把配置变成结构化声明」，不碰 MCP 协议。
连接构造在 :mod:`doudou.mcp.loader`，拉取在 :mod:`doudou.mcp.client`。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from doudou.config.schema import (
    McpConfig,
    McpDefaults,
    McpServerSpec,
    McpTransportName,
)
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

#: 兼容旧名字：MCP 内部一直用 ``TransportName``。
TransportName = McpTransportName

#: ``${VAR}`` 占位。
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class McpConfigError(ValueError):
    """MCP 配置不合法。消息会带上具体位置，避免「配置看起来没问题但连不上」。"""


# --------------------------------------------------------------------------- 环境变量替换


def substitute_env(value: Any, *, source: str = "", env: Mapping[str, str] | None = None) -> Any:
    """把字符串里的 ``${VAR}`` 换成环境变量的值。

    递归处理 dict / list。**变量缺失时抛错而不是留原样** ——
    把 ``Bearer ${GITHUB_TOKEN}`` 原样发出去只会得到一个莫名其妙的 401。

    Args:
        value: 任意结构。
        source: 出错信息里标注来源（一般是文件路径）。
        env: 变量来源；默认读进程环境（测试可注入自己的映射）。
    """
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda match: _lookup(match.group(1), source, env), value)
    if isinstance(value, dict):
        return {key: substitute_env(item, source=source, env=env) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute_env(item, source=source, env=env) for item in value]
    return value


def _lookup(name: str, source: str, env: Mapping[str, str] | None) -> str:
    source_map = os.environ if env is None else env
    raw = source_map.get(name)
    if raw is None:
        where = f"（{source}）" if source else ""
        raise McpConfigError(
            f"MCP 清单引用了未设置的环境变量 ${{{name}}}{where}。"
            "请把它加到 .env，或从清单里删掉这一项。"
        )
    return raw


class McpManifest(BaseModel):
    """运行期清单：配置里已经校验过的 server 列表 + 继承后的默认值。"""

    model_config = ConfigDict(extra="forbid")

    defaults: McpDefaults = Field(default_factory=McpDefaults)
    servers: list[McpServerSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique(self) -> McpManifest:
        seen: set[str] = set()
        for server in self.servers:
            if server.name in seen:
                raise McpConfigError(f"server 名重复：{server.name!r}")
            seen.add(server.name)
        return self

    @property
    def enabled_servers(self) -> list[McpServerSpec]:
        return [server for server in self.servers if server.enabled]

    def by_name(self, name: str) -> McpServerSpec | None:
        for server in self.servers:
            if server.name == name:
                return server
        return None


# --------------------------------------------------------------------------- 加载


def build_spec(payload: dict[str, Any]) -> McpServerSpec:
    """构造并校验一个 server 声明。

    存在的理由：pydantic 会把 ``model_validator`` 里抛出的异常**包装**成
    ``ValidationError``，于是 ``except McpConfigError`` 就再也抓不到了。
    这里统一转回来，保证模块对外只有一种错误类型 —— 否则每个调用方都要
    同时处理两种异常，迟早漏掉一个。
    """
    try:
        return McpServerSpec.model_validate(payload)
    except ValidationError as exc:
        raise McpConfigError(_readable(exc)) from exc


def build_manifest(payload: dict[str, Any]) -> McpManifest:
    """构造并校验整份清单，并把 ``defaults`` 合并进各 server。

    合并依据 ``model_fields_set``：**只有没被显式写过的字段**才用默认值填充，
    因此「全局默认 60s，某个 server 单独写 5s」这种写法符合直觉。
    """
    try:
        manifest = McpManifest.model_validate(payload)
    except ValidationError as exc:
        raise McpConfigError(_readable(exc)) from exc

    defaults = manifest.defaults
    for spec in manifest.servers:
        for field_name in ("enabled", "timeout", "retries", "read_only"):
            if field_name not in spec.model_fields_set:
                setattr(spec, field_name, getattr(defaults, field_name))
    return manifest


def _readable(exc: ValidationError) -> str:
    """把 pydantic 的错误压成一行可读信息。"""
    parts: list[str] = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        message = str(item.get("msg", "")).removeprefix("Value error, ")
        parts.append(f"{location}: {message}" if location else message)
    return "；".join(parts) or str(exc)


def manifest_from_config(config: McpConfig, *, env: Mapping[str, str] | None = None) -> McpManifest:
    """把 ``config.yaml`` 的 ``mcp`` 段变成运行期清单。

    只做两件事：替换 ``${ENV}`` 占位、把已经校验过的 spec 重新组装成清单
    （重新组装是为了让替换后的值再走一遍校验，并统一抛出 ``McpConfigError``）。

    Args:
        config: ``settings.app.mcp``。
        env: 变量来源；默认读进程环境（测试可注入自己的映射）。

    Raises:
        McpConfigError: 占位变量缺失，或替换后的值不合法。

    Notes:
        没有配任何 server 时返回空清单 —— MCP 是可选项，没配不该影响启动。
    """
    defaults = config.defaults
    if not config.servers:
        return McpManifest(defaults=defaults)

    payload: dict[str, Any] = {
        "defaults": defaults.model_dump(),
        "servers": [server.model_dump() for server in config.servers],
    }
    substituted = substitute_env(payload, source="config.yaml 的 mcp.servers", env=env)
    manifest = build_manifest(substituted)

    logger.info(
        "mcp.manifest_loaded",
        source="config.yaml: mcp.servers",
        servers=[server.name for server in manifest.enabled_servers],
        disabled=[server.name for server in manifest.servers if not server.enabled],
    )
    return manifest


__all__ = [
    "McpConfigError",
    "McpDefaults",
    "McpManifest",
    "McpServerSpec",
    "TransportName",
    "build_manifest",
    "build_spec",
    "manifest_from_config",
    "substitute_env",
]
