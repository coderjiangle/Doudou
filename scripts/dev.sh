#!/usr/bin/env bash
# 初始化本地开发环境：同步依赖、创建 .env，并初始化 ~/doudou 默认目录。
# 用法： bash scripts/dev.sh
set -euo pipefail
cd "$(dirname "$0")/.."

copy_if_missing() {
    if [ -e "$2" ]; then
        echo "skip   $2 (已存在)"
    else
        cp "$1" "$2"
        echo "create $2"
    fi
}

# 当前目录的 .env 优先级高于 ~/doudou/.env，便于仓库内开发时使用独立密钥。
copy_if_missing .env.example .env

printf '\n同步依赖 ...\n'
uv sync --all-extras

printf '\n初始化 ~/doudou ...\n'
uv run doudou init

printf '\n完成。下一步：\n'
printf '  1. 编辑 .env 或 ~/doudou/.env 填入模型与飞书凭证\n'
printf '  2. uv run doudou config check\n'
printf '  3. uv run doudou chat\n'
