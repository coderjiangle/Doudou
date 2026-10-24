"""Interactive card builders for streaming and approval flows.

卡片就是一段 JSON。这里只做「把运行状态渲染成卡片」这一件事，
调用方（:mod:`doudou.channels.feishu.channel`）负责节流与发送。

关于流式：飞书有两条路 ——

* **整卡 patch**（本项目采用）：每次把整张卡片的 JSON ``PATCH`` 上去；
* cardkit 增量更新（``cardkit.v1.card_element.content``）：只推变化的元素。

选前者是因为它的 API 面小、行为可预测，配合节流已经足够「看起来在流式输出」。
cardkit 增量作为后续优化，见 docs/channels.md。
"""

from __future__ import annotations

from typing import Any

from doudou.utils.text import truncate

#: 卡片正文单条上限（飞书卡片有长度限制，超了会整条发送失败）。
MAX_CARD_CHARS = 3000
#: 工具结果在卡片里的展示上限。
MAX_TOOL_CHARS = 200

#: 需要转义成行内代码的字符，避免 Markdown 被模型输出破坏排版。
_PENDING_TEXT = "正在思考…"


def _markdown(content: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": content or " "}


def _note(content: str) -> dict[str, Any]:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": content}]}


def _hr() -> dict[str, Any]:
    return {"tag": "hr"}


def thinking_card(*, bot_name: str, question: str) -> dict[str, Any]:
    """收到消息后立刻发出去的「占位卡片」。

    先发一张卡再不断更新，比等模型跑完再回一句体验好得多 ——
    用户马上知道「它收到了」。
    """
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"{bot_name} 正在处理"},
        },
        "elements": [
            _markdown(f"**{truncate(question, 200)}**"),
            _hr(),
            _markdown(_PENDING_TEXT),
        ],
    }


def answer_card(
    *,
    bot_name: str,
    question: str,
    answer: str,
    tool_notes: list[str] | None = None,
    footer: str | None = None,
    failed: bool = False,
) -> dict[str, Any]:
    """最终卡片：问题 + 工具调用痕迹 + 答复。

    ``tool_notes`` 让用户看到「它查了什么」，这是检索类回答建立信任的关键 ——
    只给答案不给过程，用户无法判断可信度。
    """
    elements: list[dict[str, Any]] = [_markdown(f"**{truncate(question, 200)}**"), _hr()]

    if tool_notes:
        elements.append(_note("\n".join(tool_notes[:6])))

    body = answer.strip() or "（模型没有给出内容）"
    if len(body) > MAX_CARD_CHARS:
        body = f"{truncate(body, MAX_CARD_CHARS)}\n\n_内容过长已截断_"
    elements.append(_markdown(body))

    if footer:
        elements.append(_note(footer))

    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "red" if failed else "green",
            "title": {"tag": "plain_text", "content": f"{bot_name} 的回复"},
        },
        "elements": elements,
    }


def error_card(*, bot_name: str, question: str, message: str) -> dict[str, Any]:
    """运行失败时的卡片。明确说失败，不要假装成功。"""
    return answer_card(
        bot_name=bot_name,
        question=question,
        answer=message,
        footer="可稍后重试；若持续失败请查看服务端日志。",
        failed=True,
    )


def tool_note(name: str, *, ok: bool, result: str = "") -> str:
    """把一次工具调用渲染成一行痕迹。"""
    marker = "✓" if ok else "✗"
    body = " ".join(result.split())
    if body:
        body = f" — {truncate(body, MAX_TOOL_CHARS)}"
    return f"{marker} {name}{body}"


def tool_running_note(name: str, args: dict[str, Any] | None = None) -> str:
    """工具开始执行时的痕迹。"""
    rendered = ""
    if args:
        pairs = ", ".join(f"{key}={value}" for key, value in list(args.items())[:3])
        rendered = f"（{truncate(pairs, 80)}）"
    return f"… 正在调用 {name}{rendered}"


# --------------------------------------------------------------------------- 人工确认


def _approval_args_text(args: dict[str, Any]) -> str:
    """把工具参数渲染成卡片里的可读文本。

    只展示、不解释：用户要判断「这个操作是不是我想要的」，
    因此保留原始的键值对，而不是替模型美化。
    """
    if not args:
        return "（无参数）"
    lines = [
        f"- `{key}` = {truncate(str(value).replace(chr(10), ' '), MAX_TOOL_CHARS)}"
        for key, value in list(args.items())[:6]
    ]
    return "\n".join(lines)


def approval_card(
    *,
    bot_name: str,
    approval_id: str,
    tool_name: str,
    args: dict[str, Any],
    question: str = "",
    expires_in: float | None = None,
) -> dict[str, Any]:
    """需要人工确认的卡片，带「同意 / 拒绝」按钮。

    为什么按钮里带的是 ``approval_id`` 而不是工具名：点头的人只能确认**这一次**
    具体的调用。把「同意 write_file」当成一次性授权会让后续每一次写文件都静默通过。
    """
    elements: list[dict[str, Any]] = []
    if question:
        elements.append(_markdown(f"**{truncate(question, 120)}**"))
    elements.append(_markdown(f"它想调用 `{tool_name}`："))
    elements.append(_markdown(_approval_args_text(args)))
    if expires_in is not None:
        elements.append(_note(f"{expires_in:.0f} 秒内无人处理则按拒绝处理。"))
    elements.append(_hr())
    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "同意执行"},
                    "type": "primary",
                    "value": {"action": "approve", "approval_id": approval_id},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "拒绝"},
                    "type": "danger",
                    "value": {"action": "reject", "approval_id": approval_id},
                },
            ],
        }
    )
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"{bot_name} 需要你确认"},
        },
        "elements": elements,
    }


def approval_decided_card(
    *,
    bot_name: str,
    tool_name: str,
    approved: bool,
    reason: str = "",
) -> dict[str, Any]:
    """决策后的卡片：把按钮换掉，避免被重复点击。

    飞书会用这张卡替换原卡，因此它必须自洽 —— 用户可能过几天才回看。
    """
    verdict = "已同意执行" if approved else "已拒绝执行"
    elements: list[dict[str, Any]] = [_markdown(f"`{tool_name}` — **{verdict}**")]
    if reason:
        elements.append(_note(reason))
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "green" if approved else "grey",
            "title": {"tag": "plain_text", "content": f"{bot_name} 的确认结果"},
        },
        "elements": elements,
    }


__all__ = [
    "MAX_CARD_CHARS",
    "MAX_TOOL_CHARS",
    "answer_card",
    "approval_card",
    "approval_decided_card",
    "error_card",
    "thinking_card",
    "tool_note",
    "tool_running_note",
]
