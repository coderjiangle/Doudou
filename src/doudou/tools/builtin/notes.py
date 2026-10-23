"""Personal notes and todo capture tools.

笔记落在 ``tools.notes.directory`` 下的独立目录里，一个笔记一个 Markdown 文件。

写笔记是**有副作用**的操作（``side_effects=True``），因此在默认策略下
每次写入都会先要人工确认。删除/覆盖已有笔记不在本里程碑内 ——
那类破坏性操作要和 M8 的权限规则一起放行。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from doudou.config.loader import Settings
from doudou.observability.logging import get_logger
from doudou.tools.decorators import annotate
from doudou.utils.text import truncate

logger = get_logger(__name__)

GROUP = "notes"

#: 文件名里不允许出现的字符（Windows 与 POSIX 的并集）。
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MAX_STEM_CHARS = 60


class NotesError(ValueError):
    """笔记操作失败。消息会直接回填给模型。"""


def build_tools(settings: Settings) -> Sequence[BaseTool]:
    """构造笔记工具集。"""
    config = settings.app.tools.notes
    directory = Path(config.directory).expanduser()
    if not directory.is_absolute():
        directory = Path.cwd() / directory
    root = directory.resolve()

    @tool
    def note_save(title: str, content: str) -> str:
        """把一段内容保存为笔记。

        Args:
            title: 笔记标题，同时用作文件名。同名笔记会被**覆盖**。
            content: 笔记正文，Markdown 格式。

        Returns:
            保存结果，包含最终文件名。

        当用户说「记一下」「帮我记住」「存成笔记」时使用。
        用户只是随口提到信息、并没有要求记录时不要主动保存。
        """
        stem = safe_stem(title)
        path = _resolve(root, stem)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        body = f"# {title.strip()}\n\n> 更新于 {stamp}\n\n{content.strip()}\n"
        existed = path.exists()
        try:
            root.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        except OSError as exc:
            raise NotesError(f"保存笔记失败：{exc}") from exc
        action = "已覆盖" if existed else "已保存"
        logger.info("tools.note_saved", file=path.name, overwritten=existed, chars=len(content))
        return f"{action}笔记 {path.name}（{len(content)} 字）。"

    @tool
    def note_list() -> str:
        """列出已保存的笔记。

        Args:
            无。

        Returns:
            笔记文件名与更新时间；没有笔记时说明目录为空。
        """
        if not root.is_dir():
            return "还没有任何笔记。"
        entries = sorted(
            (item for item in root.glob("*.md") if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if not entries:
            return "还没有任何笔记。"

        lines = [f"共 {len(entries)} 篇笔记（按最近更新排序）："]
        for item in entries[: config.max_list]:
            stamp = datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            lines.append(f"- {item.name}（{stamp}）")
        if len(entries) > config.max_list:
            lines.append(f"（仅显示前 {config.max_list} 篇）")
        return "\n".join(lines)

    @tool
    def note_read(name: str) -> str:
        """读取一篇笔记的完整内容。

        Args:
            name: 笔记名，可以是 ``todo.md`` 或 ``todo``。

        Returns:
            笔记正文；不存在时列出可选的笔记名。
        """
        path = _resolve(root, safe_stem(name))
        if not path.is_file():
            available = sorted(item.stem for item in root.glob("*.md")) if root.is_dir() else []
            hint = "、".join(available[:20]) if available else "（目录为空）"
            raise NotesError(f"没有名为 {name!r} 的笔记。现有笔记：{hint}")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise NotesError(f"读取笔记失败：{exc}") from exc
        return truncate(text.strip(), config.max_read_chars)

    annotate(note_save, group=GROUP, side_effects=True)
    annotate(note_list, group=GROUP)
    annotate(note_read, group=GROUP)
    return [note_save, note_list, note_read]


def safe_stem(title: str) -> str:
    """把标题转成安全的文件名主干。

    做三件事：替换非法字符、挡掉会导致路径穿越或非法文件的输入、
    去掉结尾的 ``.md``（这样 ``note_read("todo.md")`` 与 ``note_read("todo")``
    指向同一篇，而不会变成 ``todo.md.md``）。
    """
    cleaned = _UNSAFE_CHARS.sub("-", title.strip())
    cleaned = cleaned.strip(" .-")
    if not cleaned or set(cleaned) == {"."}:
        raise NotesError(f"标题 {title!r} 无法作为文件名，请换一个更具体的标题。")
    if cleaned.lower().endswith(".md"):
        cleaned = cleaned[:-3].strip(" .-")
        if not cleaned:
            raise NotesError(f"标题 {title!r} 去掉扩展名后为空，请换一个更具体的标题。")
    # 直接切片而不是用 truncate()：后者的「已截断 N 字」后缀会把文件名重新撑长。
    return cleaned[:_MAX_STEM_CHARS]


def _resolve(root: Path, stem: str) -> Path:
    """拼出笔记的绝对路径，并确认它没有跑出笔记目录。"""
    path = (root / f"{stem}.md").resolve()
    if not path.is_relative_to(root):
        raise NotesError(f"笔记名 {stem!r} 会写到笔记目录之外，已拒绝。")
    return path


__all__ = ["GROUP", "NotesError", "build_tools", "safe_stem"]
