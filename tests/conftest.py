"""Shared fixtures: settings, fake model, temp config dirs.

所有单元测试都必须**离线可跑**：模型一律用 ``doudou.agent.testing`` 里的确定性模型，
配置一律落在 ``tmp_path``，不读仓库里的 ``.env``。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from doudou.config.loader import Settings, load_settings

#: 测试用最小配置：声明一个 Skill 根目录、打开写入类工具的人工确认。
MINIMAL_CONFIG = """\
version: 1
runtime:
  env: local
  log_level: WARNING
  timezone: Asia/Shanghai
agent:
  name: TestDoudou
  system_prompt_file: prompts/system.md
  skills: ["../skills/"]
  max_steps: 4
  interrupt_on:
    add: true
"""

#: 测试用 SKILL.md（``skills_settings`` 夹具会写进临时目录）。
DEMO_SKILL = """\
---
name: demo-skill
description: 演示技能。当用户要求演示技能机制时使用。
---

# 演示技能

## 步骤

1. 说明技能是按需加载的。
2. 引用附表 `reference.md` 里的细节。
"""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """一个可用的配置目录：含 config.yaml 与系统提示词。"""
    root = tmp_path / "config"
    (root / "prompts").mkdir(parents=True)
    (root / "prompts" / "system.md").write_text("你是测试助手。", encoding="utf-8")
    (root / "config.yaml").write_text(MINIMAL_CONFIG, encoding="utf-8")
    return root


@pytest.fixture
def missing_env_file(tmp_path: Path) -> Path:
    """一个不存在的 .env 路径，用于隔离宿主机环境。"""
    return tmp_path / "no-such.env"


@pytest.fixture
def settings(config_dir: Path, missing_env_file: Path) -> Settings:
    """加载 ``config_dir`` 的配置，且不读环境变量。"""
    return load_settings(config_dir=config_dir, env_file=missing_env_file, use_env=False)


@pytest.fixture
def tools_settings(
    tmp_path: Path, settings_factory: Callable[..., Settings]
) -> Callable[..., Settings]:
    """构造带临时工作区的配置。

    ``filesystem.root_dir`` 必须指向 ``tmp_path``，否则工具会去读写仓库里的
    ``data/``，测试之间互相污染。
    """

    def make(
        *,
        allowed_domains: tuple[str, ...] = ("example.com",),
        max_response_chars: int | None = None,
        **kwargs: object,
    ) -> Settings:
        workspace = (tmp_path / "workspace").as_posix()
        limit_line = f"    max_response_chars: {max_response_chars}\n" if max_response_chars else ""
        config_yaml = (
            f'filesystem:\n  root_dir: "{workspace}"\n'
            f"tools:\n"
            f"  http:\n"
            f"    allowed_domains: {json.dumps(list(allowed_domains))}\n"
            f"{limit_line}"
        )
        return settings_factory(config_yaml=config_yaml, **kwargs)  # type: ignore[arg-type]

    return make


@pytest.fixture
def skills_settings(
    tmp_path: Path, settings_factory: Callable[..., Settings]
) -> Callable[..., Settings]:
    """构造带**真实 Skill 目录**的配置。

    conftest 的默认配置把 ``agent.skills`` 指向 ``../skills/``（相对 tmp 配置目录），
    那里不存在，因此默认是「没有技能」。要测技能必须自己铺目录。
    """

    def make(
        *, skills: dict[str, str] | None = None, extra_files: dict[str, str] | None = None
    ) -> Settings:
        root = tmp_path / "skills"
        payload = skills if skills is not None else {"demo-skill": DEMO_SKILL}
        for name, content in payload.items():
            directory = root / name
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "SKILL.md").write_text(content, encoding="utf-8")
        for relative, content in (extra_files or {}).items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        config_yaml = f'agent:\n  skills: ["{root.as_posix()}"]\n'
        return settings_factory(config_yaml=config_yaml)

    return make


@pytest.fixture
def settings_factory(
    tmp_path: Path, missing_env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., Settings]:
    """按需构造配置：``make(config_yaml=..., local_yaml=..., env={...})``。"""

    def make(
        *,
        config_yaml: str | None = None,
        local_yaml: str | None = None,
        env: dict[str, str] | None = None,
        use_env: bool = True,
        env_file: Path | None = None,
    ) -> Settings:
        root = (
            tmp_path
            / f"cfg-{abs(hash((config_yaml, local_yaml, str(sorted((env or {}).items())))))}"
        )
        root.mkdir(exist_ok=True)
        if config_yaml is not None:
            (root / "config.yaml").write_text(config_yaml, encoding="utf-8")
        if local_yaml is not None:
            (root / "config.local.yaml").write_text(local_yaml, encoding="utf-8")
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        return load_settings(
            config_dir=root,
            env_file=env_file if env_file is not None else missing_env_file,
            use_env=use_env,
        )

    return make
