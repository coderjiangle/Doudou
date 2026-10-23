"""Local file helpers that are not part of the virtual filesystem.

只读版本：本里程碑只让助手「看得见」工作区里的文件，写入与删除留给 M8
（要和权限规则一起放行）。

读取根目录固定为 ``filesystem.root_dir``（默认 ``./data/workspace``）。
所有路径都会先解析成绝对路径再校验是否仍在根目录内 ——
只靠字符串里有没有 ``..`` 是挡不住的（软链接、``%2e%2e``、绝对路径都能绕过）。

工具名刻意用 ``workspace_*`` 前缀：M9 接入 deepagents 的
``FilesystemMiddleware`` 时会提供 ``read_file`` / ``ls``，重名会被
``tools.registry`` 判为冲突并丢掉后者，这里避免那种静默失效。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from doudou.config.loader import Settings
from doudou.observability.logging import get_logger
from doudou.tools.decorators import annotate
from doudou.utils.text import truncate

logger = get_logger(__name__)

GROUP = "files"

#: 拒绝读取超过「单次上限 × 这个倍数」的文件。
#: 否则一次误读一个 GB 级文件就会把内存吃光（截断发生在读完之后）。
SIZE_GUARD_FACTOR = 8


class FilesError(ValueError):
    """文件访问失败。消息会直接回填给模型。"""


def build_tools(settings: Settings) -> Sequence[BaseTool]:
    """构造只读文件工具。"""
    config = settings.app.tools.files
    root = settings.root_dir

    @tool
    def workspace_read(path: str) -> str:
        """读取工作区里的一个文件。

        Args:
            path: 相对工作区的路径，例如 ``report.md``、``notes/todo.md``。

        Returns:
            文件内容（超长会截断）；文件不存在时给出工作区里有哪些文件。

        只在确实需要文件原文时使用。工作区是空的时应先确认路径，
        可以先用 workspace_list 看看有什么。
        """
        target = _resolve(root, path)
        if not target.is_file():
            raise FilesError(
                f"工作区里没有文件 {path!r}。现有文件：{_preview_files(root, config.max_list)}"
            )

        size = target.stat().st_size
        guard = config.max_read_chars * SIZE_GUARD_FACTOR
        if size > guard:
            raise FilesError(
                f"文件 {path!r} 有 {size} 字节，超过单次读取上限，已拒绝加载。"
                "请用其它方式处理（例如先拆分）。"
            )
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise FilesError(f"文件 {path!r} 不是 UTF-8 文本，无法读取。") from exc
        except OSError as exc:
            raise FilesError(f"读取 {path!r} 失败：{exc}") from exc

        content = truncate(text.strip(), config.max_read_chars)
        logger.info("tools.workspace_read", path=str(target.relative_to(root)), bytes=size)
        return content

    @tool
    def workspace_list(directory: str = "") -> str:
        """列出工作区里的文件。

        Args:
            directory: 相对工作区的子目录；留空表示工作区根目录。

        Returns:
            文件与子目录清单；目录为空或不存在时说明情况。
        """
        target = _resolve(root, directory) if directory.strip() else root
        if not target.is_dir():
            raise FilesError(f"工作区里没有目录 {directory!r}。")

        files = sorted(
            (item for item in target.rglob("*") if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if not files:
            shown = target.relative_to(root)
            where = "工作区根目录" if str(shown) == "." else f"目录 {shown}"
            return f"{where}还没有任何文件。"

        lines = [f"共 {len(files)} 个文件（按最近修改排序）："]
        for item in files[: config.max_list]:
            lines.append(f"- {item.relative_to(root).as_posix()}（{item.stat().st_size} 字节）")
        if len(files) > config.max_list:
            lines.append(f"（仅显示前 {config.max_list} 个）")
        return "\n".join(lines)

    annotate(workspace_read, group=GROUP)
    annotate(workspace_list, group=GROUP)
    return [workspace_read, workspace_list]


def _resolve(root: Path, path: str) -> Path:
    """拼出绝对路径并确认它仍在工作区内。

    先 ``resolve()`` 再比较，因此软链接与 ``..`` 都会被展开后拦下。
    """
    if not path.strip():
        raise FilesError("路径不能为空。")
    candidate = Path(path.strip().replace("\\", "/"))
    if candidate.is_absolute():
        raise FilesError("请给相对工作区的路径，不要用绝对路径。")
    resolved = (root / candidate).resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise FilesError(f"路径 {path!r} 指向工作区之外，已拒绝访问。")
    return resolved


def _preview_files(root: Path, limit: int) -> str:
    if not root.is_dir():
        return "（工作区目录还不存在）"
    names = [
        item.relative_to(root).as_posix() for item in sorted(root.rglob("*")) if item.is_file()
    ]
    return "、".join(names[:limit]) if names else "（工作区为空）"


__all__ = ["GROUP", "FilesError", "build_tools"]
