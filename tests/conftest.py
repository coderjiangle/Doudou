"""Shared fixtures: settings, fake model, temp config dirs.

所有单元测试都必须**离线可跑**：模型一律用 ``doudou.agent.testing`` 里的确定性模型，
配置一律落在 ``tmp_path``，不读仓库里的 ``.env``。
"""

from __future__ import annotations

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
