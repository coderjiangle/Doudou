#!/usr/bin/env bash
# 启动 FastAPI 开发服务。
# 用法： bash scripts/run_api.sh
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run uvicorn doudou.api.app:app --reload --host 127.0.0.1 --port 8000
