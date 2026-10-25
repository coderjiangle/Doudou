"""扫描被 git 跟踪的文件里有没有「长得像凭据」的值。

## 为什么需要它

这个仓库真的发生过一次：一个包含完整会话记录的文件（里面抄着明文 API Key）
被提交并推送到了远程。事后处置只有两条 —— **轮换密钥**（唯一确定有效的）
和改历史（成本高、还不保证清干净）。所以更好的办法是**不让它进来**。

## 它检查什么、不检查什么

* 检查**被跟踪的文件内容**里是否出现高置信度的凭据形态
  （各家 key 的前缀 + 足够长度、私钥头、云厂商 id）；
* **不**检查熵、不猜「像密码的字符串」——那种规则误报太多，
  报几次无关紧要的警告之后，人就学会忽略它了，检查也就等于没有；
* `.env` 本身不在 git 里（被 ignore），因此不会误报你自己的真实凭据；
* 文档与测试里的示例值（``sk-abcdef123456``、``your-api-key`` 之类）
  通过「占位符关键词 + 长度阈值」排除：形态本身要够长（≥20 位），
  且允许行内出现 ``example`` / ``your-`` / ``xxx`` / ``placeholder`` 等标记。

用法：

```bash
uv run python scripts/check_secrets.py            # 扫被跟踪的文件
uv run python scripts/check_secrets.py --staged   # 只扫暂存区（pre-commit 用）
```

命中时退出码为 1，并打印 ``文件:行号  形态  上下文片段（已截断）``。
片段只给前若干字符 —— 检查工具自己也不该把凭据抄进日志。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: 高置信度的凭据形态。刻意只收「前缀明确 + 长度足够」的那些，压误报。
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("tavily key", re.compile(r"\btvly-[A-Za-z0-9]{20,}")),
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("github token", re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{20,}")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("aws access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("lark/feishu app secret", re.compile(r"\bcli_[a-z0-9]{16,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer literal", re.compile(r"\bBearer\s+[A-Za-z0-9._-]{30,}")),
)

#: 行内出现这些词就认为是占位/示例，不报。
_PLACEHOLDER_HINTS = (
    "your",
    "example",
    "placeholder",
    "changeme",
    "replace",
    "dummy",
    "fake",
    "sample",
    "xxx",
    "todo",
    "redact",
    "mask",
)

#: 这些文件整体豁免：
#:
#: * ``.env.example`` —— 它的职责就是展示「长什么样」；
#: * 扫描器自己的测试 —— 要测「能不能抓到这个形态」，就必须在文件里写出那个形态。
#:   里面的值全部是合成值，但形态是真的，因此按路径豁免而不是按内容豁免。
ALLOWLIST_PATHS = frozenset(
    {
        ".env.example",
        "tests/unit/test_check_secrets.py",
    }
)

#: 命中时上下文的截断长度。
CONTEXT_CHARS = 12


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    pattern: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}  {self.pattern}  {self.excerpt}…"


def is_allowlisted(path: str) -> bool:
    """整文件豁免：``.env.example``（职责就是展示形态）与扫描器自己的测试。"""
    return path.replace("\\", "/") in ALLOWLIST_PATHS


def _scan_line(line: str, *, path: str, lineno: int) -> list[Finding]:
    lowered = line.lower()
    if any(hint in lowered for hint in _PLACEHOLDER_HINTS):
        return []
    hits: list[Finding] = []
    for name, pattern in PATTERNS:
        match = pattern.search(line)
        if match is None:
            continue
        hits.append(
            Finding(
                path=path,
                line=lineno,
                pattern=name,
                # 只留前几个字符：检查工具自己也不该把凭据抄进日志。
                excerpt=match.group(0)[:CONTEXT_CHARS],
            )
        )
    return hits


def scan_text(text: str, *, path: str = "") -> list[Finding]:
    """扫描一段文本。单独抽出来是为了能离线写测试。"""
    if is_allowlisted(path):
        return []
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        findings.extend(_scan_line(line, path=path, lineno=lineno))
    return findings


def tracked_paths(*, staged: bool = False) -> list[str]:
    command = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
    if not staged:
        command = ["git", "ls-files"]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    return [item for item in result.stdout.splitlines() if item.strip()]


def scan_paths(paths: list[str]) -> list[Finding]:
    """逐个文件扫描。

    **按行流式读取，不设大小上限。** 这一点是被真实事故教出来的：
    曾经把一个 4.5 MB 的会话记录（里面抄着明文 Key）提交并推到了远程，
    而最初写的版本用「超过 4 MB 就跳过」把自己要抓的那个文件跳过了。
    检查工具漏掉最大那个文件的唯一后果，就是让人以为它没问题。
    """
    findings: list[Finding] = []
    for relative in paths:
        if is_allowlisted(relative):
            continue
        path = Path(relative)
        try:
            if not path.is_file():
                continue
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for lineno, line in enumerate(handle, start=1):
                    if lineno == 1 and "\x00" in line:
                        break  # 二进制文件：没有「行」的概念，扫了也没意义
                    findings.extend(_scan_line(line, path=relative, lineno=lineno))
        except OSError:
            continue
    return findings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="检查跟踪文件里有没有凭据")
    parser.add_argument("--staged", action="store_true", help="只检查暂存区（pre-commit 用）")
    args = parser.parse_args(argv)

    paths = tracked_paths(staged=args.staged)
    findings = scan_paths(paths)

    if not findings:
        print(f"未发现凭据形态（检查了 {len(paths)} 个文件）")
        return 0

    print(f"发现 {len(findings)} 处疑似凭据：\n")
    for item in findings:
        print(f"  {item}")
    print(
        "\n处置建议：\n"
        "  1) 把它从提交里去掉（.gitignore + git rm --cached），不要只删文件；\n"
        "  2) **轮换该凭据** —— 一旦推送到远程，改历史也不能保证清干净；\n"
        "  3) 若只是文档示例，行内加 example / your- 之类占位词即可豁免。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
