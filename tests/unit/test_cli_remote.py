"""RemoteAgentRunner 与 EphemeralServeProcess 离线单元测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from doudou.agent.react import ToolApprovalRequest
from doudou.api.app import create_app
from doudou.cli.remote import EphemeralServeProcess, RemoteAgentRunner, RemoteServerError
from doudou.config.loader import Settings
from doudou.gateway.schema import RunRequest


@asynccontextmanager
async def _asgi_remote(
    settings: Settings,
    *,
    approval_hook: Any = None,
    new_session: bool = False,
):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            trust_env=False,
        ) as client:
            runner = RemoteAgentRunner(
                settings,
                base_url=f"http://testserver{settings.app.server.base_path.rstrip('/')}",
                approval_hook=approval_hook,
                new_session=new_session,
                client=client,
            )
            await runner.connect()
            yield runner


def _make_settings(
    settings_factory: Callable[..., Settings],
    tmp_path: Path,
    model: str,
    *,
    interrupt_now: bool = False,
) -> Settings:
    db_path = (tmp_path / "remote.db").as_posix()
    ws_dir = (tmp_path / "ws").as_posix()
    (tmp_path / "ws").mkdir(exist_ok=True)
    interrupt_block = "  interrupt_on:\n    now: true\n" if interrupt_now else ""
    return settings_factory(
        config_yaml=f"""\
version: 1
runtime:
  log_level: ERROR
agent:
  name: RemoteTest
  system_prompt_file: null
  skills: []
  approval_timeout_seconds: 10
{interrupt_block}filesystem:
  root_dir: {ws_dir}
storage:
  database_url: sqlite+aiosqlite:///{db_path}
  history: database
mcp:
  enabled: false
scheduler:
  enabled: false
""",
        env={"DOUDOU_MODEL_PRIMARY": model},
    )


async def test_remote_runner_streams_tools_threads_and_new(
    settings_factory: Callable[..., Settings], tmp_path: Path
) -> None:
    settings = _make_settings(settings_factory, tmp_path, "scripted:demo")

    async with _asgi_remote(settings, new_session=True) as runner:
        assert runner.model_label == "scripted:demo"
        assert runner.tool_count >= 1
        infos = await runner.list_tool_infos()
        assert any(info.name == "now" for info in infos)

        req1 = RunRequest(text="现在几点了？", conversation_id="local", channel="cli")
        events1 = [ev async for ev in runner.astream(req1)]
        types1 = [ev.type for ev in events1]
        assert types1[0] == "run_start"
        assert "tool_call" in types1
        assert "tool_result" in types1
        assert types1[-1] == "run_end"

        tid1 = await runner.resolve_thread(req1)
        assert tid1
        hist1 = await runner.history(tid1)
        assert len(hist1) >= 2
        follow_up = RunRequest(text="你好", conversation_id="local", channel="cli")
        assert (await runner.ainvoke(follow_up)).events[-1].thread_id == tid1

        await runner.start_new_thread("local", channel="cli")
        assert await runner.resolve_thread(req1) == ""
        events2 = [ev async for ev in runner.astream(req1)]
        tid2 = events2[-1].thread_id
        assert tid2 and tid2 != tid1

        threads = await runner.list_threads(limit=10)
        thread_ids = [item.thread_id for item in threads]
        assert tid1 in thread_ids
        assert tid2 in thread_ids

        # 显式传入 thread_id 时即使处于 new_session=True 也应续接目标历史会话
        resumed = await runner.ainvoke(
            RunRequest(text="回到旧会话", conversation_id="local", channel="cli", thread_id=tid1)
        )
        assert resumed.events[-1].thread_id == tid1
        assert any("回到旧会话" in str(m.content) for m in await runner.history(tid1))


@asynccontextmanager
async def _live_remote(
    settings: Settings,
    *,
    approval_hook: Any = None,
    new_session: bool = False,
):
    import socket
    from contextlib import suppress

    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    app = create_app(settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        else:
            raise RuntimeError("uvicorn test server did not start")
        base_url = f"http://127.0.0.1:{port}{settings.app.server.base_path.rstrip('/')}"
        async with httpx.AsyncClient(trust_env=False) as client:
            runner = RemoteAgentRunner(
                settings,
                base_url=base_url,
                approval_hook=approval_hook,
                new_session=new_session,
                client=client,
            )
            await runner.connect()
            yield runner
    finally:
        server.should_exit = True
        await task
        with suppress(OSError):
            sock.close()


async def test_remote_runner_handles_approval_required(
    settings_factory: Callable[..., Settings], tmp_path: Path
) -> None:
    settings = _make_settings(settings_factory, tmp_path, "scripted:demo", interrupt_now=True)
    asked: list[ToolApprovalRequest] = []

    async def approve(req: ToolApprovalRequest) -> bool:
        asked.append(req)
        return True

    async with _live_remote(settings, approval_hook=approve) as runner:
        result = await runner.ainvoke(RunRequest(text="现在几点了？", conversation_id="ap"))
        assert result.ok
        assert len(asked) == 1
        assert asked[0].tool_name == "now"
        assert any(ev.type == "tool_result" and ev.tool_ok for ev in result.events)

    # 没有交互确认回调时应拒绝执行（不让服务端傻等超时）
    deny_dir = tmp_path / "deny"
    deny_dir.mkdir()
    deny_settings = _make_settings(settings_factory, deny_dir, "scripted:demo", interrupt_now=True)
    async with _live_remote(deny_settings, approval_hook=None) as runner_deny:
        result_deny = await runner_deny.ainvoke(
            RunRequest(text="现在几点了？", conversation_id="ap2")
        )
        assert result_deny.ok
        assert "拒绝" in result_deny.text or any(
            ev.type == "tool_result" and not ev.tool_ok for ev in result_deny.events
        )


async def test_ephemeral_serve_attaches_existing_without_owning(
    settings_factory: Callable[..., Settings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _make_settings(settings_factory, tmp_path, "scripted:demo")

    async def fake_probe(base_url: str, *, api_token: str | None, timeout_seconds: float) -> bool:
        return True

    monkeypatch.setattr("doudou.cli.remote._probe_health", fake_probe)

    proc = await EphemeralServeProcess.attach_or_spawn(settings)
    assert not proc.owned
    assert proc.base_url.endswith(settings.app.server.base_path)
    await proc.aclose()


async def test_ephemeral_serve_raises_when_explicit_server_unreachable(
    settings_factory: Callable[..., Settings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _make_settings(settings_factory, tmp_path, "scripted:demo")

    async def fake_probe(base_url: str, *, api_token: str | None, timeout_seconds: float) -> bool:
        return False

    monkeypatch.setattr("doudou.cli.remote._probe_health", fake_probe)

    with pytest.raises(RemoteServerError, match="无法连接到指定的服务端"):
        await EphemeralServeProcess.attach_or_spawn(settings, server_url="http://127.0.0.1:9999")


async def test_remote_runner_raises_on_http_error(
    settings_factory: Callable[..., Settings], tmp_path: Path
) -> None:
    settings = _make_settings(settings_factory, tmp_path, "scripted:demo")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"model": "demo", "tools": ["now"]})
        return httpx.Response(500, text=json.dumps({"detail": "boom"}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        runner = RemoteAgentRunner(settings, base_url="http://mock/api/v1", client=client)
        await runner.connect()
        with pytest.raises(RemoteServerError, match="HTTP 500"):
            _ = [ev async for ev in runner.astream(RunRequest(text="hi", conversation_id="c"))]
        await asyncio.sleep(0)
