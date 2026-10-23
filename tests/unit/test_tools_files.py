"""files：只读工作区访问与路径逃逸防护。"""

from __future__ import annotations

import pytest

from doudou.config.loader import Settings
from doudou.tools.builtin.files import FilesError, build_tools


def tools_by_name(settings: Settings) -> dict[str, object]:
    return {item.name: item for item in build_tools(settings)}


@pytest.fixture
def workspace(tools_settings) -> object:  # type: ignore[no-untyped-def]
    """准备好工作区目录与其外的「敏感文件」，用于验证逃逸防护。"""
    settings = tools_settings()
    root = settings.root_dir
    root.mkdir(parents=True, exist_ok=True)
    (root / "report.md").write_text("# 报告\n\n内容", encoding="utf-8")
    (root / "sub").mkdir(exist_ok=True)
    (root / "sub" / "note.txt").write_text("嵌套文件", encoding="utf-8")
    return settings


def test_read_file(workspace) -> None:  # type: ignore[no-untyped-def]
    tools = tools_by_name(workspace)

    assert "内容" in tools["workspace_read"].invoke({"path": "report.md"})


def test_read_nested_file(workspace) -> None:  # type: ignore[no-untyped-def]
    tools = tools_by_name(workspace)

    assert "嵌套文件" in tools["workspace_read"].invoke({"path": "sub/note.txt"})


def test_list_files(workspace) -> None:  # type: ignore[no-untyped-def]
    tools = tools_by_name(workspace)

    listed = tools["workspace_list"].invoke({})

    assert "report.md" in listed
    assert "sub/note.txt" in listed
    assert "共 2 个文件" in listed


def test_list_subdirectory(workspace) -> None:  # type: ignore[no-untyped-def]
    tools = tools_by_name(workspace)

    assert "sub/note.txt" in tools["workspace_list"].invoke({"directory": "sub"})


def test_list_empty_workspace(tools_settings) -> None:  # type: ignore[no-untyped-def]
    settings = tools_settings()
    settings.root_dir.mkdir(parents=True, exist_ok=True)

    assert "还没有任何文件" in tools_by_name(settings)["workspace_list"].invoke({})


def test_read_missing_file_lists_alternatives(workspace) -> None:  # type: ignore[no-untyped-def]
    tools = tools_by_name(workspace)

    with pytest.raises(FilesError, match="report"):
        tools["workspace_read"].invoke({"path": "nope.md"})


# --------------------------------------------------------------------------- 路径防护


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "sub/../../outside.txt", "..", "./../x"],
)
def test_traversal_is_rejected(workspace, path: str) -> None:  # type: ignore[no-untyped-def]
    tools = tools_by_name(workspace)

    with pytest.raises(FilesError, match="工作区之外"):
        tools["workspace_read"].invoke({"path": path})


def test_absolute_path_is_rejected(workspace) -> None:  # type: ignore[no-untyped-def]
    tools = tools_by_name(workspace)

    with pytest.raises(FilesError, match="相对工作区"):
        tools["workspace_read"].invoke({"path": "C:/Windows/win.ini"})


def test_empty_path_is_rejected(workspace) -> None:  # type: ignore[no-untyped-def]
    tools = tools_by_name(workspace)

    with pytest.raises(FilesError, match="路径不能为空"):
        tools["workspace_read"].invoke({"path": "  "})


def test_symlink_escape_is_rejected(workspace, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """字符串里没有 ``..`` 也可能跑出去 —— 靠 resolve() 之后比较才拦得住。"""
    root = workspace.root_dir
    outside = tmp_path / "outside.txt"
    outside.write_text("工作区外的内容", encoding="utf-8")
    link = root / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")

    with pytest.raises(FilesError, match="工作区之外"):
        tools_by_name(workspace)["workspace_read"].invoke({"path": "link.txt"})


def test_large_file_is_refused_before_reading(workspace) -> None:  # type: ignore[no-untyped-def]
    """超大文件在读之前就被挡掉，避免一次性把内存吃光。"""
    root = workspace.root_dir
    limit = workspace.app.tools.files.max_read_chars
    (root / "huge.txt").write_text("x" * (limit * 20), encoding="utf-8")

    with pytest.raises(FilesError, match="超过单次读取上限"):
        tools_by_name(workspace)["workspace_read"].invoke({"path": "huge.txt"})


def test_read_truncates_within_limit(workspace) -> None:  # type: ignore[no-untyped-def]
    root = workspace.root_dir
    limit = workspace.app.tools.files.max_read_chars
    (root / "big.txt").write_text("y" * (limit + 1000), encoding="utf-8")

    text = tools_by_name(workspace)["workspace_read"].invoke({"path": "big.txt"})

    assert "已截断" in text


def test_non_utf8_file_is_reported(workspace) -> None:  # type: ignore[no-untyped-def]
    (workspace.root_dir / "binary.bin").write_bytes(b"\xff\xfe\x00\x01")

    with pytest.raises(FilesError, match="不是 UTF-8"):
        tools_by_name(workspace)["workspace_read"].invoke({"path": "binary.bin"})


def test_list_missing_directory(workspace) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(FilesError, match="没有目录"):
        tools_by_name(workspace)["workspace_list"].invoke({"directory": "nope"})


def test_files_error_is_a_value_error() -> None:
    assert issubclass(FilesError, ValueError)
