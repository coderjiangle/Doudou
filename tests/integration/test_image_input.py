"""图片经 HTTP、ReAct、存储和历史回放的离线端到端验证。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from doudou.agent.testing import ScriptedChatModel
from tests.integration.test_api import _write_config, api_client
from tests.unit.test_image_input import PNG_BASE64, PNG_URL


@pytest.mark.parametrize("stream", [False, True])
async def test_image_input_reaches_model_and_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    seen: list[list[BaseMessage]] = []

    def answer(messages: list[BaseMessage]) -> AIMessage:
        seen.append(list(messages))
        return AIMessage(content="图片已收到")

    monkeypatch.setattr(
        "doudou.agent.factory.build_model",
        lambda *_args, **_kwargs: ScriptedChatModel(steps=[answer, answer]),
    )
    settings = _write_config(tmp_path)
    endpoint = "/api/v1/chat/stream" if stream else "/api/v1/chat"
    payload = {"conversation_id": "with-image", "message": "", "images": [{"data_url": PNG_URL}]}
    async with api_client(settings) as client:
        response = await client.post(endpoint, json=payload)
        assert response.status_code == 200
        assert "图片已收到" in response.text
        threads = (await client.get("/api/v1/threads")).json()["threads"]
        thread_id = threads[0]["thread_id"]
        detail = (await client.get(f"/api/v1/threads/{thread_id}")).json()
        assert detail["messages"][0]["images"] == [PNG_URL]
        assert detail["messages"][0]["content"] == ""
    assert seen[0][-1].content == [{"type": "image_url", "image_url": {"url": PNG_URL}}]

    async with api_client(settings) as restarted:
        followup = await restarted.post(
            "/api/v1/chat",
            json={
                "thread_id": thread_id,
                "message": "刚才图片还在吗？",
            },
        )
        assert followup.status_code == 200
        assert any(item.content == seen[0][-1].content for item in seen[-1])
        assert len(seen) == 2


@pytest.mark.parametrize(
    "images",
    [
        [{"data_url": "https://example.com/image.png"}],
        [{"data_url": "data:image/png;base64,invalid"}],
        [{"data_url": PNG_URL}] * 5,
    ],
)
async def test_invalid_image_requests_do_not_start_a_run(tmp_path: Path, images: list[Any]) -> None:
    async with api_client(_write_config(tmp_path)) as client:
        response = await client.post("/api/v1/chat", json={"images": images})
        assert response.status_code == 422
        assert PNG_BASE64 not in response.text
        assert (await client.get("/api/v1/threads")).json()["threads"] == []
