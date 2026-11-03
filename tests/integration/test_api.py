"""HTTP API 端到端：鉴权、对话、SSE、线程与**重启后可续**。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from doudou.api.app import create_app
from doudou.config.loader import Settings, load_settings
from doudou.config.schema import McpServerSpec

#: 本地演示 MCP server：真进程、真 stdio 往返，用来验证「配置 → 工具 → Agent」这条链路。
DEMO_MCP_SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp" / "demo_server.py"

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
  skills: {skills}
  max_steps: 4
filesystem:
  root_dir: "{workspace}"
storage:
  history: database
  database_url: "sqlite+aiosqlite:///{db}"
"""


def _write_config(
    tmp_path: Path,
    *,
    token: str = "",
    history: str = "database",
    skills_root: Path | None = None,
    extra_config: str = "",
) -> Settings:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    db = (tmp_path / "doudou.db").as_posix()
    skills = f'["{skills_root.as_posix()}"]' if skills_root else "[]"
    # 工作区指向 tmp_path：否则测试会去动仓库里的 data/
    text = CONFIG_TEMPLATE.format(
        db=db,
        workspace=(tmp_path / "workspace").as_posix(),
        skills=skills,
    ).replace("history: database", f"history: {history}")
    (config_dir / "config.yaml").write_text(text + extra_config, encoding="utf-8")
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
    # 默认配置下 web/http 因依赖不满足而不注册，其余分组全部可用
    assert {"now", "workspace_read", "workspace_write"} <= set(body["tools"])
    assert "web_search" not in body["tools"]
    assert body["checks"]["history"] == "database"
    # 默认配置没开任何常驻通道；通道列表为空也要出现在响应里
    assert body["channels"] == []


async def test_ready_probes_the_store(settings_for_api: Settings) -> None:
    async with api_client(settings_for_api) as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json()["history"] == "ok"


async def test_web_ui_is_served(settings_for_api: Settings) -> None:
    """内置聊天页由服务托管，且不需要鉴权（页面自己处理令牌）。"""
    async with api_client(settings_for_api) as client:
        page = await client.get("/")
        alias = await client.get("/ui")
        avatar = await client.get("/avatar.png")
        nested_avatar = await client.get("/ui/avatar.png")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "Doudou" in page.text
    assert "/chat/stream" in page.text  # 前端确实接的是流式接口
    assert "/approvals" in page.text  # 人工确认也在页面里处理
    # 工具调用折成一个可折叠的块（收起只占一行），不再逐条占行把回复顶下去
    assert "tool-fold" in page.text
    assert "toolFoldLabel" in page.text
    assert "trace-card" not in page.text
    # base_path 在服务端注入，改配置不需要改前端
    assert "__DOUDOU_BASE_PATH__" not in page.text
    assert 'const BASE_PATH = "/api/v1"' in page.text
    assert "avatar.png" in page.text
    assert alias.status_code == 200

    for response in (avatar, nested_avatar):
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content.startswith(b"\x89PNG")
    assert avatar.content.startswith(b"\x89PNG")


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

    tools = {item["name"]: item for item in response.json()["tools"]}
    assert {"now", "workspace_read", "workspace_write"} <= set(tools)
    # 写文件有副作用 → 清单里要标出来，客户端据此决定是否提示确认
    assert tools["workspace_write"]["side_effects"] is True
    assert tools["workspace_write"]["requires_approval"] is True
    assert tools["now"]["side_effects"] is False


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
    # 文字与图片共同决定请求是否为空，因此错误归属整个请求体。
    assert body["error"]["details"][0]["field"] == "body"
    assert "文字或发送图片" in body["error"]["details"][0]["issue"]


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


# --------------------------------------------------------------------------- 持久化


async def test_history_survives_app_restart(tmp_path: Path) -> None:
    """重启进程后仍能拿到历史并继续对话。"""
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


async def test_thread_context_endpoint(settings_for_api: Settings) -> None:
    """``GET /threads/{id}/context`` 必须回答「占多少、压过没、原文还在不在」。"""
    async with api_client(settings_for_api) as client:
        created = await client.post("/api/v1/chat", json={"message": "你好"})
        thread_id = created.json()["thread_id"]
        report = (await client.get(f"/api/v1/threads/{thread_id}/context")).json()

    assert report["thread_id"] == thread_id
    assert report["stored_messages"] == 2
    assert report["stored_tokens"] > 0
    assert report["budget"] > 0
    assert report["last_run"] is not None
    assert report["last_run"]["tokens"] <= report["budget"]
    assert report["summary"]["present"] is False
    assert "原始历史全部保留在库里" in report["note"]


async def test_thread_context_unknown_returns_404(settings_for_api: Settings) -> None:
    async with api_client(settings_for_api) as client:
        response = await client.get("/api/v1/threads/nope/context")

    assert response.status_code == 404


# --------------------------------------------------------------------------- 常驻通道


class FakeChannel:
    """假的常驻通道：只记录启停，不去连外部服务。"""

    name = "fake"

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def send(self, message: object) -> None:
        return None


class BrokenChannel(FakeChannel):
    """启动就失败，用来验证「单个通道挂掉不影响 HTTP 服务」。"""

    name = "broken"

    async def start(self) -> None:
        raise RuntimeError("连不上")


async def test_enabled_channel_is_started_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lifespan 要负责：装配总线 → 构造通道 → 启动 → 在 /health 里报告 → 关闭。"""
    from doudou import api

    made: list[FakeChannel] = []
    seen_broker: list[object] = []

    def fake_build(settings: object, router: object, **kwargs: object) -> list[object]:
        channel = FakeChannel()
        made.append(channel)
        seen_broker.append(kwargs.get("broker"))
        return [channel]

    monkeypatch.setattr(api.lifespan, "build_enabled_channels", fake_build)

    settings = _write_config(tmp_path)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            body = (await client.get("/api/v1/health")).json()

        assert made[0].started is True
        assert body["channels"] == ["fake"]
        assert body["checks"]["channels"] == "fake"
        # 总线必须已经起好，否则常驻通道的消息进来无处可去
        assert app.state.doudou.bus is not None
        # 确认中介必须传下去：否则飞书里没人能回答「同意 / 拒绝」
        assert seen_broker[0] is app.state.doudou.broker
        assert app.state.doudou.broker is not None

    assert made[0].stopped is True


async def test_channel_start_failure_does_not_break_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """飞书连不上不应该让整个服务起不来 —— 运维还要靠 /health 排查。"""
    from doudou import api

    # lifespan 是 `from ... import build_enabled_channels`，所以要 patch 它自己的命名空间。
    monkeypatch.setattr(
        api.lifespan,
        "build_enabled_channels",
        lambda settings, router, **kwargs: [BrokenChannel(), FakeChannel()],
    )

    settings = _write_config(tmp_path)
    async with api_client(settings) as client:
        body = (await client.get("/api/v1/health")).json()

    # 坏通道被跳过，好通道仍然起来
    assert body["channels"] == ["fake"]


# --------------------------------------------------------------------------- MCP


async def test_mcp_endpoint_without_servers(settings_for_api: Settings) -> None:
    """没配清单时接口仍然可用：``enabled`` 为真但 server 列表为空。"""
    async with api_client(settings_for_api) as client:
        response = await client.get("/api/v1/mcp")
        refreshed = await client.post("/api/v1/mcp/refresh")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "started": True,
        "servers": [],
        "note": None,
    }
    # 刷新也必须能成功（没有 server 时就是空操作）
    assert refreshed.status_code == 200
    assert refreshed.json()["note"]


async def test_health_summary_includes_mcp(settings_for_api: Settings) -> None:
    async with api_client(settings_for_api) as client:
        body = (await client.get("/api/v1/health")).json()

    assert body["mcp"] == {"enabled": True, "servers": 0, "tools": 0, "failed": []}
    assert body["checks"]["mcp"] == "none"


def _with_mcp_servers(settings: Settings, servers: list[dict]) -> Settings:
    """返回一个把 MCP server 清单内联进 ``mcp.servers`` 的 Settings 副本。

    清单现在写在 ``config.yaml`` 的 ``mcp`` 段里，所以测试直接改配置对象，
    不再往配置目录里塞 ``mcp.servers.yaml``。
    """
    mcp = settings.app.mcp.model_copy(
        update={"servers": [McpServerSpec.model_validate(item) for item in servers]}
    )
    app = settings.app.model_copy(update={"mcp": mcp})
    return settings.model_copy(update={"app": app})


async def test_mcp_tools_reach_the_agent_through_the_api(tmp_path: Path) -> None:
    """端到端：配置里的清单 → 真实 stdio server → Agent 工具 → ``GET /tools``。

    MCP 工具必须真的进到 Agent 的工具列表里，而不只是「配置解析成功」。
    """
    import sys

    settings = _with_mcp_servers(
        _write_config(tmp_path),
        [
            {
                "name": "demo",
                "transport": "stdio",
                "command": sys.executable,
                "args": [DEMO_MCP_SERVER.as_posix()],
                "read_only": True,
                "exclude_tools": ["delete_everything"],
            }
        ],
    )

    async with api_client(settings) as client:
        tools = {item["name"]: item for item in (await client.get("/api/v1/tools")).json()["tools"]}
        mcp = (await client.get("/api/v1/mcp")).json()

    # 真实 MCP 工具进入了 Agent 的工具面
    assert {"demo__echo", "demo__add"} <= set(tools)
    # exclude_tools 挡住了危险工具
    assert "demo__delete_everything" not in tools
    # read_only 声明生效：只读 server 的工具不必逐次确认
    assert tools["demo__echo"]["requires_approval"] is False
    assert tools["demo__echo"]["group"] == "mcp:demo"

    server_entry = mcp["servers"][0]
    assert server_entry["ok"] is True
    assert server_entry["name"] == "demo"
    assert "delete_everything" in server_entry["filtered_out"]
    assert server_entry["elapsed"] is not None


async def test_broken_mcp_server_degrades_but_service_works(tmp_path: Path) -> None:
    """一个 MCP server 挂掉，服务必须照常可用（``fail_fast: false``）。"""
    import sys

    settings = _with_mcp_servers(
        _write_config(tmp_path),
        [
            {
                "name": "broken",
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-c", "import sys; sys.exit(3)"],
            }
        ],
    )

    async with api_client(settings) as client:
        health = (await client.get("/api/v1/health")).json()
        mcp = (await client.get("/api/v1/mcp")).json()
        chat = await client.post("/api/v1/chat", json={"message": "你好"})

    assert health["status"] == "ok"
    assert health["mcp"]["failed"] == ["broken"]
    assert mcp["servers"][0]["ok"] is False
    assert mcp["servers"][0]["error"]
    # 服务照常能对话
    assert chat.status_code == 200


# --------------------------------------------------------------------------- Skill


SKILL_MD = """\
---
name: demo-skill
description: 演示技能。当用户要求演示技能机制时使用。
---

# 演示技能

按需加载的正文。
"""

SKILL_MD_EXTRA = SKILL_MD.replace("demo-skill", "added-skill").replace("演示技能", "新技能")


def _write_skills(root: Path) -> Path:
    """铺一个技能目录（只动文件，不碰配置）。

    配置通过 ``_write_config(skills_root=...)`` 传进去 —— 早期版本在这里改
    config.yaml，但 ``_write_config`` 每次都会从模板重写它，改动会被覆盖。
    """
    directory = root / "demo-skill"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (directory / "reference.md").write_text("附表内容", encoding="utf-8")
    return root


async def test_skills_endpoint_lists_index_without_body(tmp_path: Path) -> None:
    """列表只给索引：正文可能很大，检视时不需要。"""
    root = _write_skills(tmp_path / "skills")
    settings = _write_config(tmp_path, skills_root=root)

    async with api_client(settings) as client:
        listed = (await client.get("/api/v1/skills")).json()
        detail = (await client.get("/api/v1/skills/demo-skill")).json()
        with_body = (
            await client.get("/api/v1/skills/demo-skill", params={"include_body": "true"})
        ).json()

    assert listed["count"] == 1
    assert listed["skills"][0]["name"] == "demo-skill"
    assert listed["skills"][0]["files"] == ["reference.md"]
    assert detail["body"] is None
    assert with_body["body"] is not None
    assert "按需加载的正文" in with_body["body"]


async def test_skills_endpoint_reports_invalid_skills(tmp_path: Path) -> None:
    """写坏的 SKILL.md 要被报出来，而不是静默跳过。"""
    root = _write_skills(tmp_path / "skills")
    (root / "broken").mkdir()
    (root / "broken" / "SKILL.md").write_text("---\nname: broken\n---\n", encoding="utf-8")
    settings = _write_config(tmp_path, skills_root=root)

    async with api_client(settings) as client:
        listed = (await client.get("/api/v1/skills")).json()

    assert listed["count"] == 1
    assert len(listed["errors"]) == 1
    assert "description" in listed["errors"][0]


async def test_skill_detail_unknown_returns_404(tmp_path: Path) -> None:
    root = _write_skills(tmp_path / "skills")
    settings = _write_config(tmp_path, skills_root=root)

    async with api_client(settings) as client:
        response = await client.get("/api/v1/skills/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "skill_not_found"


async def test_skills_reload_reports_new_skills(tmp_path: Path) -> None:
    """``POST /skills/reload`` 要能发现新增的技能。"""
    root = _write_skills(tmp_path / "skills")
    settings = _write_config(tmp_path, skills_root=root)

    async with api_client(settings) as client:
        before = (await client.get("/api/v1/skills")).json()

        (root / "added-skill").mkdir()
        (root / "added-skill" / "SKILL.md").write_text(SKILL_MD_EXTRA, encoding="utf-8")

        reloaded = (await client.post("/api/v1/skills/reload")).json()
        after = (await client.get("/api/v1/skills")).json()

    assert before["count"] == 1
    assert reloaded["count"] == 2
    assert after["count"] == 2
    assert reloaded["system_prompt_chars"] > 0
    assert "added-skill" in {item["name"] for item in after["skills"]}


async def test_skills_reload_changes_agent_prompt(tmp_path: Path) -> None:
    """直接断言 Agent 手里的提示词变了 —— 这才是「生效」的证据。"""
    root = _write_skills(tmp_path / "skills")
    settings = _write_config(tmp_path, skills_root=root)

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        bundle = app.state.doudou.bundle
        assert "demo-skill" in bundle.system_prompt
        assert "added-skill" not in bundle.system_prompt

        (root / "added-skill").mkdir()
        (root / "added-skill" / "SKILL.md").write_text(SKILL_MD_EXTRA, encoding="utf-8")

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/v1/skills/reload")

        assert "added-skill" in bundle.system_prompt
        assert bundle.agent.system_prompt == bundle.system_prompt


async def test_skill_tools_are_exposed_to_the_agent(tmp_path: Path) -> None:
    """技能工具必须真的进入 Agent 的工具列表。"""
    root = _write_skills(tmp_path / "skills")
    settings = _write_config(tmp_path, skills_root=root)

    async with api_client(settings) as client:
        tools = {item["name"]: item for item in (await client.get("/api/v1/tools")).json()["tools"]}

    assert {"read_skill", "list_skill_files", "skill_read_file"} <= set(tools)
    assert tools["read_skill"]["group"] == "skills"
    assert tools["read_skill"]["side_effects"] is False


async def test_no_skill_tools_without_skills(settings_for_api: Settings) -> None:
    """没有技能时不该给模型一个永远失败的 read_skill。"""
    async with api_client(settings_for_api) as client:
        listed = (await client.get("/api/v1/skills")).json()
        names = {item["name"] for item in (await client.get("/api/v1/tools")).json()["tools"]}

    assert listed["count"] == 0
    assert "read_skill" not in names


# --------------------------------------------------------------------------- 人工确认


async def _wait_for_pending(client: httpx.AsyncClient, *, budget: float = 3.0) -> str:
    """轮询 ``GET /approvals`` 直到出现待确认请求，返回它的 id。"""
    deadline = asyncio.get_running_loop().time() + budget
    while asyncio.get_running_loop().time() < deadline:
        body = (await client.get("/api/v1/approvals")).json()
        if body["pending"]:
            return str(body["pending"][0]["approval_id"])
        await asyncio.sleep(0.01)
    raise AssertionError("等待超时：始终没有出现待确认请求")


async def test_approvals_drive_the_waiter_and_survive_restart(tmp_path: Path) -> None:
    """确认请求可被 HTTP 落决策，且**重启后仍查得到**。

    这里直接向 broker 注入一条待决请求（模型脚本里没法让它随便调工具），
    走的仍是端点 → broker → 落库的真实链路。
    """
    settings = _write_config(tmp_path)
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        broker = app.state.doudou.broker
        assert broker is not None
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            empty = (await client.get("/api/v1/approvals")).json()
            assert empty["pending"] == []
            assert empty["timeout_seconds"] > 0

            missing = await client.post("/api/v1/approvals/ap-nope", json={"approved": True})
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "approval_not_found"

            # 一个真实的等待方：写文件的工具调用正卡在这里等人点头
            waiter = asyncio.create_task(
                broker.request(
                    tool_name="workspace_write",
                    args={"path": "report.md", "content": "结论"},
                    call_id="call-1",
                    thread_id="thread-1",
                )
            )
            approval_id = await _wait_for_pending(client)

            listed = (await client.get("/api/v1/approvals")).json()
            assert listed["pending"][0]["tool_name"] == "workspace_write"
            assert listed["pending"][0]["args"]["path"] == "report.md"

            decided = await client.post(
                f"/api/v1/approvals/{approval_id}", json={"approved": True, "reason": "可以写"}
            )
            assert decided.status_code == 200
            assert decided.json()["status"] == "approved"

            outcome = await asyncio.wait_for(waiter, timeout=3)
            assert outcome.approved is True

            after = (await client.get("/api/v1/approvals")).json()
            assert after["pending"] == []
            assert after["recent"][0]["status"] == "approved"
            assert after["recent"][0]["reason"] == "可以写"

    # 「跨重启不丢」的可实现部分：审计记录还在（同一个库）
    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        transport = httpx.ASGITransport(app=restarted)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = (await client.get("/api/v1/approvals")).json()

    assert body["pending"] == []  # 那次运行随进程结束，不会自己续跑
    assert body["recent"][0]["tool_name"] == "workspace_write"
    assert body["recent"][0]["status"] == "approved"


async def test_approvals_require_token_when_configured(tmp_path: Path) -> None:
    """确认接口能改变「危险操作是否执行」，因此必须与其它写接口一样受鉴权保护。"""
    settings = _write_config(tmp_path, token="s3cret")

    async with api_client(settings, token="s3cret") as client:
        authorized = await client.get("/api/v1/approvals")

    anonymous = create_app(settings.model_copy(update={"api_token": "s3cret"}))
    async with anonymous.router.lifespan_context(anonymous):
        transport = httpx.ASGITransport(app=anonymous)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as bare:
            denied = await bare.get("/api/v1/approvals")

    assert authorized.status_code == 200
    assert denied.status_code == 401


# --------------------------------------------------------------------------- 审计 / 用量


async def test_run_is_audited_and_counted(tmp_path: Path) -> None:
    """跑一轮 → 工具调用有审计记录、token 有归属。

    用 ``scripted:demo``（离线脚本模型）而不是真模型：这条链路要能在 CI 里跑，
    它验证的是**接线**（事件 → 观察者 → 库 → 接口），不是模型能力。
    """
    settings = _write_config(tmp_path)

    async with api_client(settings) as client:
        chat = await client.post("/api/v1/chat", json={"message": "现在几点？"})
        assert chat.status_code == 200
        thread_id = chat.json()["thread_id"]

        audit = (await client.get(f"/api/v1/audit?thread_id={thread_id}")).json()
        usage = (await client.get("/api/v1/usage")).json()
        health = (await client.get("/api/v1/health")).json()

    tool_rows = [item for item in audit["entries"] if item["kind"] == "tool"]
    assert audit["backend"] == "database"
    assert tool_rows, "工具调用没有被审计"
    assert tool_rows[0]["tool_name"] == "now"
    # demo 模型会先问时间再作答，所以这条记录一定是「不需要确认」的那种
    assert tool_rows[0]["approved"] is None
    assert tool_rows[0]["status"] == "ok"
    assert tool_rows[0]["result_preview"]

    run_rows = [item for item in audit["entries"] if item["kind"] == "run"]
    assert run_rows and run_rows[0]["status"] == "ok"

    assert usage["today"]["total_tokens"] >= 0
    assert usage["by_thread"], "用量没有按会话归属"
    assert usage["by_thread"][0]["runs"] >= 1

    assert "audit" in health["checks"]["observability"]
    assert health["observability"]["audit_backend"] == "database"


async def test_audit_arguments_are_redacted(tmp_path: Path) -> None:
    """审计要能事后还原，但**不能**把密钥抄一份进库。"""
    settings = _write_config(tmp_path)

    async with api_client(settings) as client:
        # http_request 是出网工具，参数里常带 token；这里直接构造审计记录来验证打码
        store = client._transport.app.state.doudou.observability.audit_store  # type: ignore[attr-defined]
        from doudou.observability.audit import AuditEntry

        await store.append(
            AuditEntry(
                audit_id="t1:tool:probe",
                kind="tool",
                thread_id="t1",
                tool_name="http_request",
                args={"api_key": "sk-should-not-be-here", "url": "https://example.com"},
                status="ok",
            )
        )
        body = (await client.get("/api/v1/audit")).json()

    row = body["entries"][0]
    assert "sk-should-not-be-here" not in json.dumps(row, ensure_ascii=False)
    assert row["args"]["url"] == "https://example.com"


async def test_usage_reports_unknown_price_as_null(tmp_path: Path) -> None:
    """没配价格的模型 → ``cost`` 是 ``null``，不是 0。

    把未知当 0 会让人以为成本在掌控之中，那比不显示更危险。
    """
    settings = _write_config(tmp_path)

    async with api_client(settings) as client:
        await client.post("/api/v1/chat", json={"message": "你好"})
        usage = (await client.get("/api/v1/usage")).json()

    assert usage["today"]["cost"] is None
    assert usage["unknown_models"] == ["scripted:demo"]
    assert usage["budget"]["cost_known"] is False
    assert usage["budget"]["exceeded"] is False


async def test_usage_computes_cost_with_prices(tmp_path: Path) -> None:
    settings = _write_config(
        tmp_path,
        extra_config="observability:\n"
        "  cost:\n"
        "    prices:\n"
        "      scripted:demo:\n"
        "        input: 1.0\n"
        "        output: 1.0\n"
        "    daily_budget: 100\n",
    )

    async with api_client(settings) as client:
        await client.post("/api/v1/chat", json={"message": "现在几点？"})
        usage = (await client.get("/api/v1/usage")).json()

    assert usage["today"]["cost"] is not None
    assert usage["unknown_models"] == []
    assert usage["budget"]["exceeded"] is False
    assert "scripted:demo" in usage["prices_known"]


# --------------------------------------------------------------------------- 轨迹 / 统计


async def test_run_produces_trace_and_stats(tmp_path: Path) -> None:
    """跑一轮 → 落一条轨迹、统计接口有数。

    验证的是**接线**（事件 → TraceObserver → run_traces → /traces 与 /stats），
    用 ``scripted:demo`` 离线模型，因此 CI 里可跑。
    """
    settings = _write_config(tmp_path)

    async with api_client(settings) as client:
        chat = await client.post("/api/v1/chat", json={"message": "现在几点？"})
        assert chat.status_code == 200

        traces = (await client.get("/api/v1/traces")).json()
        stats = (await client.get("/api/v1/stats")).json()

        assert traces["backend"] == "database"
        assert traces["count"] >= 1
        run_id = traces["traces"][0]["run_id"]
        detail = (await client.get(f"/api/v1/traces/{run_id}")).json()

    assert detail["run_id"] == run_id
    assert detail["ok"] is True
    assert detail["steps_detail"], "轨迹没有步骤明细"
    tool_names = {tool["name"] for step in detail["steps_detail"] for tool in step["tools"]}
    assert "now" in tool_names

    assert stats["overview"]["runs"] >= 1
    assert stats["overview"]["success_rate"] == 1.0
    assert "now" in {tool["name"] for tool in stats["tools"]}
    # now 不需要确认（没问过）→ auto 至少 1，且没有「需确认」的记录
    assert stats["approvals"]["auto"] >= 1
    assert stats["approvals"]["required"] == 0


async def test_trace_detail_unknown_id_is_404(tmp_path: Path) -> None:
    settings = _write_config(tmp_path)

    async with api_client(settings) as client:
        response = await client.get("/api/v1/traces/nope")

    assert response.status_code == 404


async def test_stats_can_be_scoped_to_a_thread(tmp_path: Path) -> None:
    """``/stats?thread_id=`` 只统计那一条会话；三份数据都按它过滤。"""
    settings = _write_config(tmp_path)

    async with api_client(settings) as client:
        thread_a = (await client.post("/api/v1/chat", json={"message": "现在几点？"})).json()[
            "thread_id"
        ]
        thread_b = (await client.post("/api/v1/chat", json={"message": "现在几点？"})).json()[
            "thread_id"
        ]
        assert thread_a != thread_b

        all_stats = (await client.get("/api/v1/stats")).json()
        scope_a = (await client.get(f"/api/v1/stats?thread_id={thread_a}")).json()
        traces_a = (await client.get(f"/api/v1/traces?thread_id={thread_a}")).json()

    assert all_stats["thread_id"] is None
    assert all_stats["overview"]["runs"] >= 2

    assert scope_a["thread_id"] == thread_a
    assert scope_a["overview"]["runs"] == 1
    assert "now" in {tool["name"] for tool in scope_a["tools"]}
    assert scope_a["by_channel"]  # 渠道分布也是该会话的

    assert traces_a["count"] == 1
    assert {item["thread_id"] for item in traces_a["traces"]} == {thread_a}


async def test_deleting_thread_purges_its_statistics(tmp_path: Path) -> None:
    """删会话要连它的审计 / 用量 / 轨迹一起删干净，不留孤儿行。"""
    settings = _write_config(tmp_path)

    async with api_client(settings) as client:
        thread_id = (await client.post("/api/v1/chat", json={"message": "现在几点？"})).json()[
            "thread_id"
        ]
        # 先确认数据确实存在
        assert (await client.get(f"/api/v1/traces?thread_id={thread_id}")).json()["count"] == 1
        assert (await client.get(f"/api/v1/audit?thread_id={thread_id}")).json()["count"] >= 1
        usage_before = (await client.get("/api/v1/usage")).json()
        assert any(row["thread_id"] == thread_id for row in usage_before["by_thread"])

        response = await client.delete(f"/api/v1/threads/{thread_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["deleted"] is True
        assert body["purged"]["run_traces"] == 1
        assert body["purged"]["audit_events"] >= 1
        assert body["purged"]["usage_daily"] == 1

        # 删完之后都查不到
        assert (await client.get(f"/api/v1/traces?thread_id={thread_id}")).json()["count"] == 0
        assert (await client.get(f"/api/v1/audit?thread_id={thread_id}")).json()["count"] == 0
        scoped = (await client.get(f"/api/v1/stats?thread_id={thread_id}")).json()
        assert scoped["overview"]["runs"] == 0
        usage_after = (await client.get("/api/v1/usage")).json()
        assert not any(row["thread_id"] == thread_id for row in usage_after["by_thread"])

        # 会话本身也没了
        assert (await client.get(f"/api/v1/threads/{thread_id}")).status_code == 404


async def test_trace_list_supports_paging(tmp_path: Path) -> None:
    """运行列表支持 offset 翻页，并给出 total（用于「第 N / 共 M 页」）。"""
    settings = _write_config(tmp_path)

    async with api_client(settings) as client:
        for _ in range(3):
            response = await client.post("/api/v1/chat", json={"message": "现在几点？"})
            assert response.status_code == 200

        first = (await client.get("/api/v1/traces", params={"limit": 2, "offset": 0})).json()
        assert first["total"] == 3
        assert first["count"] == 2
        assert first["has_more"] is True

        second = (await client.get("/api/v1/traces", params={"limit": 2, "offset": 2})).json()
        assert second["total"] == 3
        assert second["count"] == 1
        assert second["has_more"] is False

        ids = {item["run_id"] for item in first["traces"]} | {
            item["run_id"] for item in second["traces"]
        }
        assert len(ids) == 3  # 两页合起来正好 3 条，无重复

        # 按会话筛选后总数也收敛（分页与筛选共用同一套条件）
        thread_id = first["traces"][0]["thread_id"]
        scoped = (await client.get("/api/v1/traces", params={"thread_id": thread_id})).json()
        assert scoped["total"] == 1


async def test_stats_page_is_served(tmp_path: Path) -> None:
    settings = _write_config(tmp_path)

    async with api_client(settings) as client:
        page = await client.get("/stats")

    assert page.status_code == 200
    assert "统计" in page.text
    # base_path 已注入，占位符不该留在页面里
    assert "__DOUDOU_BASE_PATH__" not in page.text
