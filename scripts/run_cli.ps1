# 启动终端交互式对话（不依赖飞书等外部平台）。
# 用法： powershell -ExecutionPolicy Bypass -File scripts/run_cli.ps1
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
uv run doudou chat
