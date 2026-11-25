"""CLI / Voice 远程客户端与临时 serve 子进程管理。

让 ``doudou chat`` 与 ``doudou voice`` 只用一个命令就能做到：

1. 若已有同配置 ``serve`` 在运行则直接复用（退出时不关闭外部服务）；
2. 否则静默在后台拉起临时 ``serve`` 子进程（日志重定向到文件，不污染终端界面），
   并在退出时关闭 stdin 触发其优雅停机，不留常驻后台进程。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import IO, Any

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from doudou.agent.events import RunEvent
from doudou.agent.react import ApprovalHook, RunResult, ToolApprovalRequest
from doudou.config.loader import Settings
from doudou.gateway.history import ThreadInfo
from doudou.gateway.schema import RunRequest
from doudou.observability.logging import get_logger
from doudou.tools.registry import ToolInfo

logger = get_logger(__name__)

_DEFAULT_STARTUP_TIMEOUT = 25.0
_DEFAULT_SHUTDOWN_TIMEOUT = 5.0


class RemoteServerError(RuntimeError):
    """临时 serve 启动失败或远程 API 调用异常。"""


def connect_host_for(host: str) -> str:
    """将通配符监听地址（如 ``0.0.0.0``）转换为客户端可连接的本地回环地址。"""
    if host in {"0.0.0.0", "::", ""}:  # noqa: S104 - 仅用于识别通配地址并映射到本地回环
        return "127.0.0.1"
    return host


def auth_headers(api_token: str | None) -> dict[str, str]:
    if api_token:
        return {"Authorization": f"Bearer {api_token}"}
    return {}


def _is_port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
        return True


def _pick_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _parse_dt(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _build_message(raw: dict[str, Any]) -> BaseMessage:
    role = str(raw.get("role") or "human")
    content = str(raw.get("content") or "")
    if role == "ai":
        return AIMessage(content=content, tool_calls=raw.get("tool_calls") or [])
    if role == "system":
        return SystemMessage(content=content)
    if role == "tool":
        return ToolMessage(
            content=content,
            tool_call_id=str(raw.get("tool_call_id") or ""),
            name=raw.get("name"),
            status="error" if raw.get("status") == "error" else "success",
        )
    return HumanMessage(content=content)


async def _iter_sse_events(
    lines: AsyncIterator[str],
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """按 SSE 协议解析 ``event:`` 与多行 ``data:``，以空行为事件边界。"""
    event_name = "message"
    data_lines: list[str] = []

    async for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if line == "":
            if data_lines:
                joined = "\n".join(data_lines)
                try:
                    payload = json.loads(joined)
                except json.JSONDecodeError:
                    payload = {"raw": joined}
                if isinstance(payload, dict):
                    yield event_name, payload
            event_name = "message"
            data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())

    if data_lines:
        joined = "\n".join(data_lines)
        try:
            payload = json.loads(joined)
        except json.JSONDecodeError:
            payload = {"raw": joined}
        if isinstance(payload, dict):
            yield event_name, payload


class RemoteAgentRunner:
    """通过 HTTP/SSE 调用 ``doudou serve`` 的运行器适配器，实现 :class:`RunnerLike`。"""

    def __init__(
        self,
        settings: Settings,
        *,
        base_url: str,
        approval_hook: ApprovalHook | None = None,
        new_session: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.base_url = base_url.rstrip("/")
        self.approval_hook = approval_hook
        self.new_session = new_session
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers=auth_headers(settings.api_token),
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
            trust_env=False,
        )
        self._model_label = settings.app.model.primary
        self._tool_count = 0
        self._threads: dict[tuple[str, str], str] = {}
        self._pending_new: set[tuple[str, str]] = set()
        self._initialized_keys: set[tuple[str, str]] = set()

    @property
    def model_label(self) -> str:
        return self._model_label

    @property
    def tool_count(self) -> int:
        return self._tool_count

    async def connect(self) -> None:
        """探测 ``/health`` 并读取实际生效的模型与工具数。"""
        resp = await self._client.get(f"{self.base_url}/health")
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            if isinstance(data.get("model"), str) and data["model"]:
                self._model_label = data["model"]
            tools = data.get("tools")
            if isinstance(tools, list):
                self._tool_count = len(tools)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def resolve_thread(self, request: RunRequest) -> str:
        if request.thread_id:
            return request.thread_id
        key = (request.channel, request.conversation_id)
        if not request.new_thread and key not in self._pending_new and key in self._threads:
            return self._threads[key]
        return ""

    async def start_new_thread(self, conversation_id: str, *, channel: str = "cli") -> str:
        key = (channel, conversation_id)
        self._threads.pop(key, None)
        self._pending_new.add(key)
        return "（将在下一条消息开启新会话）"

    async def get_thread(self, thread_id: str) -> ThreadInfo | None:
        if not thread_id:
            return None
        resp = await self._client.get(f"{self.base_url}/threads/{thread_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        thread = resp.json().get("thread") or {}
        return ThreadInfo(
            thread_id=str(thread.get("thread_id") or thread_id),
            channel=thread.get("channel"),
            conversation_id=thread.get("conversation_id"),
            created_at=_parse_dt(thread.get("created_at")),
            updated_at=_parse_dt(thread.get("updated_at")),
            message_count=int(thread.get("message_count") or 0),
        )

    async def history(self, thread_id: str) -> list[BaseMessage]:
        if not thread_id:
            return []
        resp = await self._client.get(f"{self.base_url}/threads/{thread_id}")
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        raw_messages = resp.json().get("messages") or []
        return [_build_message(item) for item in raw_messages if isinstance(item, dict)]

    async def list_tool_infos(self) -> Sequence[ToolInfo]:
        resp = await self._client.get(f"{self.base_url}/tools")
        resp.raise_for_status()
        raw_tools = resp.json().get("tools") or []
        return [
            ToolInfo(
                name=str(item.get("name") or ""),
                description=str(item.get("description") or ""),
                group=str(item.get("group") or ""),
                source=str(item.get("source") or ""),
                args={str(k): str(v) for k, v in (item.get("args") or {}).items()},
                requires_approval=bool(item.get("requires_approval")),
                side_effects=bool(item.get("side_effects")),
                timeout=float(item["timeout"]) if item.get("timeout") is not None else None,
            )
            for item in raw_tools
            if isinstance(item, dict)
        ]

    async def astream(
        self, request: RunRequest, *, thread_id: str | None = None
    ) -> AsyncIterator[RunEvent]:
        key = (request.channel, request.conversation_id)
        pending_new = key in self._pending_new
        effective_thread = thread_id or (None if pending_new else self._threads.get(key))
        want_new = (
            request.new_thread
            or pending_new
            or (self.new_session and key not in self._initialized_keys and not effective_thread)
        )

        payload: dict[str, Any] = {
            "message": request.text,
            "channel": request.channel,
            "conversation_id": request.conversation_id,
            "new_thread": want_new,
        }
        if effective_thread:
            payload["thread_id"] = effective_thread
        if request.images:
            payload["images"] = [img.model_dump(mode="json") for img in request.images]

        resolved_thread = effective_thread
        async with self._client.stream(
            "POST", f"{self.base_url}/chat/stream", json=payload
        ) as response:
            if response.status_code >= 400:
                detail = (await response.aread()).decode("utf-8", errors="replace")
                raise RemoteServerError(f"服务端返回 HTTP {response.status_code}：{detail}")

            async for event_name, data in _iter_sse_events(response.aiter_lines()):
                if event_name == "start":
                    resolved_thread = str(data.get("thread_id") or resolved_thread or "")
                    if resolved_thread:
                        self._threads[key] = resolved_thread
                        self._pending_new.discard(key)
                        self._initialized_keys.add(key)
                    yield RunEvent.run_start(thread_id=resolved_thread or None)
                elif event_name == "delta":
                    yield RunEvent.text_delta(
                        str(data.get("text") or ""),
                        step=1,
                        thread_id=resolved_thread or None,
                    )
                elif event_name == "tool_call":
                    yield RunEvent.tool_call(
                        step=int(data.get("step") or 1),
                        name=str(data.get("name") or ""),
                        call_id=str(data.get("call_id") or ""),
                        args=dict(data.get("args") or {}),
                        thread_id=resolved_thread or None,
                    )
                elif event_name == "approval_required":
                    await self._handle_approval(data)
                elif event_name == "tool_result":
                    elapsed = data.get("elapsed")
                    yield RunEvent.tool_result(
                        step=1,
                        name=str(data.get("name") or ""),
                        call_id=str(data.get("call_id") or ""),
                        result=str(data.get("result") or ""),
                        ok=bool(data.get("ok", True)),
                        elapsed=float(elapsed) if isinstance(elapsed, (int, float)) else None,
                        thread_id=resolved_thread or None,
                    )
                elif event_name == "done":
                    final_thread = str(data.get("thread_id") or resolved_thread or "")
                    if final_thread:
                        self._threads[key] = final_thread
                        self._pending_new.discard(key)
                        self._initialized_keys.add(key)
                    usage = {
                        str(k): int(v)
                        for k, v in (data.get("usage") or {}).items()
                        if isinstance(v, (int, float))
                    }
                    yield RunEvent.run_end(
                        str(data.get("text") or ""),
                        step=int(data.get("steps") or 0),
                        usage=usage,
                        thread_id=final_thread or None,
                    )
                elif event_name == "error":
                    yield RunEvent.error(
                        str(data.get("message") or "服务端运行出错"),
                        step=1,
                        thread_id=data.get("thread_id") or resolved_thread or None,
                    )

    async def _handle_approval(self, data: dict[str, Any]) -> None:
        approval_id = str(data.get("approval_id") or "")
        if not approval_id:
            return
        req = ToolApprovalRequest(
            tool_name=str(data.get("tool_name") or ""),
            args=dict(data.get("args") or {}),
            step=int(data.get("step") or 0),
            call_id=str(data.get("call_id") or ""),
        )
        if self.approval_hook is not None:
            approved = await self.approval_hook(req)
            reason = "" if approved else "客户端拒绝"
        else:
            approved = False
            reason = "客户端未开启交互确认"
        resp = await self._client.post(
            f"{self.base_url}/approvals/{approval_id}",
            json={"approved": approved, "reason": reason},
        )
        resp.raise_for_status()

    async def ainvoke(self, request: RunRequest, *, thread_id: str | None = None) -> RunResult:
        resolved_thread = thread_id or await self.resolve_thread(request)
        events: list[RunEvent] = []
        text = ""
        steps = 0
        usage: dict[str, int] = {}
        error: str | None = None

        async for event in self.astream(request, thread_id=resolved_thread):
            events.append(event)
            if event.type == "run_end":
                text = event.text
                steps = event.step
                usage = event.usage
                resolved_thread = event.thread_id or resolved_thread
            elif event.type == "error":
                error = event.text
                steps = event.step

        return RunResult(
            text=text,
            steps=steps,
            messages=await self.history(resolved_thread),
            usage=usage,
            events=events,
            ok=error is None,
            error=error,
        )


class EphemeralServeProcess:
    """管理随 CLI 启停的本地 ``doudou serve`` 子进程（或复用已在线服务）。"""

    def __init__(
        self,
        *,
        base_url: str,
        process: subprocess.Popen[bytes] | None = None,
        log_file: IO[bytes] | None = None,
        log_path: Path | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._process = process
        self._log_file = log_file
        self.log_path = log_path

    @property
    def owned(self) -> bool:
        return self._process is not None

    @classmethod
    async def attach_or_spawn(
        cls,
        settings: Settings,
        *,
        server_url: str | None = None,
        config_dir: Path | None = None,
        model_spec: str | None = None,
        startup_timeout: float = _DEFAULT_STARTUP_TIMEOUT,
    ) -> EphemeralServeProcess:
        base_path = settings.app.server.base_path.rstrip("/")
        if server_url:
            base_url = _normalize_server_url(server_url, base_path)
            if not await _probe_health(base_url, api_token=settings.api_token, timeout_seconds=2.0):
                raise RemoteServerError(f"无法连接到指定的服务端：{base_url}")
            return cls(base_url=base_url)

        host = connect_host_for(settings.app.server.host)
        configured_port = settings.app.server.port
        configured_base = f"http://{host}:{configured_port}{base_path}"

        # 没有显式覆盖配置/模型且本机已有服务在线时，直接复用现有 serve，退出时不关闭它。
        if config_dir is None and model_spec is None:
            if await _probe_health(
                configured_base,
                api_token=settings.api_token,
                timeout_seconds=0.6,
            ):
                logger.info("cli.attached_existing_serve", base_url=configured_base)
                return cls(base_url=configured_base)

        port = (
            configured_port if _is_port_available(host, configured_port) else _pick_free_port(host)
        )
        base_url = f"http://{host}:{port}{base_path}"
        log_file, log_path = _open_serve_log(settings.home_dir)

        cmd = [
            sys.executable,
            "-m",
            "doudou",
            "serve",
            "--host",
            host,
            "--port",
            str(port),
            "--ephemeral-stdin-exit",
        ]
        if config_dir is not None:
            cmd.extend(["--config-dir", str(config_dir)])

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if config_dir is not None:
            env["DOUDOU_CONFIG_DIR"] = str(config_dir)
        if model_spec is not None:
            env["DOUDOU_MODEL_PRIMARY"] = model_spec

        proc = await asyncio.to_thread(_spawn_serve, cmd, env=env, log_file=log_file)
        instance = cls(base_url=base_url, process=proc, log_file=log_file, log_path=log_path)
        try:
            await instance._wait_ready(
                api_token=settings.api_token,
                timeout_seconds=startup_timeout,
            )
        except Exception:
            await instance.aclose()
            raise
        logger.info("cli.ephemeral_serve_started", pid=proc.pid, base_url=base_url)
        return instance

    async def _wait_ready(self, *, api_token: str | None, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._process is not None:
                code = self._process.poll()
                if code is not None:
                    tail = _read_log_tail(self.log_path)
                    detail = f"\n{tail}" if tail else ""
                    raise RemoteServerError(f"后台 serve 启动后立即退出（退出码 {code}）。{detail}")
            if await _probe_health(self.base_url, api_token=api_token, timeout_seconds=0.5):
                return
            await asyncio.sleep(0.15)
        tail = _read_log_tail(self.log_path)
        detail = f"\n{tail}" if tail else ""
        raise RemoteServerError(f"等待后台 serve 就绪超时（>{timeout_seconds:.0f}s）。{detail}")

    async def aclose(self) -> None:
        proc = self._process
        self._process = None
        try:
            if proc is not None and proc.poll() is None:
                if proc.stdin is not None:
                    with suppress(OSError):
                        proc.stdin.close()
                try:
                    await asyncio.to_thread(proc.wait, _DEFAULT_SHUTDOWN_TIMEOUT)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        await asyncio.to_thread(proc.wait, 2.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        with suppress(Exception):
                            await asyncio.to_thread(proc.wait, 1.0)
                logger.info("cli.ephemeral_serve_stopped", pid=proc.pid, returncode=proc.returncode)
        finally:
            if self._log_file is not None:
                with suppress(OSError):
                    self._log_file.close()
                self._log_file = None


def _normalize_server_url(raw: str, base_path: str) -> str:
    trimmed = raw.rstrip("/")
    if base_path and trimmed.endswith(base_path):
        return trimmed
    return f"{trimmed}{base_path}"


async def _probe_health(base_url: str, *, api_token: str | None, timeout_seconds: float) -> bool:
    try:
        async with httpx.AsyncClient(
            headers=auth_headers(api_token),
            timeout=timeout_seconds,
            trust_env=False,
        ) as client:
            resp = await client.get(f"{base_url}/health")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


def _spawn_serve(
    cmd: list[str],
    *,
    env: dict[str, str],
    log_file: IO[bytes] | None,
) -> subprocess.Popen[bytes]:
    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": log_file if log_file is not None else subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
        "env": env,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(cmd, **popen_kwargs)  # noqa: S603


def _open_serve_log(home_dir: Path) -> tuple[IO[bytes] | None, Path | None]:
    try:
        log_path = home_dir / "data" / "serve-ephemeral.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        return log_path.open("ab"), log_path
    except OSError:
        return None, None


def _read_log_tail(log_path: Path | None, max_lines: int = 12) -> str:
    if log_path is None or not log_path.is_file():
        return ""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:]).strip()


__all__ = [
    "EphemeralServeProcess",
    "RemoteAgentRunner",
    "RemoteServerError",
    "auth_headers",
    "connect_host_for",
]
