# 初始化本地开发环境：复制配置模板、创建数据目录、同步依赖。
# 用法： powershell -ExecutionPolicy Bypass -File scripts/dev.ps1
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

function Copy-IfMissing($src, $dst) {
    if (Test-Path $dst) {
        Write-Host "skip   $dst (已存在)"
    } else {
        Copy-Item $src $dst
        Write-Host "create $dst"
    }
}

Copy-IfMissing '.env.example' '.env'
Copy-IfMissing 'config/config.example.yaml' 'config/config.yaml'
Copy-IfMissing 'config/mcp.servers.example.yaml' 'config/mcp.servers.yaml'

New-Item -ItemType Directory -Force -Path 'data/workspace' | Out-Null

Write-Host "`n同步依赖 ..."
uv sync --all-extras

Write-Host "`n完成。下一步："
Write-Host "  1. 编辑 .env 填入模型与飞书凭证"
Write-Host "  2. uv run doudou config check"
Write-Host "  3. uv run doudou chat"
