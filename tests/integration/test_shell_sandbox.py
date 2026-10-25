"""真实 bubblewrap 的端到端验证（默认不跑：需要 Linux + 已安装 bwrap）。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from doudou.tools.builtin import shell

pytestmark = pytest.mark.integration

requires_bwrap = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bwrap") is None,
    reason="需要 Linux 且已安装 bubblewrap",
)


@requires_bwrap
async def test_sandbox_hides_sibling_files(settings_factory, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("top-secret", encoding="utf-8")

    settings = settings_factory(
        config_yaml=(
            f'filesystem:\n  root_dir: "{workspace.as_posix()}"\n'
            "tools:\n  shell:\n    enabled: true\n    sandbox: bwrap\n"
        )
    )
    tool = shell.build_tools(settings)[0]

    blocked = await tool.ainvoke({"command": "cat ../secret.txt"})
    assert "top-secret" not in blocked
    assert "退出码 0" not in blocked

    written = await tool.ainvoke({"command": "echo inside > note.txt && cat note.txt"})
    assert "inside" in written
    assert (workspace / "note.txt").read_text(encoding="utf-8").strip() == "inside"
