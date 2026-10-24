"""Filesystem permission rules for the agent and subagents.

## 与「工作区内」的关系

工具层已经有一道硬边界：所有路径 ``resolve()`` 之后必须落在工作区内
（见 ``tools/builtin/files.py``）。这一层是**在这之上再加细粒度的限制**，
例如「工作区里可以随便读写，但 ``.env`` 和 ``secrets/`` 一律拒绝」。

因此：**没有匹配任何规则时就放行**。想做到「默认拒绝」，
写一条兜底规则即可（``paths: ["/**"], mode: deny``），并把放行规则写在它前面。

## 路径形态

规则里的 glob 匹配的是**相对工作区的 POSIX 路径**，以 ``/`` 开头：

```
/report.md              工作区根下的文件
/notes/todo.md          子目录里的文件
/**                     工作区里的任何路径（含子目录）
/**/.env                任何目录下的 .env
/secrets/**             secrets 目录及其下所有内容
/*.log                  根目录下的 .log（不递归）
```

匹配语义（自研，不用 ``fnmatch``/``PurePath.match`` —— 两者对 ``**`` 的处理
都不够明确，跨版本还可能变）：

* ``*``   匹配除 ``/`` 以外的任意字符
* ``**``  匹配任意字符，**含** ``/``
* ``?``   匹配单个非 ``/`` 字符
* ``[abc]`` 字符集

## 首匹配生效

规则按声明顺序求值，**第一条匹配的规则决定结果**。所以拒绝规则要写在放行规则前面 ——
这点和防火墙一致，也和直觉一致（先想到例外，再写通则）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from doudou.config.schema import AppConfig, PermissionOperation
from doudou.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """一次权限判定的结果。"""

    allowed: bool
    #: 给出结论的规则下标；``None`` 表示「没有任何规则匹配」。
    rule_index: int | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "rule_index": self.rule_index,
            "reason": self.reason,
        }


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """把工作区路径 glob 编译成正则。

    单独抽出来是为了能直接对匹配语义写测试 —— 安全相关的匹配逻辑
    「看起来对」是不够的。

    两个必须处理的边界（朴素翻译都会踩）：

    * ``/**/.env`` 要能匹配根目录的 ``/.env``。朴素翻译会得到 ``/.*/\\.env``，
      要求至少两层斜杠，于是**恰好漏掉最想拦的那个根目录 ``.env``**；
    * ``/secrets/**`` 要能匹配 ``/secrets`` 本身（目录），而不只是它的子项。

    因此 ``**`` 在「前面紧跟 /」时按「零或多个层级」处理，而不是「任意字符」。
    """
    out: list[str] = []
    index = 0
    length = len(pattern)

    while index < length:
        if pattern.startswith("**", index):
            preceded_by_slash = index > 0 and pattern[index - 1] == "/"
            index += 2
            if index < length and pattern[index] == "/":
                # /**/ → 零个或多个目录层级
                index += 1
                out.append("(?:.*/)?")
            elif preceded_by_slash and out and out[-1] == "/":
                # 收尾的 /** → 目录自身也可以匹配
                out.pop()
                out.append("(?:/.*)?")
            else:
                out.append(".*")
            continue

        char = pattern[index]
        if char == "*":
            out.append("[^/]*")
            index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        elif char == "[":
            closing = pattern.find("]", index + 1)
            if closing == -1:
                out.append(re.escape(char))
                index += 1
            else:
                out.append(pattern[index : closing + 1])
                index = closing + 1
        else:
            out.append(re.escape(char))
            index += 1

    return re.compile("^" + "".join(out) + "$")


def normalize_workspace_path(path: str | Path) -> str:
    """把路径规范化成规则使用的形态：``/`` 开头、正斜杠、不含 ``..``。"""
    text = str(path).replace("\\", "/").strip()
    if not text:
        return "/"
    # 折叠 ./ 与重复斜杠；不解析 ..（调用方应已 resolve 过）
    parts = [part for part in text.split("/") if part not in ("", ".")]
    return "/" + "/".join(parts)


def relative_workspace_path(root: Path, target: Path) -> str:
    """把绝对路径换算成相对工作区的 POSIX 形态。

    **落在工作区外时返回其绝对形态**（例如 ``/C:/Users/...``）——
    这是给日志用的，不能作为「在工作区内」的判断依据：
    规则里的 ``/**`` 同样会匹配那种形态。

    因此调用方应该用 :meth:`PermissionPolicy.require_target`，
    它把「在工作区内」作为规则 #0 先判一次。
    """
    resolved = target.resolve()
    if resolved == root or resolved.is_relative_to(root):
        return normalize_workspace_path(resolved.relative_to(root).as_posix())
    return normalize_workspace_path(resolved.as_posix())


class PermissionPolicy:
    """按声明顺序求值的路径权限策略。"""

    def __init__(self, config: AppConfig) -> None:
        self.rules = list(config.agent.permissions)
        self._compiled: list[list[tuple[re.Pattern[str], int]]] = []
        for rule in self.rules:
            self._compiled.append(
                [(glob_to_regex(pattern), index) for index, pattern in enumerate(rule.paths)]
            )
        if self.rules:
            logger.debug(
                "permissions.loaded",
                rules=[
                    {"mode": rule.mode, "ops": rule.operations, "paths": rule.paths}
                    for rule in self.rules
                ],
            )

    @property
    def enabled(self) -> bool:
        return bool(self.rules)

    def check(self, operation: PermissionOperation, path: str | Path) -> PermissionDecision:
        """判定一次操作是否被允许。首匹配生效，无匹配则放行。"""
        if not self.rules:
            return PermissionDecision(allowed=True, reason="未配置权限规则")

        normalized = normalize_workspace_path(path)
        for index, rule in enumerate(self.rules):
            if operation not in rule.operations:
                continue
            for compiled, _pattern_index in self._compiled[index]:
                if compiled.match(normalized):
                    allowed = rule.mode == "allow"
                    decision = PermissionDecision(
                        allowed=allowed,
                        rule_index=index,
                        reason=(f"命中规则 #{index}（{rule.mode} {rule.operations} {rule.paths}）"),
                    )
                    logger.debug(
                        "permissions.decision",
                        operation=operation,
                        path=normalized,
                        allowed=allowed,
                        rule_index=index,
                    )
                    return decision

        return PermissionDecision(allowed=True, reason="没有匹配到任何规则，按放行处理")

    def require(self, operation: PermissionOperation, path: str | Path) -> None:
        """判定并抛异常，供工具直接调用。

        Raises:
            PermissionDenied: 被规则拒绝时。消息里带上规则原文 ——
                模型看到「为什么被拒」才可能换个做法，人看到才知道改哪条配置。
        """
        decision = self.check(operation, path)
        if decision.allowed:
            return
        raise PermissionDenied(
            f"权限规则拒绝了这次 {operation} 操作：{normalize_workspace_path(path)}。"
            f"{decision.reason}。"
            "如需放开，请调整 config.yaml 的 agent.permissions（拒绝规则写在放行规则前面）。"
        )

    def describe(self) -> list[dict[str, object]]:
        """给检视接口用的快照。"""
        return [
            {
                "index": index,
                "mode": rule.mode,
                "operations": list(rule.operations),
                "paths": list(rule.paths),
            }
            for index, rule in enumerate(self.rules)
        ]

    def require_target(self, operation: PermissionOperation, target: Path, *, root: Path) -> None:
        """判定一个**绝对路径**，并先强制「必须在工作区内」。

        为什么把 containment 也放进权限层：工具层已经 resolve 过一遍，但把它
        作为规则 #0 再判一次是**纵深防御** —— 万一某天有新工具忘了检查，
        这里仍然拦得住。规则匹配本身无法区分区内外（两者都形如 ``/a/b``）。

        Raises:
            PermissionDenied: 落在工作区外，或被规则拒绝。
        """
        resolved = Path(target).resolve()
        if resolved != root and not resolved.is_relative_to(root):
            logger.warning("permissions.outside_workspace", target=str(resolved), root=str(root))
            raise PermissionDenied(
                f"路径 {resolved} 落在工作区（{root}）之外，已拒绝 {operation} 操作。"
            )
        self.require(operation, relative_workspace_path(root, resolved))


class PermissionDenied(PermissionError):
    """路径权限被拒。

    继承 ``PermissionError``（而非 ``ValueError``）：语义上它就是权限问题，
    也让「按类型捕获」的调用方自然接住。
    """


def build_policy(config: AppConfig) -> PermissionPolicy:
    return PermissionPolicy(config)


__all__ = [
    "PermissionDecision",
    "PermissionDenied",
    "PermissionPolicy",
    "build_policy",
    "glob_to_regex",
    "normalize_workspace_path",
    "relative_workspace_path",
]
