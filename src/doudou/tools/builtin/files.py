"""Local file helpers that are not part of the virtual filesystem.

读写一体的工作区文件工具。两道防线叠在一起：

1. **硬边界**（本模块）—— 所有路径 ``resolve()`` 之后必须落在工作区内。
   只检查字符串里有没有 ``..`` 是挡不住软链接的；
2. **细粒度规则**（``agent.permissions``）—— 在硬边界之上再限制具体路径，
   例如「工作区里都能写，但 ``.env`` 和 ``secrets/`` 一律拒绝」。

写类工具还声明了 ``side_effects=True``，因此默认每次调用都要人工确认。
三层各管一件事：**确认**决定「要不要做」，**权限**决定「能不能碰这个路径」，
**硬边界**保证「跑不出工作区」。

工具名用 ``workspace_*`` 前缀：前缀本身说明了「操作的边界是工作区」，
也避免日后接入任何工具时撞名 —— ``tools.registry`` 对重名是「留前者、丢后者」，
那种静默失效比报错更难查。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from doudou.agent.permissions import PermissionPolicy
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
    """构造文件工具。

    读工具始终可用；**写/改/删**同样注册，但声明了 ``side_effects=True``，
    因此在默认策略下每次调用都要人工确认，并且还要过 ``agent.permissions``
    的路径规则。两层叠加：确认解决「要不要做」，权限解决「能不能碰这个路径」。
    """
    config = settings.app.tools.files
    root = settings.root_dir
    policy = PermissionPolicy(settings.app)

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
        policy.require_target("read", target, root=root)
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
        logger.info("tools.workspace_read", path=_relative(root, target), bytes=size)
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
        policy.require_target("read", target, root=root)
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

    @tool
    def workspace_write(path: str, content: str) -> str:
        """把内容写入工作区里的文件（新建或整体覆盖）。

        Args:
            path: 相对工作区的路径，例如 ``report.md``。
            content: 完整文件内容。**会覆盖原有内容**。

        Returns:
            写入结果与字节数。

        需要「改」已有文件的一小部分时，用 workspace_edit 而不是整体覆盖 ——
        覆盖会丢掉你没看到的那部分。写之前若不确定现有内容，先 workspace_read。
        """
        target = _resolve(root, path)
        policy.require_target("write", target, root=root)
        if target.exists() and target.is_dir():
            raise FilesError(f"{path!r} 是一个目录，不能用 workspace_write 覆盖。")
        if len(content) > config.max_write_chars:
            raise FilesError(
                f"内容有 {len(content)} 字，超过单次写入上限"
                f"（{config.max_write_chars} 字）。请拆分后分次写入。"
            )
        existed = target.exists()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        except OSError as exc:
            raise FilesError(f"写入 {path!r} 失败：{exc}") from exc
        size = target.stat().st_size
        logger.info("tools.workspace_write", path=_relative(root, target), bytes=size)
        action = "已覆盖" if existed else "已创建"
        return f"{action} {path}（{size} 字节）。"

    @tool
    def workspace_edit(path: str, old: str, new: str) -> str:
        """在文件里做一次精确字符串替换。

        Args:
            path: 相对工作区的路径。
            old: 要被替换的原文，必须**在文件里唯一出现**。
            new: 替换成的内容。

        Returns:
            替换结果。

        用于改动已有文件的一小部分。``old`` 出现多次时会拒绝执行 ——
        那说明它不够具体，硬替换会改到不该改的地方。
        """
        target = _resolve(root, path)
        policy.require_target("write", target, root=root)
        if not target.is_file():
            raise FilesError(f"工作区里没有文件 {path!r}。")
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise FilesError(f"读取 {path!r} 失败：{exc}") from exc

        occurrences = text.count(old)
        if occurrences == 0:
            raise FilesError(f"在 {path!r} 里找不到要替换的原文，请先 workspace_read 确认。")
        if occurrences > 1:
            raise FilesError(
                f"要替换的原文在 {path!r} 里出现了 {occurrences} 次，无法确定改哪一处。"
                "请给出更长、更唯一的上下文。"
            )
        updated = text.replace(old, new, 1)
        if len(updated) > config.max_write_chars:
            raise FilesError(f"替换后超过单次写入上限（{config.max_write_chars} 字）。")
        try:
            target.write_text(updated, encoding="utf-8", newline="\n")
        except OSError as exc:
            raise FilesError(f"写入 {path!r} 失败：{exc}") from exc
        logger.info("tools.workspace_edit", path=_relative(root, target))
        return f"已修改 {path}（替换 1 处）。"

    @tool
    def workspace_delete(path: str) -> str:
        """删除工作区里的一个文件或空目录。

        Args:
            path: 相对工作区的路径。

        Returns:
            删除结果。

        这是不可逆操作。只在用户明确要求删除时使用；不确定就先问。
        非空目录不会被删除（避免一次删掉一堆东西）。
        """
        target = _resolve(root, path)
        policy.require_target("write", target, root=root)
        if target == root:
            raise FilesError("不能删除工作区根目录。")
        if not target.exists():
            raise FilesError(f"工作区里没有 {path!r}。")
        try:
            if target.is_dir():
                if any(target.iterdir()):
                    raise FilesError(f"目录 {path!r} 非空，请先逐个删除其中文件。")
                target.rmdir()
            else:
                target.unlink()
        except OSError as exc:
            raise FilesError(f"删除 {path!r} 失败：{exc}") from exc
        logger.info("tools.workspace_delete", path=_relative(root, target))
        return f"已删除 {path}。"

    annotate(workspace_read, group=GROUP, side_effects=False)
    annotate(workspace_list, group=GROUP, side_effects=False)
    # 写类：有副作用 → 默认需要人工确认；且都会过 policy.require("write", ...)
    annotate(workspace_write, group=GROUP, side_effects=True)
    annotate(workspace_edit, group=GROUP, side_effects=True)
    annotate(workspace_delete, group=GROUP, side_effects=True)
    return [
        workspace_read,
        workspace_list,
        workspace_write,
        workspace_edit,
        workspace_delete,
    ]


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


def _relative(root: Path, target: Path) -> str:
    """日志里用相对路径，避免把机器的绝对路径写进日志。"""
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        return str(target)


__all__ = ["GROUP", "FilesError", "build_tools"]
