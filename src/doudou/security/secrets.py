"""Secret loading and redaction helpers."""

from __future__ import annotations

import re
from typing import Any

#: 与密钥相关的字段名模式。命中后其值在日志与 ``config show`` 中被打码。
_SENSITIVE_NAME = re.compile(
    r"(?:api[_-]?key|apikey|secret|token|password|passwd|credential|authorization)",
    re.IGNORECASE,
)

MASK = "********"


def is_sensitive(name: str) -> bool:
    """字段名是否属于敏感字段。"""
    return bool(_SENSITIVE_NAME.search(name))


def mask(value: str, *, keep: int = 4) -> str:
    """保留首尾少量字符用于辨认，其余打码。"""
    if not value:
        return ""
    if len(value) <= keep + 2:
        return MASK
    return f"{value[:keep]}{MASK}"


def redact(value: Any) -> Any:
    """递归打码字典/列表中的敏感字段值。

    非敏感字段原样保留，方便排查配置问题。
    """
    if isinstance(value, dict):
        return {
            key: (mask(item) if isinstance(item, str) and is_sensitive(str(key)) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


__all__ = ["MASK", "is_sensitive", "mask", "redact"]
