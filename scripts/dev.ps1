# 初始化本地开发环境：同步依赖、创建 .env，并初始化 ~/doudou 默认目录。
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

# 当前目录的 .env 优先级高于 ~/doudou/.env，便于仓库内开发时使用独立密钥。
Copy-IfMissing '.env.example' '.env'

Write-Host "`n同步依赖 ..."
uv sync --all-extras

Write-Host "`n初始化 ~/doudou ..."
uv run doudou init

Write-Host "`n完成。下一步："
Write-Host "  1. 编辑 .env 或 ~/doudou/.env 填入模型与飞书凭证"
Write-Host "  2. uv run doudou config check"
Write-Host "  3. uv run doudou chat"
