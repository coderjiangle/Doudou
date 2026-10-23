"""lark-oapi client wrapper: message send, card update.

把 lark SDK 的细节（请求对象、错误码、receive_id_type）收在一层里，好处有两个：

* 通道逻辑只面对 :class:`FeishuClient` 的四个方法，读起来是业务语义；
* 测试可以注入一个假客户端，**完全离线**地验证卡片发送/更新（见 tests）。

错误策略：lark SDK 不抛异常，而是把错误码放在响应里，因此每个方法都要显式检查
``response.success()``。漏检会让「发送失败」表现成「什么都没发生」，极难排查。
"""

from __future__ import annotations

import json
from typing import Any

from doudou.channels.feishu.schemas import CardActionValue
from doudou.config.schema import FeishuChannelConfig
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

#: 以群维度发消息（我们的会话标识就是 chat_id）。
RECEIVE_ID_TYPE = "chat_id"

MSG_TYPE_TEXT = "text"
MSG_TYPE_INTERACTIVE = "interactive"


class FeishuError(RuntimeError):
    """调用飞书接口失败。"""


class FeishuClient:
    """飞书消息接口的窄封装。"""

    def __init__(self, config: FeishuChannelConfig, *, client: Any | None = None) -> None:
        self.config = config
        self._client = client if client is not None else self._build_client()

    def _build_client(self) -> Any:
        """构造真实的 lark 客户端。只在需要时才 import，便于离线测试。"""
        import lark_oapi as lark

        return (
            lark.Client.builder()
            .app_id(self.config.app_id or "")
            .app_secret(self.config.app_secret or "")
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

    # ---------------------------------------------------------------- 发消息

    def send_card(self, chat_id: str, card: dict[str, Any]) -> str | None:
        """发送一张交互卡片，返回 ``message_id``（用于后续 patch）。"""
        return self._send(chat_id, MSG_TYPE_INTERACTIVE, card)

    def send_text(self, chat_id: str, text: str) -> str | None:
        """发送纯文本消息。"""
        return self._send(chat_id, MSG_TYPE_TEXT, {"text": text})

    def _send(self, chat_id: str, msg_type: str, payload: dict[str, Any]) -> str | None:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type(msg_type)
            .content(json.dumps(payload, ensure_ascii=False))
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(RECEIVE_ID_TYPE)
            .request_body(body)
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            logger.error(
                "feishu.send_failed",
                chat_id=chat_id,
                msg_type=msg_type,
                code=response.code,
                msg=response.msg,
            )
            raise FeishuError(f"发送消息失败（code={response.code} msg={response.msg}）")

        message_id = getattr(getattr(response, "data", None), "message_id", None)
        logger.debug("feishu.sent", chat_id=chat_id, msg_type=msg_type, message_id=message_id)
        return str(message_id) if message_id else None

    # ---------------------------------------------------------------- 更新卡片

    def patch_card(self, message_id: str, card: dict[str, Any]) -> bool:
        """整卡更新。返回是否成功。

        更新失败不算致命：模型可能已经算完了，只是卡片没刷新。
        因此这里只告警并返回 ``False``，由调用方决定是否补发一条文本。
        """
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        body = (
            PatchMessageRequestBody.builder().content(json.dumps(card, ensure_ascii=False)).build()
        )
        request = PatchMessageRequest.builder().message_id(message_id).request_body(body).build()
        response = self._client.im.v1.message.patch(request)
        if not response.success():
            logger.warning(
                "feishu.patch_failed",
                message_id=message_id,
                code=response.code,
                msg=response.msg,
            )
            return False
        return True

    # ---------------------------------------------------------------- 机器人身份

    def fetch_bot_open_id(self) -> str | None:
        """取机器人自己的 ``open_id``。

        用于群聊 @ 判定（比按显示名比对可靠）。接口不存在或调用失败时返回 ``None``：
        @ 判定会退回按 ``bot_name`` 匹配，功能不至于失效。
        """
        try:  # pragma: no cover - 需要真实凭据，测试用假客户端覆盖
            response = self._client.request(_bot_info_request())
        except Exception as exc:
            logger.warning("feishu.bot_info_failed", error=str(exc))
            return None
        if not getattr(response, "success", lambda: False)():
            logger.warning(
                "feishu.bot_info_unsuccessful",
                code=getattr(response, "code", None),
                msg=getattr(response, "msg", None),
            )
            return None
        data = getattr(response, "data", None) or {}
        open_id = data.get("open_id") if isinstance(data, dict) else None
        return str(open_id) if open_id else None


def _bot_info_request() -> Any:  # pragma: no cover - 需要真实凭据
    """构造 ``GET /open-apis/bot/v3/info`` 请求。

    lark-oapi 没有 bot info 的类型化 API（``bot.v4`` 只有 search），
    所以用它的通用 ``request`` 通道手搓一个。
    """
    import lark_oapi as lark
    from lark_oapi.core import HttpMethod

    return (
        lark.BaseRequest.builder()
        .http_method(HttpMethod.GET)
        .uri("/open-apis/bot/v3/info")
        .token_types({lark.AccessTokenType.TENANT})
        .build()
    )


def parse_action_value(raw: Any) -> CardActionValue:
    """解析卡片按钮回传的 ``value``。

    飞书的 ``value`` 既可能是 dict 也可能是 JSON 字符串，两种都要吃下。
    M8 的确认按钮依赖它，因此现在就把解析逻辑与测试放好。
    """
    if isinstance(raw, CardActionValue):
        return raw
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return CardActionValue()
    if isinstance(raw, dict):
        return CardActionValue.model_validate(raw)
    return CardActionValue()


__all__ = [
    "MSG_TYPE_INTERACTIVE",
    "MSG_TYPE_TEXT",
    "RECEIVE_ID_TYPE",
    "FeishuClient",
    "FeishuError",
    "parse_action_value",
]
