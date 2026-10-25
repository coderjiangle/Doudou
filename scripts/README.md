# scripts/

开发与运维辅助脚本。**保持薄**：只做进程启动、环境准备等编排，业务逻辑一律放 `src/doudou/`。

| 用途 | PowerShell（Windows） | Bash（Linux / macOS） |
| --- | --- | --- |
| 准备本地环境（建 .env、config、安装依赖） | `dev.ps1` | `dev.sh` |
| 启动 FastAPI（开发模式） | `run_api.ps1` | `run_api.sh` |
| 启动终端对话 | `run_cli.ps1` | `run_cli.sh` |

用法：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1
```

```bash
bash scripts/dev.sh
```

也可以直接用 Makefile 的等价目标：`make install` / `make serve` / `make chat`
（`make` 需要 POSIX shell；Windows 上请用 Git Bash / WSL，或直接用上面的 `.ps1`）。

> 约定：脚本内不硬编码密钥；统一从 `.env` 读取。
