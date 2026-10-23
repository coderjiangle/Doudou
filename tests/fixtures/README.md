# tests/fixtures/

测试夹具。

**离线模型不放在这里**，而是放在 `src/doudou/agent/testing.py`：
`ScriptedChatModel` 同时被单元测试、集成测试、CI 和 CLI 的 `--model scripted:demo`
使用，放在源码里才能共享（也便于在没有 API Key 的环境里做端到端自检）。

本目录留给需要文件形态的夹具：

- `settings/` 各环境的最小可用配置（用于配置加载测试）
- `responses/` 外部接口的录制响应（模型、飞书、MCP）
- `mcp/` 用于本地 stdio MCP 集成测试的样例 server

约定：夹具内**不得**包含真实密钥，所有外部凭据一律用占位值。
