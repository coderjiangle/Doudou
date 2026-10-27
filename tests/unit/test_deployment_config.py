"""部署模板必须使用生效的配置名，并保持单进程状态一致性。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from dotenv.variables import parse_variables
from pydantic import ValidationError
from typer.testing import CliRunner

from doudou.cli.main import app
from doudou.config.env import CONFIG_ENV_PATHS, META_ENV, build_overrides
from doudou.config.loader import Settings
from doudou.config.schema import AppConfig

ROOT = Path(__file__).resolve().parents[2]


def deployment_env() -> dict[str, str]:
    deployment = next(
        yaml.safe_load_all((ROOT / "deploy/k8s/deployment.yaml").read_text(encoding="utf-8"))
    )
    values = {
        item["name"]: str(item["value"])
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    for document in yaml.safe_load_all(
        (ROOT / "deploy/k8s/configmap.yaml").read_text(encoding="utf-8")
    ):
        values.update(document.get("data", document.get("stringData", {})))
    return values


@pytest.mark.parametrize("template", ["k8s", "compose"])
def test_deployment_variables_are_recognized_and_effective(template: str) -> None:
    if template == "k8s":
        values = deployment_env()
    else:
        values = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))[
            "services"
        ]["api"]["environment"]
        # 默认环境下展开 Compose 的 ${VAR:-default}，再校验实际配置值。
        values = {
            name: "".join(atom.resolve({}) for atom in parse_variables(value))
            for name, value in values.items()
        }
    assert set(values) <= set(CONFIG_ENV_PATHS) | set(META_ENV)
    _meta, overrides = build_overrides(values)
    config = AppConfig.model_validate(overrides)
    assert config.runtime.env == "prod"
    assert config.runtime.debug is False
    assert config.server.host == "0.0.0.0"
    assert config.server.workers == 1
    assert config.filesystem.root_dir == "/data"
    assert config.storage.database_url == "sqlite+aiosqlite:////data/doudou.db"


def test_compose_database_url_can_be_overridden() -> None:
    template = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))[
        "services"
    ]["api"]["environment"]["DOUDOU_STORAGE_DATABASE_URL"]
    configured = "postgresql+asyncpg://doudou:example@postgres:5432/doudou"
    resolved = "".join(
        atom.resolve({"DOUDOU_STORAGE_DATABASE_URL": configured})
        for atom in parse_variables(template)
    )
    _meta, overrides = build_overrides({"DOUDOU_STORAGE_DATABASE_URL": resolved})
    assert AppConfig.model_validate(overrides).storage.database_url == configured


@pytest.mark.parametrize("value", [0, 2, 4])
def test_multiple_workers_are_rejected(value: int) -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"server": {"workers": value}})


def test_worker_count_from_environment_is_validated(
    settings_factory: Callable[..., Settings],
) -> None:
    assert settings_factory(env={"DOUDOU_SERVER_WORKERS": "1"}).app.server.workers == 1
    with pytest.raises(ValidationError):
        settings_factory(env={"DOUDOU_SERVER_WORKERS": "4"})


def test_cli_explicitly_selects_one_worker(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def run(_application: str, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("uvicorn.run", run)
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    result = CliRunner().invoke(app, ["serve", "--config-dir", str(settings.config_dir)])
    assert result.exit_code == 0, result.output
    assert calls and calls[0]["workers"] == 1
