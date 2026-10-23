"""Parse mcp servers config: stdio, sse, streamable http, auth.

清单是**声明式**的：代码里不出现任何硬编码的 server 地址，新增一个 MCP server
只需要改 YAML。三件事在这里完成：

1. **``${ENV}`` 占位替换** —— 清单可以进仓库，密钥留在 ``.env``；
2. **严格校验** —— transport 与必填字段必须匹配（``extra="forbid"`` 挡住拼错）；
3. **名称与工具面校验** —— 重名、空白名单写法等低级错误在启动时就报出来。

刻意不做的事：这里只负责「把 YAML 变成结构化声明」，不碰 MCP 协议。
连接构造在 :mod:`doudou.mcp.loader`，拉取在 :mod:`doudou.mcp.client`。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from doudou.config.loader import Settings
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

#: 允许的 transport。``http`` 在适配器里叫 ``streamable_http``，见 loader 的映射。
TransportName = Literal["stdio", "http", "sse"]

#: server 名会成为工具前缀，因此限制字符集。
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

#: ``${VAR}`` 占位。
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class McpConfigError(ValueError):
    """清单不合法。消息会带上文件名与具体位置，避免「配置看起来没问题但连不上」。"""


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


# --------------------------------------------------------------------------- 模型


class McpServerSpec(BaseModel):
    """一个 MCP server 的声明。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool = True
    transport: TransportName = "stdio"
    description: str = ""

    # ---- stdio ----
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None

    # ---- http / sse ----
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    # ---- 工具面收敛 ----
    #: 白名单：非空时只暴露这些工具。
    include_tools: list[str] = Field(default_factory=list)
    #: 黑名单：显式屏蔽危险工具（删除、DDL、支付…）。
    exclude_tools: list[str] = Field(default_factory=list)
    #: 声明该 server 的工具都是只读的。
    #:
    #: 默认 ``False`` = 不确定 → 按「有副作用」处理、每次调用都要人工确认。
    #: 这是安全的默认：第三方 server 里既可能有查询也可能有删除，
    #: 猜错的方向必须是「多问一次」而不是「默默删了」。
    read_only: bool = False

    #: 拉取工具与调用工具的超时（秒）。
    timeout: float = 60.0
    #: 拉取失败时的重试次数（``defaults`` 可统一设置）。
    retries: int = 0

    @model_validator(mode="after")
    def _check(self) -> McpServerSpec:
        if not _NAME_PATTERN.match(self.name):
            raise McpConfigError(
                f"server 名 {self.name!r} 不合法：只允许字母、数字、下划线、连字符，"
                "且必须以字母或数字开头（它会成为工具名的前缀）。"
            )
        if self.transport == "stdio":
            if not self.command:
                raise McpConfigError(f"server {self.name!r} 使用 stdio 时必须提供 command。")
            if self.url:
                raise McpConfigError(f"server {self.name!r} 是 stdio，不应该有 url。")
        else:
            if not self.url:
                raise McpConfigError(f"server {self.name!r} 使用 {self.transport} 时必须提供 url。")
            if self.command:
                raise McpConfigError(
                    f"server {self.name!r} 是 {self.transport}，不应该有 command。"
                )
        overlap = set(self.include_tools) & set(self.exclude_tools)
        if overlap:
            raise McpConfigError(
                f"server {self.name!r} 的 include_tools 与 exclude_tools 同时包含 "
                f"{sorted(overlap)}；白名单优先，这样写只会造成困惑。"
            )
        return self

    @property
    def mcp_transport_name(self) -> str:
        """映射到 langchain-mcp-adapters 的 transport 字面量。"""
        return "streamable_http" if self.transport == "http" else self.transport

    @property
    def source_label(self) -> str:
        """工具来源标记：``mcp:<server>``。"""
        return f"mcp:{self.name}"

    def tool_allowed(self, tool_name: str) -> bool:
        """工具是否应当暴露。

        白名单优先于黑名单；两者都为空表示全部暴露。
        """
        if self.include_tools:
            return tool_name in self.include_tools
        return tool_name not in self.exclude_tools


class McpDefaults(BaseModel):
    """``defaults`` 段：被各 server 继承，**显式指定者优先**。

    只放「经常需要统一调整」的几个字段，而不是把所有字段都做成可继承 ——
    可继承字段越多，读配置时越难判断某个值到底来自哪里。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    timeout: float = 60.0
    retries: int = 0
    read_only: bool = False


class McpManifest(BaseModel):
    """整份清单。"""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
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


def load_manifest(path: Path, *, env: Mapping[str, str] | None = None) -> McpManifest:
    """读取并校验 MCP 清单。

    Args:
        path: YAML 路径。**不存在时返回空清单**（MCP 是可选项，
        没配清单不应该阻止服务启动）。
        env: 覆盖环境变量来源（测试用）。

    Raises:
        McpConfigError: 文件存在但内容不合法。
    """
    if not path.is_file():
        logger.debug("mcp.manifest_missing", path=str(path))
        return McpManifest()

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise McpConfigError(f"读取 MCP 清单失败：{path}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise McpConfigError(f"MCP 清单根节点必须是映射（mapping）：{path}")

    substituted = substitute_env(raw, source=str(path), env=env)
    try:
        manifest = McpManifest.model_validate(substituted)
    except McpConfigError:
        raise
    except Exception as exc:
        raise McpConfigError(f"MCP 清单不合法（{path}）：{exc}") from exc

    logger.info(
        "mcp.manifest_loaded",
        path=str(path),
        servers=[server.name for server in manifest.enabled_servers],
        disabled=[server.name for server in manifest.servers if not server.enabled],
    )
    return manifest


def default_manifest_path(settings: Settings) -> Path:
    """从配置里取清单路径（``mcp.config_file`` 相对 ``config/`` 解析）。"""
    return settings.mcp_config_path


__all__ = [
    "McpConfigError",
    "McpDefaults",
    "McpManifest",
    "McpServerSpec",
    "TransportName",
    "build_manifest",
    "build_spec",
    "default_manifest_path",
    "load_manifest",
    "substitute_env",
]
