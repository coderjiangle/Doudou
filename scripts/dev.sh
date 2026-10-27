#!/usr/bin/env bash
# 初始化本地开发环境：复制配置模板、创建数据目录、同步依赖。
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

copy_if_missing .env.example .env
copy_if_missing config/config.example.yaml config/config.yaml

mkdir -p data/workspace

printf '\n同步依赖 ...\n'
uv sync --all-extras

printf '\n完成。下一步：\n'
printf '  1. 编辑 .env 填入模型与飞书凭证\n'
printf '  2. uv run doudou config check\n'
printf '  3. uv run doudou chat\n'
