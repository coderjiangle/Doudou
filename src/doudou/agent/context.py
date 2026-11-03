"""上下文工程：token 预算、摘要压缩、大结果卸载。

三个机制对应三种不同的「上下文被顶坏」的方式：

| 问题 | 机制 | 不做会怎样 |
| --- | --- | --- |
| 历史无限增长 | 按 token 预算**裁剪** | 请求被 provider 拒绝 |
| 裁剪等于丢信息 | **摘要压缩** | 助手「忘了刚才说的事」 |
| 单条结果过大 | **卸载到文件** | 一次 5 万字返回挤掉后续所有请求 |

压缩是**有损**的，这是它最大的风险：出错时的表现是「助手悄悄忘事」，
而不是报错。因此这里做三件事来兜住它：

1. **原始历史不删** —— 压缩只影响「这次送给模型的窗口」，
   库里仍保留完整消息，``GET /threads/{id}`` 能回看；
2. **摘要可增量复用** —— 逐轮把「上一版摘要 + 新纳入的旧消息」再压一次，
   而不是每轮从零重压（那既慢又贵，还会让摘要漂移）；
3. **所有数字都上报** —— ``/threads/{id}/context`` 给出裁剪/压缩前后的 token 数。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from doudou.config.loader import Settings
from doudou.observability.logging import get_logger
from doudou.utils.text import truncate
from doudou.utils.tokens import count_messages_tokens, count_tokens

logger = get_logger(__name__)

#: 摘要消息的前缀，用来在历史里一眼认出它。
SUMMARY_PREFIX = "[历史摘要]"

#: 摘要本身的上限（它是每轮都带的，不能太长）。
SUMMARY_MAX_CHARS = 4000

#: 少于这个 token 数就不值得压缩 —— 一次摘要调用本身也要花钱。
MIN_SUMMARIZE_TOKENS = 1500

SUMMARY_INSTRUCTIONS = """\
你在压缩一段对话历史。请输出一份**陈述式**摘要，供后续对话继续使用。

要求：
- 保留：用户的目标与约束、已达成的结论、关键事实与数据、未完成的事项。
- 丢弃：寒暄、重复内容、工具的原始输出（只保留结论）。
- 用第三人称陈述，不要写「用户说」「助手说」这类对话记录。
- 不要编造原文没有的信息；不确定的写「未确认」。
- 直接输出摘要正文，不要任何前言或标题。
"""


#: 摘要器：把一批消息压成一段文本。
Summarizer = Callable[[Sequence[BaseMessage]], Awaitable[str]]


@dataclass(slots=True)
class ContextStats:
    """一次运行的上下文占用快照。排查「为什么它忘了」全靠它。"""

    messages: int = 0
    tokens: int = 0
    budget: int = 0
    dropped: int = 0
    summarized: int = 0
    summary_tokens: int = 0
    offloaded: int = 0
    compressed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "messages": self.messages,
            "tokens": self.tokens,
            "budget": self.budget,
            "dropped": self.dropped,
            "summarized": self.summarized,
            "summary_tokens": self.summary_tokens,
            "offloaded": self.offloaded,
            "compressed": self.compressed,
            "usage_ratio": round(self.tokens / self.budget, 3) if self.budget else 0.0,
        }


class HistorySummarizer:
    """用模型把一批消息压成摘要。"""

    def __init__(
        self,
        model: BaseChatModel,
        *,
        max_chars: int = SUMMARY_MAX_CHARS,
        fallback_models: Sequence[BaseChatModel] = (),
    ) -> None:
        self.model = model
        self.max_chars = max_chars
        self.fallback_models = list(fallback_models)

    async def __call__(self, messages: Sequence[BaseMessage]) -> str:
        """压缩。

        失败时**不抛异常**，而是返回一段「压缩失败」的说明 ——
        宁可让模型看到「早期内容未能压缩」的提示，也不要让整轮对话失败。
        """
        transcript = self._render(messages)
        if not transcript.strip():
            return ""
        prompt = [
            SystemMessage(content=SUMMARY_INSTRUCTIONS),
            HumanMessage(content=transcript),
        ]
        from doudou.utils.text import content_to_text

        for index, model in enumerate([self.model, *self.fallback_models]):
            try:
                response = await model.ainvoke(prompt)
            except Exception as exc:
                logger.warning(
                    "context.summarize_model_failed",
                    model_index=index,
                    error_type=type(exc).__name__,
                )
                continue
            return truncate(content_to_text(response.content).strip(), self.max_chars)
        return ""

    @staticmethod
    def _render(messages: Sequence[BaseMessage]) -> str:
        """把消息拍成可读文本。

        不做「角色名 + 内容」之外的花样：摘要的输入越简单，模型越不容易
        被格式细节带偏。
        """
        from doudou.utils.text import blocks_to_text

        lines: list[str] = []
        for message in messages:
            # 摘要模型只接收文字和图片占位，不将整段 base64 当成文本送出。
            content = message.content
            text = (
                content if isinstance(content, str) else blocks_to_text(content, placeholders=True)
            ).strip()
            calls = getattr(message, "tool_calls", None)
            if not text and calls:
                text = f"（调用工具 {', '.join(str(call['name']) for call in calls)}）"
            if not text:
                continue
            lines.append(f"{_role_label(message.type)}: {text}")
        return "\n\n".join(lines)


def _role_label(message_type: str) -> str:
    return {
        "human": "用户",
        "ai": "助手",
        "tool": "工具结果",
        "system": "系统",
    }.get(message_type, message_type)


class ResultOffloader:
    """把过大的工具结果落盘，只在上下文里留预览 + 路径。

    卸载目录默认落在**工作区内**（``<root_dir>/.offload``）。这不是随手选的：
    模型要靠 ``workspace_read`` 把内容读回来，而那个工具有路径逃逸防护——
    落在工作区外的文件它读不到，「卸载」就变成了「丢弃」。
    """

    def __init__(self, settings: Settings) -> None:
        self.config = settings.app.context
        self.workspace_root = settings.root_dir
        self.root = _resolve_offload_dir(settings, self.workspace_root)
        self.count = 0

    @property
    def enabled(self) -> bool:
        return bool(self.config.offload_large_results)

    @property
    def threshold(self) -> int:
        return max(0, self.config.offload_threshold_bytes)

    def maybe_offload(self, text: str, *, name: str, call_id: str = "") -> str:
        """超过阈值时落盘并返回替代文本；否则原样返回。"""
        if not self.enabled or self.threshold <= 0:
            return text
        size = len(text.encode("utf-8", errors="replace"))
        if size <= self.threshold:
            return text

        try:
            path = self._write(text, name=name, call_id=call_id)
        except OSError as exc:
            # 落盘失败不能让工具调用失败：退回「截断」，至少还能用。
            logger.warning("context.offload_failed", tool=name, error=str(exc))
            return truncate(text, self.threshold)

        self.count += 1
        try:
            relative = path.relative_to(self.workspace_root).as_posix()
        except ValueError:  # pragma: no cover - 见 _resolve_offload_dir 的告警
            relative = str(path)
        preview = truncate(text, min(800, max(200, self.threshold // 8)))
        logger.info(
            "context.offloaded", tool=name, bytes=size, path=str(path), chars_kept=len(preview)
        )
        return (
            f"（这次结果很大：{size} 字节，已存为文件，以下只是预览）\n\n"
            f"{preview}\n\n"
            f"完整内容在 `{relative}`，需要细节时用 workspace_read 读取。"
        )

    @property
    def offload_root(self) -> Path:
        """卸载目录（检视接口用）。"""
        return self.root

    def _write(self, text: str, *, name: str, call_id: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(char if char.isalnum() or char in "-_" else "-" for char in name)
        suffix = call_id[-8:] if call_id else f"{self.count:04d}"
        path = self.root / f"{safe_name}-{suffix}.txt"
        path.write_text(text, encoding="utf-8", newline="\n")
        return path


def _resolve_offload_dir(settings: Settings, workspace_root: Path) -> Path:
    configured = settings.app.context.offload_dir
    if not configured:
        return workspace_root / ".offload"
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.is_relative_to(workspace_root):
        # 落在工作区外 → 模型读不回来 → 卸载等于丢弃。必须吵一声。
        logger.warning(
            "context.offload_dir_outside_workspace",
            offload_dir=str(path),
            workspace=str(workspace_root),
            hint="模型无法用 workspace_read 读回工作区外的文件，卸载会变成丢弃",
        )
    return path


class ContextManager:
    """决定「这次送给模型的是哪段历史」。

    逐轮复用摘要（``_summaries``）是这里唯一有点绕的地方，理由在模块 docstring：
    从零重压既慢又贵，还会让摘要随轮次漂移。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        summarizer: Summarizer | None = None,
        offloader: ResultOffloader | None = None,
    ) -> None:
        self.settings = settings
        self.config = settings.app.context
        self.summarizer = summarizer
        self.offloader = offloader or ResultOffloader(settings)
        #: thread_id -> (已被摘要覆盖的消息数, 摘要正文)
        self._summaries: dict[str, tuple[int, str]] = {}

    # ---------------------------------------------------------------- 配置

    @property
    def budget(self) -> int:
        return max(1000, self.config.max_input_tokens)

    @property
    def summarize_at(self) -> int:
        ratio = min(max(self.config.summarize_threshold_ratio, 0.1), 0.95)
        return int(self.budget * ratio)

    @property
    def enabled(self) -> bool:
        return self.config.summarization and self.summarizer is not None

    # ---------------------------------------------------------------- 主流程

    async def prepare(
        self, thread_id: str, history: Sequence[BaseMessage]
    ) -> tuple[list[BaseMessage], ContextStats]:
        """产出本轮要用的历史，并给出统计。

        触发压缩的**唯一**条件是 token 预算（``budget * summarize_threshold_ratio``）
        """
        messages = list(history)
        stats = ContextStats(budget=self.budget)
        total = count_messages_tokens(messages)
        if total <= self.summarize_at:
            stats.messages = len(messages)
            stats.tokens = total
            return messages, stats

        compressed = await self._compress(thread_id, messages, stats)
        kept = self._select_recent(compressed, stats)
        stats.messages = len(kept)
        stats.tokens = count_messages_tokens(kept)
        logger.info(
            "context.prepared",
            thread_id=thread_id,
            **{k: v for k, v in stats.as_dict().items() if k != "usage_ratio"},
        )
        return kept, stats

    async def _compress(
        self, thread_id: str, messages: list[BaseMessage], stats: ContextStats
    ) -> list[BaseMessage]:
        """把最早的若干条压成一条摘要消息。"""
        if not self.enabled:
            return messages

        keep_recent = max(0, self.config.keep_recent_messages)
        if len(messages) <= keep_recent:
            return messages

        split = self._split_point(messages, keep_recent)
        cached_count, cached_summary = self._summaries.get(thread_id, (0, ""))

        # 缓存覆盖得比切点还多说明历史被换过（例如换了 thread），丢弃缓存。
        if cached_count > split:
            self._summaries.pop(thread_id, None)
            cached_count, cached_summary = 0, ""

        covered = 0
        summary = cached_summary
        if split > cached_count:
            # 增量：把「上一版摘要 + 新纳入的旧消息」一起再压一次。
            batch: list[BaseMessage] = []
            if cached_summary:
                batch.append(SystemMessage(content=f"{SUMMARY_PREFIX}\n{cached_summary}"))
            batch.extend(messages[cached_count:split])
            if count_messages_tokens(batch) >= MIN_SUMMARIZE_TOKENS:
                assert self.summarizer is not None  # enabled 已保证
                summary = await self.summarizer(batch)
                covered = split
            else:
                # 内容太少不值得压缩，也不清缓存（下一轮可能就够了）。
                covered = cached_count
                if not summary:
                    return messages
        else:
            covered = cached_count

        if not summary:
            return messages

        self._summaries[thread_id] = (covered, summary)
        stats.summarized = covered
        stats.compressed = True
        stats.summary_tokens = count_tokens(summary)
        stats.dropped += covered
        logger.info(
            "context.compressed",
            thread_id=thread_id,
            covered_messages=covered,
            summary_chars=len(summary),
        )
        return [*_summary_messages(summary), *messages[covered:]]

    def _select_recent(self, messages: list[BaseMessage], stats: ContextStats) -> list[BaseMessage]:
        """按 token 预算从尾部取，且不切断工具调用组。"""
        kept = trim_to_budget(messages, max_tokens=self.budget)
        stats.dropped += len(messages) - len(kept)
        return kept

    @staticmethod
    def _split_point(messages: list[BaseMessage], keep_recent: int) -> int:
        """压缩的切点：保留最后 ``keep_recent`` 条，且不切断工具调用组。

        落点是工具结果时向前回退 —— 否则会把「AI 要调用工具」留在外面、
        只把工具结果留给模型，provider 会直接拒绝这种历史。
        """
        split = max(0, len(messages) - keep_recent)
        while split > 0 and messages[split].type == "tool":
            split -= 1
        return split

    # ---------------------------------------------------------------- 统计与缓存

    def summary_for(self, thread_id: str) -> str | None:
        cached = self._summaries.get(thread_id)
        return cached[1] if cached else None

    def forget(self, thread_id: str) -> None:
        """清掉某个会话的摘要缓存（删除会话时调用）。"""
        self._summaries.pop(thread_id, None)

    def cached_threads(self) -> list[str]:
        return list(self._summaries)


def _summary_messages(summary: str) -> list[BaseMessage]:
    return [
        SystemMessage(
            content=(
                f"{SUMMARY_PREFIX} 以下是更早对话的摘要（原文未删除，可按需回看）：\n\n{summary}"
            )
        )
    ]


def render_summary(summary: str) -> str:
    """给检视接口/CLI 用的摘要渲染。"""
    return f"{SUMMARY_PREFIX}\n{summary}"


def trim_to_budget(messages: Sequence[BaseMessage], *, max_tokens: int) -> list[BaseMessage]:
    """从尾部往前取，直到 token 预算用完；且不以工具结果开头。

    纯函数，便于单独测试与复用。永远至少保留最后一条 ——
    返回空历史会让模型直接看不到用户刚说的话。
    """
    if not messages:
        return []

    kept: list[BaseMessage] = []
    used = 0
    for message in reversed(messages):
        cost = count_messages_tokens([message])
        if kept and used + cost > max_tokens:
            break
        kept.append(message)
        used += cost

    kept.reverse()
    # 起点落在工具结果上就往后顺延：不切断工具调用组。
    start = 0
    while start < len(kept) and kept[start].type == "tool":
        start += 1
    trimmed = kept[start:]
    return trimmed or kept[-1:]


def context_report(
    *,
    thread_id: str,
    messages: Sequence[BaseMessage],
    stats: ContextStats | None,
    summary: str | None,
    budget: int,
) -> dict[str, Any]:
    """给 ``GET /threads/{id}/context`` 用的报告。"""
    total = count_messages_tokens(messages)
    return {
        "thread_id": thread_id,
        "stored_messages": len(messages),
        "stored_tokens": total,
        "budget": budget,
        "last_run": stats.as_dict() if stats else None,
        "summary": {"present": bool(summary), "chars": len(summary or "")},
        "note": (
            "原始历史全部保留在库里；摘要只影响送给模型的窗口。"
            "token 数为估算值（cl100k_base 或字符启发式）。"
        ),
    }


__all__ = [
    "MIN_SUMMARIZE_TOKENS",
    "SUMMARY_MAX_CHARS",
    "SUMMARY_PREFIX",
    "ContextManager",
    "ContextStats",
    "HistorySummarizer",
    "ResultOffloader",
    "Summarizer",
    "context_report",
    "render_summary",
    "trim_to_budget",
]
