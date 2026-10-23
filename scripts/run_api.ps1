# 启动 FastAPI 开发服务。
# 用法： powershell -ExecutionPolicy Bypass -File scripts/run_api.ps1
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
uv run uvicorn doudou.api.app:app --reload --host 127.0.0.1 --port 8000
