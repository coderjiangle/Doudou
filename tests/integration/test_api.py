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
tools:
  notes:
    directory: "{notes}"
"""


def _write_config(
    tmp_path: Path,
    *,
    token: str = "",
    history: str = "database",
    skills_root: Path | None = None,
) -> Settings:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    db = (tmp_path / "doudou.db").as_posix()
    skills = f'["{skills_root.as_posix()}"]' if skills_root else "[]"
    # 工作区与笔记目录都指向 tmp_path：否则测试会去动仓库里的 data/
    text = CONFIG_TEMPLATE.format(
        db=db,
        workspace=(tmp_path / "workspace").as_posix(),
        notes=(tmp_path / "notes").as_posix(),
        skills=skills,
    ).replace("history: database", f"history: {history}")
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
    # 默认配置下 web/http 因依赖不满足而不注册，其余分组全部可用
    assert {"now", "days_between", "workspace_read", "note_save"} <= set(body["tools"])
    assert "web_search" not in body["tools"]
    assert body["checks"]["history"] == "database"
    # 默认配置没开任何常驻通道；通道列表为空也要出现在响应里
    assert body["channels"] == []


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

    tools = {item["name"]: item for item in response.json()["tools"]}
    assert {"now", "days_between", "workspace_read", "note_save"} <= set(tools)
    # 写笔记有副作用 → 清单里要标出来，客户端据此决定是否提示确认
    assert tools["note_save"]["side_effects"] is True
    assert tools["note_save"]["requires_approval"] is True
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


def _write_mcp_manifest(config_dir: Path) -> None:
    """在配置目录里放一份指向本地演示 server 的 MCP 清单。"""
    import sys

    server = Path(__file__).resolve().parents[1] / "fixtures" / "mcp" / "demo_server.py"
    (config_dir / "mcp.servers.yaml").write_text(
        "version: 1\n"
        "servers:\n"
        "  - name: demo\n"
        "    transport: stdio\n"
        f"    command: {sys.executable}\n"
        f"    args: [{server.as_posix()!r}]\n"
        "    read_only: true\n"
        "    exclude_tools: [delete_everything]\n",
        encoding="utf-8",
    )


async def test_mcp_tools_reach_the_agent_through_the_api(tmp_path: Path) -> None:
    """端到端：清单 → 真实 stdio server → Agent 工具 → ``GET /tools``。

    这是 M5 验收标准在 HTTP 层的版本：MCP 工具必须真的进到 Agent 的工具列表里，
    而不只是「配置解析成功」。
    """
    settings = _write_config(tmp_path)
    _write_mcp_manifest(settings.config_dir)

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

    server = mcp["servers"][0]
    assert server["ok"] is True
    assert server["name"] == "demo"
    assert "delete_everything" in server["filtered_out"]
    assert server["elapsed"] is not None


async def test_broken_mcp_server_degrades_but_service_works(tmp_path: Path) -> None:
    """一个 MCP server 挂掉，服务必须照常可用（``fail_fast: false``）。"""
    import sys

    settings = _write_config(tmp_path)
    (settings.config_dir / "mcp.servers.yaml").write_text(
        "version: 1\n"
        "servers:\n"
        "  - name: broken\n"
        "    transport: stdio\n"
        f"    command: {sys.executable}\n"
        "    args: ['-c', 'import sys; sys.exit(3)']\n",
        encoding="utf-8",
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
    """M8 验收：确认请求可被 HTTP 落决策，且**重启后仍查得到**。

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
