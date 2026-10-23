"""notes：保存、列出、读取，以及文件名与路径的防护。"""

from __future__ import annotations

import pytest

from doudou.config.loader import Settings
from doudou.tools.builtin.notes import NotesError, build_tools, safe_stem


def tools_by_name(settings: Settings) -> dict[str, object]:
    return {item.name: item for item in build_tools(settings)}


# --------------------------------------------------------------------------- 文件名


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("购物清单", "购物清单"),
        ("Weekly Plan", "Weekly Plan"),
        ("a/b\\c:d*e?f", "a-b-c-d-e-f"),
        ("  前后空格  ", "前后空格"),
        ("点号。保留", "点号。保留"),
    ],
)
def test_safe_stem(title: str, expected: str) -> None:
    assert safe_stem(title) == expected


@pytest.mark.parametrize("title", ["", "   ", "...", "///"])
def test_safe_stem_rejects_unusable_titles(title: str) -> None:
    with pytest.raises(NotesError, match="无法作为文件名"):
        safe_stem(title)


def test_safe_stem_truncates() -> None:
    stem = safe_stem("很长的标题" * 40)

    assert len(stem) <= 60


# --------------------------------------------------------------------------- 基本流程


def test_note_save_then_read(tools_settings) -> None:  # type: ignore[no-untyped-def]
    tools = tools_by_name(tools_settings())

    saved = tools["note_save"].invoke({"title": "购物清单", "content": "- 牛奶\n- 面包"})
    read = tools["note_read"].invoke({"name": "购物清单"})

    assert "已保存笔记 购物清单.md" in saved
    assert read.startswith("# 购物清单")
    assert "- 牛奶" in read


def test_note_read_accepts_name_without_extension(tools_settings) -> None:  # type: ignore[no-untyped-def]
    tools = tools_by_name(tools_settings())
    tools["note_save"].invoke({"title": "todo", "content": "写文档"})

    assert "写文档" in tools["note_read"].invoke({"name": "todo.md"})


def test_note_save_reports_overwrite(tools_settings) -> None:  # type: ignore[no-untyped-def]
    tools = tools_by_name(tools_settings())

    tools["note_save"].invoke({"title": "草稿", "content": "第一版"})
    second = tools["note_save"].invoke({"title": "草稿", "content": "第二版"})

    assert "已覆盖" in second
    assert "第二版" in tools["note_read"].invoke({"name": "草稿"})


def test_note_list_is_empty_first(tools_settings) -> None:  # type: ignore[no-untyped-def]
    assert tools_by_name(tools_settings())["note_list"].invoke({}) == "还没有任何笔记。"


def test_note_list_shows_saved_notes(tools_settings) -> None:  # type: ignore[no-untyped-def]
    tools = tools_by_name(tools_settings())
    tools["note_save"].invoke({"title": "甲", "content": "1"})
    tools["note_save"].invoke({"title": "乙", "content": "2"})

    listed = tools["note_list"].invoke({})

    assert "共 2 篇笔记" in listed
    assert "甲.md" in listed and "乙.md" in listed


def test_note_read_missing_lists_available(tools_settings) -> None:  # type: ignore[no-untyped-def]
    tools = tools_by_name(tools_settings())
    tools["note_save"].invoke({"title": "存在", "content": "x"})

    with pytest.raises(NotesError, match="现有笔记：存在"):
        tools["note_read"].invoke({"name": "不存在"})


def test_note_save_creates_directory(tools_settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """笔记目录不存在时应自动创建，而不是报错。"""
    settings = tools_settings()
    notes_dir = (tmp_path / "notes").resolve()
    assert not notes_dir.exists()

    tools_by_name(settings)["note_save"].invoke({"title": "第一篇", "content": "x"})

    assert (notes_dir / "第一篇.md").is_file()


# --------------------------------------------------------------------------- 路径防护


def test_note_read_cannot_escape_directory(tools_settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """``..`` 会被替换掉，因此拼不出上层路径。"""
    tools = tools_by_name(tools_settings())
    outer = tmp_path / "secret.md"
    outer.write_text("不该被读到", encoding="utf-8")

    with pytest.raises(NotesError):
        tools["note_read"].invoke({"name": "../secret"})


def test_note_save_with_traversal_like_title_stays_inside(tools_settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    tools = tools_by_name(tools_settings())

    tools["note_save"].invoke({"title": "../../escape", "content": "x"})

    notes_dir = (tmp_path / "notes").resolve()
    written = list(notes_dir.glob("*.md"))
    assert len(written) == 1
    assert written[0].parent == notes_dir


def test_note_read_truncates_long_notes(tools_settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = tools_settings()
    tools = tools_by_name(settings)
    tools["note_save"].invoke(
        {"title": "长文", "content": "字" * (settings.app.tools.notes.max_read_chars + 500)}
    )

    read = tools["note_read"].invoke({"name": "长文"})

    assert "已截断" in read


def test_notes_error_is_a_value_error() -> None:
    assert issubclass(NotesError, ValueError)
