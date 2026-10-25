#!/usr/bin/env bash
# 启动终端交互式对话（不依赖飞书等外部平台）。
# 用法： bash scripts/run_cli.sh
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run doudou chat
