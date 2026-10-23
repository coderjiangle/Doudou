# scripts/

开发与运维辅助脚本。**保持薄**：只做进程启动、环境准备等编排，业务逻辑一律放 `src/doudou/`。

| 脚本 | 用途 |
| --- | --- |
| `dev.ps1` | 一键准备本地环境（建 .env、config、安装依赖） |
| `run_api.ps1` | 启动 FastAPI（开发模式） |
| `run_cli.ps1` | 启动终端对话 |
| `run_feishu_tunnel.ps1` | 用内网穿透把飞书回调暴露到公网 |

也可以直接用 Makefile 的等价目标：`make install` / `make serve` / `make chat`。

> 约定：脚本内不硬编码密钥；统一从 `.env` 读取。
