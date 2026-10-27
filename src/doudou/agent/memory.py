"""Long term memory: memory file discovery and updates.

记忆是**每轮都带**进提示词的内容（不像 Skill 只带索引），因此它的价值与代价都很直接：

* 价值 —— 「我喜欢用中文」「别给我贴原文」这类偏好不必每次重申；
* 代价 —— 每多 1000 字，每次请求就多花约 1000 token。

所以这里做三件事：**读取并合并多个记忆文件**、**注入时截断**、
**支持助手回写**（写操作有副作用，默认需要人工确认）。

写回只追加、不重写：记忆文件是人也会编辑的文本，重写会丢掉用户手写的内容。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from doudou.config.loader import Settings
from doudou.observability.logging import get_logger
from doudou.utils.text import truncate

logger = get_logger(__name__)

#: 注入系统提示词时的小标题。
MEMORY_SECTION_TITLE = "## 长期记忆"

#: 助手回写时的默认小节标题。
DEFAULT_SECTION = "## 助手记录"

#: 写回单条事实的长度上限。
MAX_FACT_CHARS = 2000


@dataclass(frozen=True, slots=True)
class MemoryContent:
    """加载结果。"""

    text: str = ""
    files: list[Path] = field(default_factory=list)
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    @property
    def chars(self) -> int:
        return len(self.text)


class MemoryStore:
    """记忆文件的读取与追加。

    可变对象，且带一个变更回调 —— 因为记忆是**在 Agent 构造时**注入提示词的，
    回写之后必须让 Agent 换掉提示词，否则用户会看到「它说记住了但下一轮还是不知道」。
    """

    def __init__(self, settings: Settings, *, max_chars: int | None = None) -> None:
        self.settings = settings
        self.max_chars = max_chars or settings.app.context.max_memory_chars
        self._paths = list(settings.memory_paths)
        self._on_change: Callable[[], Any] | None = None

    # ---------------------------------------------------------------- 查询

    @property
    def paths(self) -> list[Path]:
        return list(self._paths)

    @property
    def enabled(self) -> bool:
        """是否配置了记忆文件。没配置就没有记忆可读写。"""
        return bool(self._paths)

    @property
    def target(self) -> Path | None:
        """回写目标：第一个记忆文件（助手自己的那份，如 ``config/AGENTS.md``）。"""
        return self._paths[0] if self._paths else None

    # ---------------------------------------------------------------- 读取

    def load(self) -> MemoryContent:
        """读取并合并所有记忆文件。

        单个文件读失败只告警、不中断 —— 记忆是可选的上下文增强，
        不该因为它让整个服务起不来。
        """
        parts: list[str] = []
        used: list[Path] = []
        for path in self._paths:
            if not path.is_file():
                logger.debug("memory.file_missing", path=str(path))
                continue
            try:
                text = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("memory.read_failed", path=str(path), error=str(exc))
                continue
            if text:
                parts.append(text)
                used.append(path)

        merged = "\n\n".join(parts)
        truncated = len(merged) > self.max_chars
        if truncated:
            logger.warning(
                "memory.truncated",
                chars=len(merged),
                limit=self.max_chars,
                hint="记忆每轮都会带进提示词，请精简",
            )
        return MemoryContent(
            text=truncate(merged, self.max_chars) if truncated else merged,
            files=used,
            truncated=truncated,
        )

    def render(self) -> str:
        """渲染成提示词里的一节；没有记忆时返回空串。"""
        content = self.load()
        if content.empty:
            return ""
        return f"{MEMORY_SECTION_TITLE}\n\n{content.text}"

    # ---------------------------------------------------------------- 写入

    def append(self, fact: str, *, section: str = DEFAULT_SECTION) -> str:
        """把一条事实追加到目标文件。

        Returns:
            实际写入的文本（便于工具把它回显给用户）。

        Raises:
            ValueError: 没有配置记忆文件、或事实为空。
        """
        target = self.target
        if target is None:
            raise ValueError(
                "没有配置记忆文件，无法记录。请先在 config.yaml 的 "
                "agent.memory_files 里指定一个文件（如 config/AGENTS.md）。"
            )
        cleaned = " ".join(fact.split())
        if not cleaned:
            raise ValueError("要记录的内容为空。")
        cleaned = truncate(cleaned, MAX_FACT_CHARS)
        stamp = datetime.now().strftime("%Y-%m-%d")

        existing = ""
        if target.is_file():
            try:
                existing = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise ValueError(f"读取记忆文件失败：{exc}") from exc

        # 只追加，不重写 —— 记忆文件是人也会编辑的文本，重写会丢掉手写内容。
        entry = f"- {cleaned}（{stamp}）"
        if section.strip() and section.strip() not in existing:
            addition = f"\n\n{section.strip()}\n\n{entry}\n"
        else:
            addition = f"{entry}\n"

        # 文件末尾没换行时先补一个，避免把新条目接在上一行尾巴上。
        needs_leading_newline = bool(existing) and not existing.endswith("\n")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(f"\n{addition}" if needs_leading_newline else addition)
        except OSError as exc:
            raise ValueError(f"写入记忆文件失败：{exc}") from exc

        logger.info("memory.appended", path=str(target), chars=len(cleaned))
        self.notify_changed()
        return cleaned

    # ---------------------------------------------------------------- 变更通知

    def on_change(self, callback: Callable[[], Any]) -> None:
        """注册「记忆已变」的回调（由 factory 接到刷新提示词上）。

        回调的返回值被忽略 —— 它可能返回字符数（``refresh_memory``）或条数
        （``refresh_skills``），这里不关心。
        """
        self._on_change = callback

    def notify_changed(self) -> None:
        """触发回调；回调失败只告警 —— 文件已经写成功了，不该因此报错。"""
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception as exc:
            logger.warning("memory.change_callback_failed", error=str(exc), exc_info=True)


__all__ = [
    "DEFAULT_SECTION",
    "MAX_FACT_CHARS",
    "MEMORY_SECTION_TITLE",
    "MemoryContent",
    "MemoryStore",
]
