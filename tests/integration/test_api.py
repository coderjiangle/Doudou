"""HTTP API 端到端：鉴权、对话、SSE、线程与**重启后可续**。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from doudou.api.app import create_app
from doudou.config.loader import Settings, load_settings

CONFIG_TEMPLATE = """\
version: 1
runtime:
  env: local
  log_level: ERROR
  timezone: Asia/Shanghai
model:
  primary: scripted:demo
  timeout: 30
agent:
  name: ApiTest
  system_prompt_file: null
  skills: []
  max_steps: 4
storage:
  history: database
  database_url: "sqlite+aiosqlite:///{db}"
"""


def _write_config(tmp_path: Path, *, token: str = "", history: str = "database") -> Settings:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    db = (tmp_path / "doudou.db").as_posix()
    text = CONFIG_TEMPLATE.format(db=db).replace("history: database", f"history: {history}")
    (config_dir / "config.yaml").write_text(text, encoding="utf-8")
    return load_settings(config_dir=config_dir, env_file=tmp_path / "no.env", use_env=False)


@asynccontextmanager
async def api_client(settings: Settings, *, token: str = "") -> AsyncIterator[httpx.AsyncClient]:
    """启动应用（跑真实 lifespan）并返回 ASGI 客户端。

    ``httpx.ASGITransport`` 不会触发 lifespan，所以这里手动进入
    ``app.router.lifespan_context``，否则 ``app.state.doudou`` 不会就绪。
    """
    if token:
        settings = settings.model_copy(update={"api_token": token})
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers=headers
        ) as client:
            yield client


@pytest.fixture
def settings_for_api(tmp_path: Path) -> Settings:
    return _write_config(tmp_path)


# --------------------------------------------------------------------------- 健康检查


async def test_health_is_public_and_light(settings_for_api: Settings) -> None:
    async with api_client(settings_for_api) as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["engine"] == "react"
    assert body["model"] == "scripted:demo"
    assert set(body["tools"]) == {"now", "days_between"}
    assert body["checks"]["history"] == "database"


async def test_ready_probes_the_store(settings_for_api: Settings) -> None:
    async with api_client(settings_for_api) as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json()["history"] == "ok"


async def test_request_id_is_echoed(settings_for_api: Settings) -> None:
    async with api_client(settings_for_api) as client:
        response = await client.get("/api/v1/health", headers={"X-Request-ID": "trace-123"})

    assert response.headers["X-Request-ID"] == "trace-123"


# --------------------------------------------------------------------------- 鉴权


async def test_health_needs_no_token(tmp_path: Path) -> None:
    settings = _write_config(tmp_path)
    async with api_client(settings, token="s3cret") as client:
        # 客户端默认带了 token；这里显式不带。
        response = await client.get("/api/v1/health", headers={"Authorization": ""})

    assert response.status_code == 200


async def test_protected_route_rejects_missing_token(tmp_path: Path) -> None:
    settings = _write_config(tmp_path)
    async with api_client(settings, token="s3cret") as client:
        response = await client.get("/api/v1/tools", headers={"Authorization": ""})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_protected_route_rejects_wrong_token(tmp_path: Path) -> None:
    settings = _write_config(tmp_path)
    async with api_client(settings, token="s3cret") as client:
        response = await client.get("/api/v1/tools", headers={"Authorization": "Bearer nope"})

    assert response.status_code == 401


async def test_protected_route_accepts_correct_token(tmp_path: Path) -> None:
    settings = _write_config(tmp_path)
    async with api_client(settings, token="s3cret") as client:
        response = await client.get("/api/v1/tools")

    assert response.status_code == 200


async def test_no_token_configured_means_open(settings_for_api: Settings) -> None:
    async with api_client(settings_for_api) as client:
        response = await client.get("/api/v1/tools", headers={"Authorization": ""})

    assert response.status_code == 200


# --------------------------------------------------------------------------- 工具清单


async def test_tools_list(settings_for_api: Settings) -> None:
    async with api_client(settings_for_api) as client:
        response = await client.get("/api/v1/tools")

    names = [item["name"] for item in response.json()["tools"]]
    assert names == ["now", "days_between"]


# --------------------------------------------------------------------------- 对话


async def test_chat_returns_answer(settings_for_api: Settings) -> None:
    async with api_client(settings_for_api) as client:
        response = await client.post("/api/v1/chat", json={"message": "你好"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["text"] == "[offline] 收到你的消息：你好"
    assert body["thread_id"]
    assert body["tool_calls"] == []


async def test_chat_runs_tool(settings_for_api: Settings) -> None:
    """真实走一遍「模型要求调工具 → 执行 → 回填 → 收敛」。"""
    async with api_client(settings_for_api) as client:
        response = await client.post("/api/v1/chat", json={"message": "现在几点了？"})

    body = response.json()
    assert body["ok"] is True
    assert body["steps"] == 2
    assert [call["name"] for call in body["tool_calls"]] == ["now"]
    assert "工具返回结果" in body["text"]


async def test_chat_multi_turn_shares_thread(settings_for_api: Settings) -> None:
    async with api_client(settings_for_api) as client:
        first = await client.post("/api/v1/chat", json={"message": "第一问"})
        thread_id = first.json()["thread_id"]

        second = await client.post(
            "/api/v1/chat", json={"message": "第二问", "thread_id": thread_id}
        )

    assert second.json()["thread_id"] == thread_id

    async with api_client(settings_for_api) as client:
        detail = await client.get(f"/api/v1/threads/{thread_id}")

    roles = [item["role"] for item in detail.json()["messages"]]
    assert roles == ["human", "ai", "human", "ai"]


async def test_chat_without_ids_creates_new_thread(settings_for_api: Settings) -> None:
    """都不传会话标识时，每次请求自成一个会话（避免不同客户端串到一个上下文）。"""
    async with api_client(settings_for_api) as client:
        first = await client.post("/api/v1/chat", json={"message": "甲"})
        second = await client.post("/api/v1/chat", json={"message": "乙"})

    assert first.json()["thread_id"] != second.json()["thread_id"]


async def test_chat_rejects_empty_message(settings_for_api: Settings) -> None:
    async with api_client(settings_for_api) as client:
        response = await client.post("/api/v1/chat", json={"message": ""})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["details"][0]["field"] == "body.message"


async def test_conversation_id_binding_is_stable(settings_for_api: Settings) -> None:
    async with api_client(settings_for_api) as client:
        first = await client.post(
            "/api/v1/chat", json={"message": "甲", "conversation_id": "cli-local"}
        )
        second = await client.post(
            "/api/v1/chat", json={"message": "乙", "conversation_id": "cli-local"}
        )

    assert first.json()["thread_id"] == second.json()["thread_id"]


# --------------------------------------------------------------------------- SSE


async def test_chat_stream_emits_expected_events(settings_for_api: Settings) -> None:
    events: list[tuple[str, dict]] = []
    async with api_client(settings_for_api) as client:
        async with client.stream(
            "POST", "/api/v1/chat/stream", json={"message": "现在几点了？"}
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            name: str | None = None
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and name:
                    events.append((name, json.loads(line.split(":", 1)[1].strip())))
                    name = None

    names = [name for name, _ in events]
    assert names[0] == "start"
    assert names[-1] == "done"
    assert "tool_call" in names and "tool_result" in names
    assert "delta" in names

    start = dict(events)[  # type: ignore[arg-type]
        "start"
    ]
    assert start["thread_id"]

    done = next(payload for name, payload in events if name == "done")
    assert "工具返回结果" in done["text"]
    assert done["steps"] == 2

    deltas = "".join(payload["text"] for name, payload in events if name == "delta")
    assert deltas == done["text"]


async def test_map_event_translates_run_end(settings_for_api: Settings) -> None:
    """映射逻辑是纯函数，可以脱离 SSE 连接直接断言。"""
    from doudou.agent.events import RunEvent
    from doudou.api.routes.chat import MAX_STREAM_RESULT_CHARS, map_event

    assert map_event(RunEvent.run_start(), "t") is None
    assert map_event(RunEvent.step_start(1), "t") is None

    name, body = map_event(RunEvent.text_delta("你好", step=1), "t")
    assert (name, body) == ("delta", {"text": "你好"})

    name, body = map_event(RunEvent.run_end("全文", step=2, usage={"output_tokens": 7}), "t")
    assert name == "done"
    assert body["thread_id"] == "t"
    assert body["usage"] == {"output_tokens": 7}

    # 超长工具结果必须被截断，否则一个 10 万字的返回值就能拖垮 SSE 连接
    name, body = map_event(
        RunEvent.tool_result(
            step=1, name="big", call_id="c1", result="x" * (MAX_STREAM_RESULT_CHARS + 10)
        ),
        "t",
    )
    assert name == "tool_result"
    assert body["truncated"] is True
    assert len(body["result"]) < MAX_STREAM_RESULT_CHARS + 100


# --------------------------------------------------------------------------- 线程


async def test_threads_list(settings_for_api: Settings) -> None:
    async with api_client(settings_for_api) as client:
        await client.post("/api/v1/chat", json={"message": "你好"})
        response = await client.get("/api/v1/threads")

    body = response.json()
    assert len(body["threads"]) == 1
    assert body["threads"][0]["message_count"] == 2
    assert body["threads"][0]["channel"] == "api"


async def test_thread_detail_unknown_returns_404(settings_for_api: Settings) -> None:
    async with api_client(settings_for_api) as client:
        response = await client.get("/api/v1/threads/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "thread_not_found"


async def test_thread_delete(settings_for_api: Settings) -> None:
    async with api_client(settings_for_api) as client:
        created = await client.post("/api/v1/chat", json={"message": "你好"})
        thread_id = created.json()["thread_id"]

        deleted = await client.delete(f"/api/v1/threads/{thread_id}")
        missing = await client.get(f"/api/v1/threads/{thread_id}")

    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert missing.status_code == 404


# --------------------------------------------------------------------------- 持久化（M2 验收标准）


async def test_history_survives_app_restart(tmp_path: Path) -> None:
    """重启进程后仍能拿到历史并继续对话 —— 这是 M2 的验收标准。"""
    settings = _write_config(tmp_path)

    async with api_client(settings) as client:
        first = await client.post("/api/v1/chat", json={"message": "第一问"})
        thread_id = first.json()["thread_id"]

    # 换一个全新的 app 实例（等价于进程重启），复用同一个 SQLite 文件。
    async with api_client(_write_config(tmp_path)) as client:
        history = await client.get(f"/api/v1/threads/{thread_id}")
        follow_up = await client.post(
            "/api/v1/chat", json={"message": "第二问", "thread_id": thread_id}
        )
        after = await client.get(f"/api/v1/threads/{thread_id}")

    assert history.status_code == 200
    assert [item["content"] for item in history.json()["messages"]] == [
        "第一问",
        "[offline] 收到你的消息：第一问",
    ]

    assert follow_up.json()["thread_id"] == thread_id
    roles = [item["role"] for item in after.json()["messages"]]
    assert roles == ["human", "ai", "human", "ai"]


async def test_conversation_binding_survives_restart(tmp_path: Path) -> None:
    """会话归属（渠道会话 → thread）也要持久，否则重启后「新会话」会另起一个。"""
    settings = _write_config(tmp_path)

    async with api_client(settings) as client:
        first = await client.post(
            "/api/v1/chat", json={"message": "甲", "conversation_id": "feishu-群-1"}
        )
        thread_id = first.json()["thread_id"]

    async with api_client(_write_config(tmp_path)) as client:
        second = await client.post(
            "/api/v1/chat", json={"message": "乙", "conversation_id": "feishu-群-1"}
        )

    assert second.json()["thread_id"] == thread_id


async def test_memory_backend_does_not_persist(tmp_path: Path) -> None:
    """``storage.history: memory`` 时不落库 —— 用于本地/CI 的轻量模式。"""
    settings = _write_config(tmp_path, history="memory")

    async with api_client(settings) as client:
        created = await client.post("/api/v1/chat", json={"message": "你好"})
        thread_id = created.json()["thread_id"]
        assert (await client.get(f"/api/v1/threads/{thread_id}")).status_code == 200

    async with api_client(settings) as client:
        assert (await client.get(f"/api/v1/threads/{thread_id}")).status_code == 404
