"""凭据扫描器（``scripts/check_secrets.py``）。

``scripts/`` 不是包（它是工具目录，不参与 mypy 与打包），
因此这里按路径加载 —— 这段样板本身就是「为什么它不在包里」的说明。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_secrets.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_secrets", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_secrets"] = module
    spec.loader.exec_module(module)
    return module


check_secrets = _load()


# --------------------------------------------------------------------------- 命中


@pytest.mark.parametrize(
    "line",
    [
        # 全部是**合成值**：这里绝不能出现真实凭据（那正是本文件要防的事）。
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx0123456789",
        "HIMODELS_API_KEY = sk-abcdefghijklmnopqrstuvwx0123",
        '"apiKey": "sk-ant-abcdefghijklmnopqrstuvwx"',
        "token = tvly-abcdefghijklmnopqrstuvwx",
        "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz",
        "AWS_ACCESS_KEY_ID=AKIA1234567890ABCDEF",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
        "-----BEGIN RSA PRIVATE KEY-----",
    ],
)
def test_detects_credential_shapes(line: str) -> None:
    """前缀 + 长度足够的形式必须被抓住 —— 这正是那次事故里的样子。"""
    findings = check_secrets.scan_text(line, path="some/file.txt")

    assert findings, f"没抓到这个形态：{line[:24]}…"
    # 片段必须被截断：检查工具自己也不该把凭据抄进日志
    assert all(len(item.excerpt) <= check_secrets.CONTEXT_CHARS for item in findings)


def test_reports_line_number() -> None:
    text = "第一行\n第二行\nkey = sk-abcdefghijklmnopqrstuvwx\n"

    findings = check_secrets.scan_text(text, path="a.txt")

    assert [item.line for item in findings] == [3]


# --------------------------------------------------------------------------- 不误报


@pytest.mark.parametrize(
    "line",
    [
        "sk-abcdef123456",  # 太短
        "sk-should-not-be-here",  # 短且有连字符
        "your-api-key",  # 占位词
        "OPENAI_API_KEY=your-key-here-placeholder",
        "DOUDOU_API_TOKEN=",  # 空值
        "见 docs/security.md 的 example 段落：sk-abcdefghijklmnopqrstuvwx",
        "password = 'changeme-please'",
        "Bearer <your-token>",
        # 云厂商文档里的经典示例值也带 example 标记，同样豁免
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
    ],
)
def test_ignores_placeholders_and_short_values(line: str) -> None:
    """误报太多等于没有检查 —— 报几次无关紧要的警告后，人就会开始忽略它。"""
    assert check_secrets.scan_text(line, path="docs/x.md") == []


def test_allowlisted_file_is_skipped() -> None:
    """``.env.example`` 与扫描器自己的测试豁免 —— 后者的职责就是写出那个形态。"""
    line = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx"

    assert check_secrets.scan_text(line, path=".env.example") == []
    assert check_secrets.scan_text(line, path="tests/unit/test_check_secrets.py") == []
    assert check_secrets.is_allowlisted("tests\\unit\\test_check_secrets.py")  # Windows 分隔符
    assert check_secrets.scan_text(line, path=".env.local")  # 别的文件仍然要查


# --------------------------------------------------------------------------- 文件级


def test_scan_paths_streams_large_files(tmp_path: Path) -> None:
    """**大文件也要扫。**

    最初那版用「超过 4 MB 就跳过」把自己要抓的那个 4.5 MB 会话文件跳过了——
    检查工具漏掉最大那个文件的唯一后果，就是让人以为它没问题。
    """
    big = tmp_path / "session.json"
    filler = '"text": "' + ("x" * 200) + '"\n'
    with big.open("w", encoding="utf-8") as handle:
        for _ in range(30_000):  # ≈ 6 MB
            handle.write(filler)
        handle.write('"apiKey": "sk-abcdefghijklmnopqrstuvwx"\n')

    findings = check_secrets.scan_paths([str(big)])

    assert len(findings) == 1
    assert findings[0].pattern == "openai-style key"


def test_scan_paths_respects_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """文件级扫描也要走豁免 —— 只在 ``scan_text`` 里判断会让 ``scan_paths`` 绕过去。

    豁免按**仓库相对路径**匹配（扫描器收到的就是 git 给的相对路径），
    因此这里先把工作目录切到临时目录。
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text(
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx\n", encoding="utf-8"
    )
    (tmp_path / "leak.env").write_text(
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx\n", encoding="utf-8"
    )

    assert check_secrets.scan_paths([".env.example"]) == []
    assert check_secrets.scan_paths(["leak.env"])  # 换个文件名就要报


def test_scan_paths_skips_binary(tmp_path: Path) -> None:
    blob = tmp_path / "image.bin"
    blob.write_bytes(b"\x00\x01\x02sk-abcdefghijklmnopqrstuvwx\n")

    assert check_secrets.scan_paths([str(blob)]) == []


def test_scan_paths_ignores_missing_file(tmp_path: Path) -> None:
    assert check_secrets.scan_paths([str(tmp_path / "nope.txt")]) == []
