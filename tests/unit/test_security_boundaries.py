"""M8：路径权限、写类文件工具、命令执行、子 Agent 委派。

安全相关的用例刻意写得「烦」一点：安全的默认值不是靠一次直觉断言保证的，
而是靠把每个方向（允许/拒绝、首个匹配、逃逸尝试）都钉住。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from doudou.agent.permissions import (
    PermissionDenied,
    PermissionPolicy,
    glob_to_regex,
    normalize_workspace_path,
    relative_workspace_path,
)
from doudou.agent.subagents import (
    SubAgentRegistry,
    SubAgentSpec,
    load_subagents,
    select_tools,
)
from doudou.agent.testing import ScriptedChatModel, tool_call
from doudou.config.loader import Settings
from doudou.tools.builtin import delegation, files, shell

# --------------------------------------------------------------------------- 权限规则


@pytest.fixture
def policy_settings(settings_factory, tmp_path: Path):  # type: ignore[no-untyped-def]
    """规则：拒绝 .env / 密钥 / secrets 目录，其余放行。"""

    def make(*, yaml: str | None = None) -> Settings:
        rules = yaml or (
            "  permissions:\n"
            "    - operations: [read, write]\n"
            '      paths: ["/**/.env", "/**/*.key", "/secrets/**"]\n'
            "      mode: deny\n"
        )
        return settings_factory(
            config_yaml=(
                f'agent:\n{rules}filesystem:\n  root_dir: "{(tmp_path / "workspace").as_posix()}"\n'
            )
        )

    return make


def test_glob_semantics() -> None:
    """``**`` 的边界是安全上的关键：漏掉根目录的 .env 就等于没拦。"""
    assert glob_to_regex("/**").match("/.env")
    assert glob_to_regex("/**/.env").match("/.env")
    assert glob_to_regex("/**/.env").match("/a/b/.env")
    assert glob_to_regex("/secrets/**").match("/secrets")
    assert glob_to_regex("/secrets/**").match("/secrets/x/y")
    assert not glob_to_regex("/secrets/**").match("/secretsx")
    assert glob_to_regex("/*.log").match("/a.log")
    assert not glob_to_regex("/*.log").match("/sub/a.log")


def test_normalize_workspace_path() -> None:
    assert normalize_workspace_path("a\\b/./c") == "/a/b/c"
    assert normalize_workspace_path("") == "/"
    assert normalize_workspace_path("/a//b") == "/a/b"


def test_relative_workspace_path_inside(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    target = root / "notes" / "a.md"

    assert relative_workspace_path(root, target) == "/notes/a.md"


def test_relative_workspace_path_outside(tmp_path: Path) -> None:
    """区外路径返回绝对形态 —— 这个形态**不能**用来判断「在工作区内」，
    因为 ``/**`` 同样会匹配它。真正的判断在 ``require_target`` 里。"""
    root = tmp_path / "ws"
    outside = tmp_path / "outside.md"

    shown = relative_workspace_path(root, outside)

    assert shown.startswith("/")
    assert "outside.md" in shown
    assert glob_to_regex("/**").match(shown) is not None


def test_policy_require_target_enforces_containment(policy_settings, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """纵深防御：即使某天新工具忘了检查，权限层也拦得住区外路径。"""
    settings = policy_settings()
    policy = PermissionPolicy(settings.app)

    with pytest.raises(PermissionDenied, match="工作区"):
        policy.require_target("write", tmp_path / "outside.md", root=settings.root_dir)


def test_policy_allows_when_no_rules(settings: Settings) -> None:
    """没配规则就放行 —— 硬边界（跑不出工作区）由工具层保证。"""
    policy = PermissionPolicy(settings.app)

    assert policy.enabled is False
    assert policy.check("write", "/report.md").allowed is True


def test_policy_denies_sensitive_paths(policy_settings) -> None:  # type: ignore[no-untyped-def]
    policy = PermissionPolicy(policy_settings().app)

    for path in ("/.env", "/sub/.env", "/app.key", "/secrets/token.txt"):
        assert policy.check("read", path).allowed is False, path
        assert policy.check("write", path).allowed is False, path


def test_policy_allows_normal_paths(policy_settings) -> None:  # type: ignore[no-untyped-def]
    policy = PermissionPolicy(policy_settings().app)

    for path in ("/report.md", "/notes/todo.md", "/env", "/secretsx/a.txt"):
        assert policy.check("write", path).allowed is True, path


def test_policy_first_match_wins(settings_factory, tmp_path: Path) -> None:
    """首匹配生效：拒绝在前就拒绝，即使后面有放行规则。"""
    allow_all = (
        "  permissions:\n"
        "    - operations: [write]\n"
        '      paths: ["/**/.env"]\n'
        "      mode: deny\n"
        "    - operations: [write]\n"
        '      paths: ["/**"]\n'
        "      mode: allow\n"
    )
    settings = settings_factory(
        config_yaml=(
            f'agent:\n{allow_all}filesystem:\n  root_dir: "{(tmp_path / "ws").as_posix()}"\n'
        )
    )
    policy = PermissionPolicy(settings.app)

    assert policy.check("write", "/.env").allowed is False
    assert policy.check("write", "/report.md").allowed is True


def test_policy_deny_all_then_allow_specific(settings_factory, tmp_path: Path) -> None:
    """反向顺序：先拒绝一切，再放行工作区 —— 这是「默认拒绝」的标准写法。"""
    deny_then_allow = (
        "  permissions:\n"
        "    - operations: [write]\n"
        '      paths: ["/**"]\n'
        "      mode: deny\n"
        "    - operations: [write]\n"
        '      paths: ["/public/**"]\n'
        "      mode: allow\n"
    )
    settings = settings_factory(
        config_yaml=(
            f'agent:\n{deny_then_allow}filesystem:\n  root_dir: "{(tmp_path / "ws").as_posix()}"\n'
        )
    )
    policy = PermissionPolicy(settings.app)

    # 首匹配是 deny /**，所以连 /public 也被拒 —— 顺序错了就达不到预期
    assert policy.check("write", "/public/a.txt").allowed is False


def test_policy_operation_scoping(settings_factory, tmp_path: Path) -> None:
    """规则可以只管读或只管写。"""
    rules = (
        "  permissions:\n"
        "    - operations: [write]\n"
        '      paths: ["/protected/**"]\n'
        "      mode: deny\n"
    )
    settings = settings_factory(
        config_yaml=(f'agent:\n{rules}filesystem:\n  root_dir: "{(tmp_path / "ws").as_posix()}"\n')
    )
    policy = PermissionPolicy(settings.app)

    assert policy.check("write", "/protected/a").allowed is False
    assert policy.check("read", "/protected/a").allowed is True


def test_policy_require_raises_with_rule_text(policy_settings) -> None:  # type: ignore[no-untyped-def]
    """拒绝信息里要带上规则原文 —— 模型才知道为什么，人也才知道改哪条。"""
    policy = PermissionPolicy(policy_settings().app)

    with pytest.raises(PermissionDenied, match="命中规则 #0"):
        policy.require("write", "/.env")


def test_policy_describe(policy_settings) -> None:  # type: ignore[no-untyped-def]
    described = PermissionPolicy(policy_settings().app).describe()

    assert described[0]["mode"] == "deny"
    assert "/**/.env" in described[0]["paths"]  # type: ignore[operator]


# --------------------------------------------------------------------------- 写类工具


@pytest.fixture
def workspace(settings_factory, tmp_path: Path):  # type: ignore[no-untyped-def]
    """工作区 + 拒绝 .env 的规则。"""

    def make(*, deny: str = "/**/.env") -> tuple[Settings, Path]:
        settings = settings_factory(
            config_yaml=(
                "agent:\n"
                "  permissions:\n"
                "    - operations: [read, write]\n"
                f'      paths: ["{deny}"]\n'
                "      mode: deny\n"
                "filesystem:\n"
                f'  root_dir: "{(tmp_path / "workspace").as_posix()}"\n'
            )
        )
        root = settings.root_dir
        root.mkdir(parents=True, exist_ok=True)
        return settings, root

    return make


def tools_by_name(settings: Settings) -> dict:
    return {item.name: item for item in files.build_tools(settings)}


def test_write_creates_file(workspace) -> None:  # type: ignore[no-untyped-def]
    settings, root = workspace()
    tools = tools_by_name(settings)

    result = tools["workspace_write"].invoke({"path": "report.md", "content": "# 报告"})

    assert "已创建" in result
    assert (root / "report.md").read_text(encoding="utf-8") == "# 报告"


def test_write_creates_parent_directories(workspace) -> None:  # type: ignore[no-untyped-def]
    settings, root = workspace()

    tools_by_name(settings)["workspace_write"].invoke({"path": "deep/nested/a.md", "content": "x"})

    assert (root / "deep" / "nested" / "a.md").is_file()


def test_write_reports_overwrite(workspace) -> None:  # type: ignore[no-untyped-def]
    settings, _root = workspace()
    tools = tools_by_name(settings)
    tools["workspace_write"].invoke({"path": "a.md", "content": "第一版"})

    result = tools["workspace_write"].invoke({"path": "a.md", "content": "第二版"})

    assert "已覆盖" in result


def test_write_rejects_path_escape(workspace) -> None:  # type: ignore[no-untyped-def]
    settings, _root = workspace()
    tools = tools_by_name(settings)

    with pytest.raises(files.FilesError, match="工作区之外"):
        tools["workspace_write"].invoke({"path": "../outside.md", "content": "x"})


def test_write_rejects_denied_path(workspace) -> None:  # type: ignore[no-untyped-def]
    """权限规则拒绝时，写操作必须失败 —— 这是「写工作区外被拒」的同类路径。"""
    settings, root = workspace(deny="/**/.env")
    tools = tools_by_name(settings)

    with pytest.raises(PermissionDenied, match="权限规则拒绝"):
        tools["workspace_write"].invoke({"path": ".env", "content": "SECRET=1"})
    assert not (root / ".env").exists()


def test_read_rejects_denied_path(workspace) -> None:  # type: ignore[no-untyped-def]
    settings, root = workspace(deny="/**/*.key")
    (root / "app.key").write_text("secret", encoding="utf-8")
    tools = tools_by_name(settings)

    with pytest.raises(PermissionDenied):
        tools["workspace_read"].invoke({"path": "app.key"})


def test_write_rejects_directory_target(workspace) -> None:  # type: ignore[no-untyped-def]
    settings, root = workspace()
    (root / "adir").mkdir()

    with pytest.raises(files.FilesError, match="是一个目录"):
        tools_by_name(settings)["workspace_write"].invoke({"path": "adir", "content": "x"})


def test_write_respects_size_limit(settings_factory, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    settings = settings_factory(
        config_yaml=(
            "tools:\n  files:\n    max_write_chars: 20\n"
            "filesystem:\n"
            f'  root_dir: "{(tmp_path / "ws").as_posix()}"\n'
        )
    )

    with pytest.raises(files.FilesError, match="超过单次写入上限"):
        tools_by_name(settings)["workspace_write"].invoke({"path": "a.md", "content": "x" * 50})


def test_edit_replaces_once(workspace) -> None:  # type: ignore[no-untyped-def]
    settings, root = workspace()
    tools = tools_by_name(settings)
    tools["workspace_write"].invoke({"path": "a.md", "content": "第一行\n第二行\n"})

    result = tools["workspace_edit"].invoke(
        {"path": "a.md", "old": "第二行", "new": "改过的第二行"}
    )

    assert "替换 1 处" in result
    assert (root / "a.md").read_text(encoding="utf-8") == "第一行\n改过的第二行\n"


def test_edit_rejects_missing_text(workspace) -> None:  # type: ignore[no-untyped-def]
    settings, _root = workspace()
    tools = tools_by_name(settings)
    tools["workspace_write"].invoke({"path": "a.md", "content": "内容"})

    with pytest.raises(files.FilesError, match="找不到要替换的原文"):
        tools["workspace_edit"].invoke({"path": "a.md", "old": "不存在", "new": "x"})


def test_edit_rejects_ambiguous_text(workspace) -> None:  # type: ignore[no-untyped-def]
    """出现多次时拒绝 —— 硬替换会改到不该改的地方。"""
    settings, root = workspace()
    tools = tools_by_name(settings)
    tools["workspace_write"].invoke({"path": "a.md", "content": "重复\n重复\n"})

    with pytest.raises(files.FilesError, match="出现了 2 次"):
        tools["workspace_edit"].invoke({"path": "a.md", "old": "重复", "new": "x"})
    # 文件必须没被改动 —— 拒绝就是拒绝，不能改一半
    assert (root / "a.md").read_text(encoding="utf-8") == "重复\n重复\n"


def test_delete_file(workspace) -> None:  # type: ignore[no-untyped-def]
    settings, root = workspace()
    tools = tools_by_name(settings)
    tools["workspace_write"].invoke({"path": "a.md", "content": "x"})

    assert "已删除" in tools["workspace_delete"].invoke({"path": "a.md"})
    assert not (root / "a.md").exists()


def test_delete_refuses_non_empty_directory(workspace) -> None:  # type: ignore[no-untyped-def]
    """非空目录不删 —— 一次删掉一堆东西是不可逆的。"""
    settings, _root = workspace()
    tools = tools_by_name(settings)
    tools["workspace_write"].invoke({"path": "dir/a.md", "content": "x"})

    with pytest.raises(files.FilesError, match="非空"):
        tools["workspace_delete"].invoke({"path": "dir"})


def test_delete_refuses_root(workspace) -> None:  # type: ignore[no-untyped-def]
    settings, _root = workspace()

    with pytest.raises(files.FilesError, match="不能删除工作区根目录"):
        tools_by_name(settings)["workspace_delete"].invoke({"path": "."})


def test_delete_missing_file(workspace) -> None:  # type: ignore[no-untyped-def]
    settings, _root = workspace()

    with pytest.raises(files.FilesError, match="没有"):
        tools_by_name(settings)["workspace_delete"].invoke({"path": "nope.md"})


# --------------------------------------------------------------------------- 命令执行


def test_shell_disabled_by_default(settings: Settings) -> None:
    """默认不开 —— 它是唯一没有路径约束机制的出口。"""
    assert shell.build_tools(settings) == []


@pytest.fixture
def shell_settings(settings_factory, tmp_path: Path):  # type: ignore[no-untyped-def]
    def make(*, enabled: bool = True, timeout: float = 10.0) -> Settings:
        return settings_factory(
            config_yaml=(
                "tools:\n"
                "  shell:\n"
                f"    enabled: {'true' if enabled else 'false'}\n"
                f"    timeout: {timeout}\n"
                "filesystem:\n"
                f'  root_dir: "{(tmp_path / "ws").as_posix()}"\n'
            )
        )

    return make


async def test_shell_runs_command_in_workspace(shell_settings) -> None:  # type: ignore[no-untyped-def]
    import sys

    settings = shell_settings()
    tool_instance = shell.build_tools(settings)[0]

    result = await tool_instance.ainvoke({"command": f'"{sys.executable}" -c "print(1+1)"'})

    assert "退出码 0" in result
    assert "2" in result


async def test_shell_reports_nonzero_exit(shell_settings) -> None:  # type: ignore[no-untyped-def]
    import sys

    tool_instance = shell.build_tools(shell_settings())[0]

    result = await tool_instance.ainvoke(
        {"command": f'"{sys.executable}" -c "raise SystemExit(3)"'}
    )

    assert "退出码 3" in result


async def test_shell_enforces_timeout(shell_settings) -> None:  # type: ignore[no-untyped-def]
    import sys

    tool_instance = shell.build_tools(shell_settings(timeout=1.0))[0]

    with pytest.raises(shell.ShellError, match="超时"):
        await tool_instance.ainvoke(
            {"command": f'"{sys.executable}" -c "import time; time.sleep(10)"'}
        )


async def test_shell_rejects_empty_command(shell_settings) -> None:  # type: ignore[no-untyped-def]
    tool_instance = shell.build_tools(shell_settings())[0]

    with pytest.raises(shell.ShellError, match="不能为空"):
        await tool_instance.ainvoke({"command": "   "})


async def test_shell_truncates_output(shell_settings) -> None:  # type: ignore[no-untyped-def]
    import sys

    tool_instance = shell.build_tools(shell_settings())[0]

    result = await tool_instance.ainvoke({"command": f'"{sys.executable}" -c "print(\'x\'*50000)"'})

    assert "已截断" in result
    assert len(result) < 25000


async def test_shell_does_not_inherit_secrets(shell_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """不能把 API Key 透传给模型临时拼出来的命令。"""
    import sys

    monkeypatch.setenv("DOUDOU_PROBE_SECRET", "s3cret-value")
    tool_instance = shell.build_tools(shell_settings())[0]

    result = await tool_instance.ainvoke(
        {
            "command": f'"{sys.executable}" -c "import os; print(os.environ.get(\'DOUDOU_PROBE_SECRET\'))"'
        }
    )

    assert "s3cret-value" not in result


# --------------------------------------------------------------------------- 子 Agent


def write_agents_yaml(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "agents.yaml"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_subagents(tmp_path: Path) -> None:
    path = write_agents_yaml(
        tmp_path,
        {
            "subagents": [
                {
                    "name": "researcher",
                    "description": "查资料",
                    "prompt": "你是研究员",
                    "tools": ["web_search"],
                },
                {"name": "planner", "description": "做规划", "prompt": "你是规划者"},
            ]
        },
    )

    registry = load_subagents(path)

    assert registry.names == ["researcher", "planner"]
    spec = registry.get("researcher")
    assert spec is not None
    assert spec.system_prompt == "你是研究员"
    assert spec.tools == ["web_search"]
    assert spec.max_steps == 6


def test_load_subagents_missing_file(tmp_path: Path) -> None:
    assert len(load_subagents(tmp_path / "nope.yaml")) == 0
    assert len(load_subagents(None)) == 0


def test_load_subagents_skips_invalid_entries(tmp_path: Path) -> None:
    """一条写坏不该让整个注册表为空，也不该静默通过。"""
    path = write_agents_yaml(
        tmp_path,
        {
            "subagents": [
                {"name": "good", "description": "d", "prompt": "p"},
                {"name": "BadName", "description": "d", "prompt": "p"},
                {"name": "missing_prompt", "description": "d"},
                {"name": "good", "description": "重复", "prompt": "p"},
            ]
        },
    )

    registry = load_subagents(path)

    assert registry.names == ["good"]


def test_load_subagents_rejects_unknown_field(tmp_path: Path) -> None:
    path = write_agents_yaml(
        tmp_path,
        {"subagents": [{"name": "a", "description": "d", "prompt": "p", "tols": ["x"]}]},
    )

    assert len(load_subagents(path)) == 0


def test_select_tools_always_excludes_task() -> None:
    """子 Agent 拿到 task 就能无限嵌套 —— 必须永远排除。"""
    spec = SubAgentSpec(name="a", description="d", prompt="p")

    @tool
    def task(description: str) -> str:
        """委派。"""
        return "x"

    @tool
    def search(q: str) -> str:
        """搜索。"""
        return q

    selected = select_tools(spec, [task, search])

    assert [item.name for item in selected] == ["search"]


def test_select_tools_respects_whitelist() -> None:
    spec = SubAgentSpec(name="a", description="d", prompt="p", tools=["search"])

    @tool
    def search(q: str) -> str:
        """搜索。"""
        return q

    @tool
    def write_file(path: str) -> str:
        """写。"""
        return path

    selected = select_tools(spec, [search, write_file])

    assert [item.name for item in selected] == ["search"]


def test_select_tools_warns_on_unknown_names() -> None:
    spec = SubAgentSpec(name="a", description="d", prompt="p", tools=["nope"])

    @tool
    def search(q: str) -> str:
        """搜索。"""
        return q

    assert select_tools(spec, [search]) == []


def test_registry_describe() -> None:
    registry = SubAgentRegistry([SubAgentSpec(name="a", description="d", prompt="p")])

    assert registry.describe()[0]["name"] == "a"
    assert len(registry) == 1


# --------------------------------------------------------------------------- 委派工具


@pytest.fixture
def delegation_settings(settings_factory, tmp_path: Path):  # type: ignore[no-untyped-def]
    def make(*, subagents: list[dict] | None = None) -> Settings:
        path = write_agents_yaml(
            tmp_path,
            {
                "subagents": subagents
                or [{"name": "helper", "description": "帮忙", "prompt": "你是帮手"}]
            },
        )
        return settings_factory(
            config_yaml=(
                "agent:\n"
                f'  subagents_file: "{path.as_posix()}"\n'
                "filesystem:\n"
                f'  root_dir: "{(tmp_path / "ws").as_posix()}"\n'
            )
        )

    return make


def test_task_not_registered_without_subagents(settings: Settings) -> None:
    assert delegation.build_tools(settings, available_tools=[], model=object()) == []


def test_task_not_registered_without_model(delegation_settings) -> None:  # type: ignore[no-untyped-def]
    """没有模型就驱动不了子 Agent，别给一个永远失败的工具。"""

    @tool
    def search(q: str) -> str:
        """搜索。"""
        return q

    assert delegation.build_tools(delegation_settings(), available_tools=[search]) == []


def test_task_registered_with_everything(delegation_settings) -> None:  # type: ignore[no-untyped-def]
    @tool
    def search(q: str) -> str:
        """搜索。"""
        return q

    tools = delegation.build_tools(
        delegation_settings(),
        available_tools=[search],
        model=ScriptedChatModel(steps=[]),
    )

    assert [item.name for item in tools] == ["task"]


async def test_task_returns_only_conclusion(delegation_settings) -> None:  # type: ignore[no-untyped-def]
    """验收：子 Agent 只回传一段结论，中间过程不进主上下文。"""
    settings = delegation_settings()

    @tool
    def search(q: str) -> str:
        """搜索。"""
        return f"关于 {q} 的一堆原始结果"

    child_model = ScriptedChatModel(
        steps=[
            tool_call("search", {"q": "x"}, call_id="c1"),
            AIMessage(content="结论：X 是 Y。来源：example.com"),
        ]
    )
    tools = delegation.build_tools(settings, available_tools=[search], model=child_model)
    task = tools[0]

    result = await task.ainvoke({"description": "调研一下 X 是什么"})

    assert "结论：X 是 Y" in result
    assert "来自子 Agent helper" in result
    # 子 Agent 的中间过程不该出现在回传内容里
    assert "一堆原始结果" not in result


async def test_task_reports_child_failure(delegation_settings) -> None:  # type: ignore[no-untyped-def]
    settings = delegation_settings(
        subagents=[{"name": "helper", "description": "d", "prompt": "p", "max_steps": 2}]
    )

    @tool
    def search(q: str) -> str:
        """搜索。"""
        return q

    endless = ScriptedChatModel(
        steps=[
            tool_call("search", {"q": "1"}, call_id="c1"),
            tool_call("search", {"q": "2"}, call_id="c2"),
        ]
    )
    task = delegation.build_tools(settings, available_tools=[search], model=endless)[0]

    result = await task.ainvoke({"description": "永远查不完"})

    assert "未能完成" in result


async def test_task_unknown_subagent_lists_available(delegation_settings) -> None:  # type: ignore[no-untyped-def]
    @tool
    def search(q: str) -> str:
        """搜索。"""
        return q

    task = delegation.build_tools(
        delegation_settings(), available_tools=[search], model=ScriptedChatModel(steps=[])
    )[0]

    result = await task.ainvoke({"description": "x", "subagent_type": "nope"})

    assert "没有名为" in result
    assert "helper" in result


async def test_task_passes_approval_hook_to_child(delegation_settings) -> None:  # type: ignore[no-untyped-def]
    """子 Agent 的副作用工具也要能问到人 —— 回调必须往下传。"""
    settings = delegation_settings(
        subagents=[{"name": "writer", "description": "d", "prompt": "p", "tools": ["write_file"]}]
    )
    asked: list[str] = []

    async def hook(request):  # type: ignore[no-untyped-def]
        asked.append(request.tool_name)
        return False  # 拒绝

    @tool
    def write_file(path: str) -> str:
        """写文件。"""
        return "written"

    from doudou.tools.decorators import annotate

    annotate(write_file, group="files", side_effects=True)

    child = ScriptedChatModel(
        steps=[tool_call("write_file", {"path": "a.md"}, call_id="c1"), AIMessage(content="好")]
    )
    task = delegation.build_tools(
        settings,
        available_tools=[write_file],
        model=child,
        approval_hook=hook,
        # 策略来自调用方：它知道 settings，也知道「谁是副作用工具」
        approval_policy=lambda tools: {item.name: True for item in tools},
    )[0]

    await task.ainvoke({"description": "写一个文件"})

    # 关键断言：子 Agent 的写工具**问到了人**，而且被拒后没有真的执行。
    assert asked == ["write_file"]
