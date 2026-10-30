"""Self-inspection tool: 查询本助手的配置，只读，不能修改。

为什么单独做成一个工具：用户经常问「你用的什么模型」「你的工作区在哪」
「为什么这个工具不能用了」。这些答案都在 ``Settings`` 里，但直接把它塞进提示词
既浪费上下文、又会随配置变动而失真。让模型按需查是最合适的。

安全边界：

* **只读**：没有 setter，也不写文件；声明 ``side_effects=False``。
* **密钥只报状态**：``credentials`` 里只有 true/false，绝不返回值；
  其余字段里命中 ``secret`` / ``token`` / ``api_key`` 之类的值统一打码，
  数据库 URL 里的密码也会被遮掉。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from langchain_core.tools import BaseTool, tool

from doudou.config.loader import Settings
from doudou.security.secrets import MASK, is_sensitive
from doudou.tools.decorators import annotate, annotations_of
from doudou.utils.text import truncate

if TYPE_CHECKING:
    from doudou.tools.registry import GroupStatus

GROUP = "meta"

#: 一次返回的上限，避免用户问「全部配置」时把上下文塞满。
MAX_OUTPUT_CHARS = 8000

#: 概览里展示的关键配置。
_OVERVIEW = (
    "runtime.env",
    "runtime.timezone",
    "model.primary",
    "model.temperature",
    "model.max_tokens",
    "agent.name",
    "agent.max_steps",
    "agent.max_tool_calls_per_step",
    "agent.approval_timeout_seconds",
    "filesystem.root_dir",
    "storage.history",
    "context.max_input_tokens",
    "server.host",
    "server.port",
    "server.base_path",
    "tools.loaded_names",
)

#: 密钥类字段：只显示是否已配置。
_CREDENTIAL_FIELDS = (
    "openai_api_key",
    "anthropic_api_key",
    "deepseek_api_key",
    "tavily_api_key",
    "feishu_app_id",
    "feishu_app_secret",
    "feishu_verification_token",
    "feishu_encrypt_key",
)

#: URL 里的用户名/密码：``postgres://user:pass@host`` → ``postgres://user:********@host``。
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[a-zA-Z][\w+.-]*://)(?P<user>[^:/@\s]+):(?P<password>[^@/\s]+)@"
)


def build_tools(
    settings: Settings,
    *,
    tools: Sequence[BaseTool] = (),
    groups: Sequence[GroupStatus] = (),
) -> Sequence[BaseTool]:
    """构造配置自检工具。

    Args:
        settings: 当前生效配置。
        tools: **已经装配好**的工具列表；``my`` 自己不在里面，展示时会补上。
        groups: 各内置分组的启用状态与原因，来自 ``registry.describe_groups``；
            用于回答「某个能力为什么没生效」。
    """
    data = _snapshot(settings, tools, groups)
    sections = " / ".join(sorted(data))

    def overview() -> str:
        lines = ["my · 配置概览"]
        for path in _OVERVIEW:
            value = _resolve(data, path)
            if value is _MISSING:
                continue
            lines.append(f"{path} = {_format_value(value)}")
        lines.append("")
        lines.append(f"可用 section：{sections}")
        lines.append('也可以直接查某个键，例如 my(section="agent.max_steps")')
        return truncate("\n".join(lines), MAX_OUTPUT_CHARS)

    def unknown(section: str) -> str:
        return "\n".join(
            [
                f"my · 没有 section {section!r}",
                f"可用 section：{sections}",
                '也可以直接查某个键，例如 my(section="agent.max_steps")',
            ]
        )

    def render(title: str, value: Any) -> str:
        if not isinstance(value, (dict, list)):
            return f"{title} = {_format_value(value)}"
        lines = [f"my · {title}"]
        _flatten(title, value, lines)
        return truncate("\n".join(lines), MAX_OUTPUT_CHARS)

    @tool
    def my(section: str = "") -> str:
        """查看本助手当前的配置（只读，不会修改任何设置）。

        Args:
            section: 想看的部分：model / agent / tools / channels / storage /
                context / mcp / server / observability / runtime / filesystem /
                paths / credentials / groups；也可以写具体键，例如
                "agent.max_steps"、"tools.web_search"。留空或传 "all" 返回概览。

        Returns:
            配置项与当前值的文本列表。密钥只会显示是否已配置（true/false），
            不会返回值。

        用户问「你用什么模型」「有什么限制」「工作区在哪」「某个能力开没开」
        「密钥配好了吗」「为什么这个工具不能用」这类问题时使用。``groups`` 会给出
        每个内置分组是否启用以及原因。**不能改配置**：要修改只能让用户改
        ``config.yaml`` / ``.env`` 并重启进程。
        """
        key = section.strip().strip(".").lower()
        if not key or key in {"all", "overview"}:
            return overview()

        value = _resolve(data, key)
        if value is _MISSING:
            return unknown(section)
        return render(key, value)

    annotate(my, group=GROUP, side_effects=False)
    return [my]


def _snapshot(
    settings: Settings,
    tools: Sequence[BaseTool],
    groups: Sequence[GroupStatus] = (),
) -> dict[str, Any]:
    """把当前配置整理成可查询的树，并在这一步完成脱敏。"""
    app = settings.app.model_dump(mode="json")

    loaded = [
        {
            "name": item.name,
            "group": annotations_of(item).group,
            "side_effects": annotations_of(item).side_effects,
        }
        for item in tools
    ]
    # my 自己不在传入的列表里（它正是在被构造），补上以免清单看起来少一个。
    loaded.append({"name": "my", "group": GROUP, "side_effects": False})

    tools_section = dict(app["tools"])
    tools_section["loaded"] = loaded
    tools_section["loaded_names"] = [entry["name"] for entry in loaded]

    credentials = {name: bool(getattr(settings.secrets, name, None)) for name in _CREDENTIAL_FIELDS}
    credentials["api_token"] = bool(settings.api_token)

    data: dict[str, Any] = {
        "runtime": app["runtime"],
        "server": app["server"],
        "model": app["model"],
        "agent": app["agent"],
        "filesystem": app["filesystem"],
        "tools": tools_section,
        "channels": app["channels"],
        "storage": app["storage"],
        "context": app["context"],
        "mcp": app["mcp"],
        "observability": app["observability"],
        "groups": {item.name: {"enabled": item.enabled, "reason": item.reason} for item in groups},
        "paths": {
            "config_dir": str(settings.config_dir),
            "config_file": str(settings.config_file) if settings.config_file else None,
            "loaded_files": [str(item) for item in settings.loaded_files],
            "root_dir": str(settings.root_dir),
            "system_prompt": str(settings.system_prompt_path)
            if settings.system_prompt_path
            else None,
            "skills_dirs": [str(item) for item in settings.skills_dirs],
        },
        "credentials": credentials,
    }
    return cast("dict[str, Any]", _sanitize(data))


_MISSING = object()


def _sanitize(value: Any) -> Any:
    """递归脱敏：敏感字段整体打码，普通字符串里的 URL 凭据也遮掉。"""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if is_sensitive(str(key)):
                cleaned[key] = _mask_tree(item)
            else:
                cleaned[key] = _sanitize(item)
        return cleaned
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return _URL_CREDENTIALS.sub(rf"\g<scheme>\g<user>:{MASK}@", value)
    return value


def _mask_tree(value: Any) -> Any:
    """敏感字段下的所有字符串都打码（保留字典的键名）。"""
    if isinstance(value, str):
        return MASK
    if isinstance(value, dict):
        return {key: _mask_tree(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_tree(item) for item in value]
    return value


def _resolve(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _format_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value if value else '""'
    return json.dumps(value, ensure_ascii=False, default=str)


def _flatten(prefix: str, value: Any, lines: list[str]) -> None:
    if isinstance(value, dict):
        if not value:
            lines.append(f"{prefix} = {{}}")
            return
        for key, item in value.items():
            _flatten(f"{prefix}.{key}" if prefix else str(key), item, lines)
        return
    if isinstance(value, list):
        if not value:
            lines.append(f"{prefix} = []")
            return
        for index, item in enumerate(value):
            _flatten(f"{prefix}[{index}]", item, lines)
        return
    lines.append(f"{prefix} = {_format_value(value)}")


__all__ = ["GROUP", "build_tools"]
