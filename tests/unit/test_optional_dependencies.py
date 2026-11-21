"""Optional-dependency gating for Feishu / voice / browser extras."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from doudou.channels import registry
from doudou.channels.voice.deps import ensure_voice_dependencies
from doudou.channels.voice.errors import VoiceDependencyError
from doudou.config.loader import Settings
from doudou.utils.optional import MissingDependencyError, require_modules


def test_require_modules_reports_install_hint() -> None:
    with pytest.raises(MissingDependencyError, match=r"doudou-agent\[voice\]") as exc_info:
        require_modules(
            feature="语音功能",
            modules=("doudou_missing_dependency_for_test",),
            extra="voice",
        )

    assert "doudou_missing_dependency_for_test" in str(exc_info.value)
    assert "uv sync --extra voice" in str(exc_info.value)


def test_voice_dependency_error_is_a_voice_error() -> None:
    with pytest.raises(VoiceDependencyError, match=r"doudou-agent\[voice\]"):
        ensure_voice_dependencies(("doudou_missing_dependency_for_test",))


def test_feishu_channel_skipped_when_lark_missing(
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory(config_yaml="channels:\n  feishu:\n    enabled: true\n")
    monkeypatch.setattr(registry, "module_available", lambda _name: False)

    channels = registry.build_enabled_channels(settings, router=object())  # type: ignore[arg-type]

    assert channels == []
