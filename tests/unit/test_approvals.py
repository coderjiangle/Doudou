"""人工确认中介、决策落库、确认卡片。

这里钉住的是「不确定时偏向安全」的那些点：
超时按拒绝、重复点击幂等、待决记录不算「已经问过」。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from doudou.agent.approvals import ApprovalBroker, ApprovalOutcome
from doudou.agent.react import ToolApprovalRequest
from doudou.channels.feishu import cards
from doudou.config.schema import StorageConfig
from doudou.storage.approvals import SqlApprovalStore
from doudou.storage.database import Database, create_database

# --------------------------------------------------------------------------- 中介本体


async def _decide_when_pending(
    broker: ApprovalBroker, *, approved: bool = True, reason: str = "", budget: float = 2.0
) -> str:
    """等出现待决请求后落一次决策，返回 ``approval_id``。

    为什么不直接 ``sleep(0)``：``request()`` 在登记**之前**可能先 await 一次
    持久化查询，因此「立刻」并不保证待决列表已经就绪。轮询既稳又不依赖内部实现。
    """
    deadline = asyncio.get_running_loop().time() + budget
    while asyncio.get_running_loop().time() < deadline:
        if broker.pending:
            approval_id = broker.pending[0].approval_id
            await broker.decide(approval_id, approved=approved, reason=reason)
            return approval_id
        await asyncio.sleep(0.01)
    raise AssertionError("等待超时：始终没有出现待决请求")


@pytest.fixture
def broker() -> ApprovalBroker:
    return ApprovalBroker(timeout=5.0)


async def test_decide_approves(broker: ApprovalBroker) -> None:
    """决策到达后等待方立刻醒来，且结果为「同意」。"""
    outcome, approval_id = await asyncio.gather(
        broker.request(tool_name="write_file", args={"path": "a.md"}, call_id="c1"),
        _decide_when_pending(broker),
    )

    assert outcome.approved is True
    assert outcome.status == "approved"
    assert approval_id  # 决策确实是通过 approval_id 落的


async def test_decide_rejects(broker: ApprovalBroker) -> None:
    outcome, _ = await asyncio.gather(
        broker.request(tool_name="write_file", args={}, call_id="c1"),
        _decide_when_pending(broker, approved=False, reason="不要"),
    )

    assert outcome.approved is False
    assert outcome.reason == "不要"


async def test_broker_warns_when_nobody_can_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有人负责通知用户时，broker 必须自己喊一声。

    否则这次运行只会静静地等到超时 —— 用户以为卡住了，运维不知道它在等什么。
    这里钉的是**可发现性**：告警里必须带上「去哪里落决策」。
    """
    from doudou.agent import approvals as approvals_module

    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        approvals_module.logger, "warning", lambda event, **kw: calls.append((event, kw))
    )

    broker = ApprovalBroker(timeout=0.05)
    assert broker.on_request is None

    outcome = await broker.request(tool_name="workspace_write", args={"path": "a.md"}, call_id="c1")

    assert outcome.status == "expired"
    events = [event for event, _ in calls]
    assert "approval.awaiting_decision" in events
    hint = next(kw for event, kw in calls if event == "approval.awaiting_decision")["hint"]
    assert "/approvals/" in hint
    assert "拒绝" in hint


async def test_timeout_is_rejected(caplog: pytest.LogCaptureFixture) -> None:
    """等不到决策 → 拒绝。悬着的确认等于没有确认，默认必须偏向安全。"""
    broker = ApprovalBroker(timeout=1.0)
    expiry_marker: list[str] = []

    broker.on_request = lambda request: _mark_expired(request, expiry_marker)  # type: ignore[assignment]

    # 把等待时长压到毫秒级，避免测试真的等 1 秒
    broker.timeout = 0.05
    outcome = await broker.request(
        tool_name="shell_exec", args={"command": "rm -rf /"}, call_id="c9"
    )

    assert outcome.approved is False
    assert outcome.status == "expired"
    assert "超过" in outcome.reason
    assert expiry_marker == ["notified"]


async def _mark_expired(request: object, sink: list[str]) -> None:
    sink.append("notified")


async def test_decide_is_idempotent(broker: ApprovalBroker) -> None:
    """飞书卡片可能被点两次；第二次只能返回第一次的结果。"""
    first: ApprovalOutcome | None = None
    second: ApprovalOutcome | None = None

    async def double_click() -> None:
        nonlocal first, second
        approval_id = await _decide_when_pending(broker, approved=True)
        first = await broker.decide(approval_id, approved=True)
        second = await broker.decide(approval_id, approved=False)

    await asyncio.gather(
        broker.request(tool_name="write_file", args={}, call_id="c1"), double_click()
    )

    assert first is not None and first.approved is True
    assert second is not None and second.approved is True  # 不翻转


async def test_unknown_approval_id_raises(broker: ApprovalBroker) -> None:
    with pytest.raises(KeyError):
        await broker.decide("ap-does-not-exist", approved=True)


async def test_decision_reused_for_same_call_id(broker: ApprovalBroker) -> None:
    """同一个 ``tool_call_id`` 不再问第二次 —— 这是「跨重启不丢」的可实现部分。"""
    await asyncio.gather(
        broker.request(tool_name="write_file", args={}, call_id="c1"),
        _decide_when_pending(broker, approved=True),
    )

    again = await broker.request(tool_name="write_file", args={}, call_id="c1")

    assert again.approved is True
    assert broker.pending == []  # 没有再挂一条待决


async def test_notify_failure_does_not_break_waiting(broker: ApprovalBroker) -> None:
    """通知用户失败（网络抖动）不该让工具调用崩掉 —— 顶多等超时。"""

    async def boom(request: object) -> None:
        raise RuntimeError("网络不通")

    broker.on_request = boom  # type: ignore[assignment]
    broker.timeout = 0.05

    outcome = await broker.request(tool_name="write_file", args={}, call_id="c1")

    assert outcome.status == "expired"


async def test_hook_adapts_react_request(broker: ApprovalBroker) -> None:
    """``hook()`` 要能把 ReAct 层的请求对象转成中介的调用。"""
    hook = broker.hook()

    approved, _ = await asyncio.gather(
        hook(
            ToolApprovalRequest(
                tool_name="workspace_write", args={"path": "a.md"}, step=2, call_id="c7"
            )
        ),
        _decide_when_pending(broker, approved=True),
    )

    assert approved is True


async def test_lookup_reuses_persisted_decision(broker: ApprovalBroker) -> None:
    """库里已有决策 → 直接复用，不再打扰用户。"""
    calls: list[str] = []

    async def lookup(call_id: str) -> ApprovalOutcome | None:
        calls.append(call_id)
        return ApprovalOutcome(approved=False, status="rejected", reason="上次拒绝")

    broker._lookup = lookup

    outcome = await broker.request(tool_name="write_file", args={}, call_id="c-any")

    assert outcome.approved is False
    assert calls == ["c-any"]
    assert broker.pending == []


def test_describe_shape(broker: ApprovalBroker) -> None:
    described = broker.describe()

    assert described["pending"] == 0
    assert described["timeout_seconds"] == broker.timeout


# --------------------------------------------------------------------------- 落库


@pytest.fixture
async def database(tmp_path: Path):  # type: ignore[no-untyped-def]
    config = StorageConfig(database_url=f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}")
    db: Database = create_database(config)
    await db.create_schema()
    yield db
    await db.dispose()


async def test_store_records_audit_even_when_expired(database: Database) -> None:
    """超时的请求也必须留痕 —— 审计要看到「当时问了什么、结果没人管」。"""
    store = SqlApprovalStore(database)
    broker = ApprovalBroker(timeout=0.05, recorder=store.record, lookup=store.lookup)

    outcome = await broker.request(
        tool_name="write_file", args={"path": "a.md"}, call_id="c-pending"
    )

    assert outcome.status == "expired"
    recent = await store.recent(limit=10)
    assert recent[0]["tool_name"] == "write_file"
    assert recent[0]["args"] == {"path": "a.md"}
    # 但「没人处理」不算一个决策：更新一次运行不该被它挡住确认
    assert await store.lookup("c-pending") is None


async def test_lookup_ignores_pending_records(database: Database) -> None:
    """**待决记录不算「已经问过」** —— 否则重启后悬着的那次会让新运行跳过确认。"""
    store = SqlApprovalStore(database)

    await store.record(_make_request("ap-1", "c1"))

    assert await store.lookup("c1") is None
    assert await store.lookup("") is None
    assert await store.lookup("unknown") is None


async def test_store_roundtrip_decision(database: Database) -> None:
    store = SqlApprovalStore(database)
    broker = ApprovalBroker(timeout=5.0, recorder=store.record, lookup=store.lookup)

    await asyncio.gather(
        broker.request(tool_name="write_file", args={"path": "a.md"}, call_id="c1"),
        _decide_when_pending(broker, approved=True, reason="可以"),
    )

    stored = await store.lookup("c1")

    assert stored is not None
    assert stored.approved is True
    assert stored.reason == "可以"


async def test_store_upsert_does_not_duplicate(database: Database) -> None:
    """同一条请求写两次（登记 + 决策）只应留下一行。"""
    store = SqlApprovalStore(database)

    await store.record(_make_request("ap-1", "c1"))
    await store.record(_make_request("ap-1", "c1", status="approved"))

    recent = await store.recent(limit=10)

    assert len(recent) == 1
    assert recent[0]["status"] == "approved"


def _make_request(approval_id: str, call_id: str, status: str = "pending"):  # type: ignore[no-untyped-def]
    from doudou.agent.approvals import ApprovalRequest

    return ApprovalRequest(
        approval_id=approval_id,
        thread_id="t1",
        tool_name="write_file",
        args={"path": "a.md"},
        call_id=call_id,
        status=status,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- 卡片


def test_approval_card_has_both_buttons() -> None:
    card = cards.approval_card(
        bot_name="Doudou",
        approval_id="ap-0001-abc",
        tool_name="workspace_write",
        args={"path": "report.md", "content": "很长的内容" * 5},
        question="把结果写到文件",
        expires_in=300,
    )

    rendered = str(card)
    assert "ap-0001-abc" in rendered
    assert "同意执行" in rendered
    assert "拒绝" in rendered
    # 参数要展示出来 —— 用户得知道它到底要写什么
    assert "report.md" in rendered


def test_approval_decided_card_has_no_buttons() -> None:
    """决策后不能再有按钮，否则会被重复点击。"""
    card = cards.approval_decided_card(
        bot_name="Doudou", tool_name="workspace_write", approved=False, reason="不要"
    )

    rendered = str(card)
    assert "已拒绝执行" in rendered
    assert "tag': 'action'" not in rendered
    assert "button" not in rendered
