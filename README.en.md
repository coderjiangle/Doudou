# Doudou

[中文](README.md) | English

A personal assistant agent with a **hand-rolled core** (the ReAct loop in `doudou.agent.react`).

It supports custom **Tools**, **MCP** (Model Context Protocol) and **Skills** (the Agent Skills
standard), talks to you over a **Feishu (Lark) channel**, and exposes REST / SSE endpoints through
**FastAPI** when you need a network service.

> Doudou is named after my adorable Chinese rural dog (Zhonghua Tianyuan). I hope this assistant
> keeps you company the way Doudou keeps me company.

<p align="center">
  <img src="docs/assets/doudou.png" alt="Doudou, the cartoon dog" width="240">
</p>

> No API key needed to exercise the full pipeline:
> `uv run doudou ask "What time is it now?" --model scripted:demo`
>
> Windows / Linux / macOS are all supported; platform-specific bootstrap scripts and command
> examples are in §4.

---

## 1. Core capabilities

| Capability | How it works | Module |
| --- | --- | --- |
| ReAct loop | Built in-house: reason → call tools → observe → converge | `doudou.agent.react` |
| Tool calling | LangChain `@tool` + registry assembly | `doudou.tools` |
| Multi-turn sessions | Conversation→thread mapping + in-memory history | `doudou.gateway` |
| CLI channel | rich-rendered streaming output and tool logs | `doudou.channels.cli_channel` |
| Human in the loop | `interrupt_on` + terminal confirmation | `doudou.agent.react` |
| HTTP API | FastAPI + SSE + persistence + Bearer auth | `doudou.api` |
| Session persistence | Messages / threads / channel bindings stored; resume after restart | `doudou.storage` |
| Built-in toolset | Search / fetch / workspace files / shell, deny by default | `doudou.tools.builtin` |
| Config self-check | The `my` tool answers model, limits, tool-list and key questions on demand (read-only) | `doudou.tools.builtin.self_config` |
| Feishu channel | WebSocket events + streaming cards + shared context per group | `doudou.channels.feishu` |
| Message bus | Serial within a conversation, concurrent across conversations, rejects backlog | `doudou.gateway.bus` |
| MCP tools | stdio / http / sse, narrowed tool surface + graceful degradation | `doudou.mcp` |
| Progressive skill loading | Index injected + `read_skill` on demand + hot reload | `doudou.skills` |
| Context engineering | Token budget, summarization, large-result offload, long-term memory | `doudou.agent.context` |
| Permissions & subagents | Path permissions + subagent delegation + approvals across restarts | `doudou.agent.permissions` |
| Approval broker | Timeout means deny, decision reuse, persisted audit | `doudou.agent.approvals` |
| Audit log | Every tool call recorded (arguments redacted + truncated) | `doudou.observability.audit` |
| Cost accounting | Tokens / spend per day × conversation, daily budget alerts | `doudou.observability.cost` |
| Tracing | Langfuse (optional, degrades when keys are missing) | `doudou.observability.tracing` |

## 1.1 Run it right now (no API key required)

```bash
uv run doudou config check                                       # validate configuration
uv run doudou tools list                                         # list loaded tools
uv run doudou ask "What time is it now?" --model scripted:demo   # one turn with a tool call
uv run doudou chat --model scripted:demo                         # interactive multi-turn
```

`scripted:demo` is a built-in deterministic offline model (`doudou.agent.testing`): it decides
whether to call the `now` tool based on keywords, so **the whole ReAct loop (reason → call tool →
observe → converge) can be verified without any network access**. To hook up a real model, just set
`DOUDOU_MODEL_PRIMARY` and the matching API key in `.env`.

## 1.2 In action (Feishu)

A normal conversation is answered with a single card, and the tool calls stay visible on that card.
Operations with side effects pop a separate confirmation card — nothing runs until you hit
"Approve". Behind that button is the `ApprovalBroker`: a timeout is treated as a denial.

<p align="center">
  <img src="image/飞书对话1.png" alt="Feishu conversation: listing loaded tools" width="820">
</p>

<p align="center">
  <img src="image/飞书对话2.png" alt="Feishu conversation: human approval before running a command" width="820">
</p>

## 2. Architecture at a glance

![Doudou architecture](docs/assets/architecture.png)

> Editable vector source: [`docs/assets/architecture.svg`](docs/assets/architecture.svg).

Design principles:

- **Channels are decoupled from the agent**: a channel only receives and sends messages; all
  orchestration lives in `gateway`.
- **Capabilities are pluggable**: tools, MCP servers and skills are declared in configuration, never
  hard-coded.
- **Configuration-driven**: `config/*.yaml` describes the agent, subagents, MCP servers and
  channels; environment variables and secrets live in `.env`.
- **One agent, many channels**: Feishu, webhook, CLI and the HTTP API share a single agent instance.

## 3. Directory layout

```
Doudou/
├─ config/                    # Declarative config (committed; secrets go in .env)
│  ├─ config.example.yaml     # Main config template
│  ├─ mcp.servers.example.yaml# MCP server list template
│  └─ agents.yaml             # Main agent prompt + subagent definitions
├─ docs/                      # Design documents
├─ skills/                    # Agent Skills (one SKILL.md per subdirectory)
├─ deploy/                    # Deployment assets (Docker / K8s)
├─ scripts/                   # Developer scripts
├─ src/doudou/
│  ├─ cli/                    # Command-line entry points
│  ├─ config/                 # Config loading and validation
│  ├─ agent/                  # ReAct loop, prompts, context engineering, permissions, approvals, subagents
│  ├─ tools/                  # Tool registry and built-in tools
│  ├─ mcp/                    # MCP client and lifecycle
│  ├─ skills/                 # Skill discovery and indexing
│  ├─ channels/               # Channel implementations (feishu / webhook / cli)
│  ├─ gateway/                # Session routing, message bus, runner
│  ├─ api/                    # FastAPI app and routes
│  ├─ storage/                # Persistence (typing / messages / channel bindings)
│  ├─ observability/          # Logging, tracing, metrics
│  ├─ security/               # Auth and secret handling
│  └─ utils/                  # Shared utilities
└─ tests/                     # Unit + integration tests
```

See [`docs/architecture.md`](docs/architecture.md) for what each directory is responsible for.

## 4. Quick start

### 4.1 Zero-config first run (offline, no API key)

```bash
uv sync

uv run doudou tools list                                  # loaded tools
uv run doudou ask "What time is it now?" --model scripted:demo   # one turn with a tool call
uv run doudou chat --model scripted:demo                  # interactive multi-turn
```

`scripted:demo` is the built-in deterministic offline model; it decides whether to call the `now`
tool based on keywords, so the full ReAct loop (reason → call tool → observe → converge) can be
verified without network access.

### 4.2 Connecting a real model

Start with the configuration. There is a one-shot script per platform; both do the same thing (copy
templates, create directories, sync dependencies):

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1
```

```bash
# Linux / macOS
bash scripts/dev.sh
```

The equivalent manual steps: copy `.env.example` → `.env` and `config/config.example.yaml` →
`config/config.yaml`; copy `config/mcp.servers.example.yaml` → `config/mcp.servers.yaml` if you need
MCP.

Then set `DOUDOU_MODEL_PRIMARY` and the matching API key in `.env`, for example:

```dotenv
DOUDOU_MODEL_PRIMARY=openai:gpt-5.5
DOUDOU_MODEL_API_KEY=sk-...
```

Every environment variable is `DOUDOU_` plus the `config.yaml` path; the full rule is in
[`docs/configuration.md`](docs/configuration.md) §1.

```bash
uv run doudou config check          # validate config and credentials, list actionable problems
uv run doudou chat
```

Non-OpenAI vendors need their integration package: `uv sync --extra anthropic` / `--extra deepseek`.

**Gateways that speak the OpenAI protocol** (self-hosted services, aggregators) all use the
`openai:` prefix plus a `base_url`, so no extra provider package is required:

```yaml
model:
  primary: openai:<model-name-on-the-gateway>
  base_url: https://<gateway-domain>/v1
```

See "Connecting an OpenAI-compatible gateway" in [`docs/configuration.md`](docs/configuration.md).

### 4.3 Enabling web access (optional)

With the default configuration, **web tools are not registered**: a tool without credentials only
eats prompt budget and tempts the model into pointless attempts. Add the keys to turn them on:

```dotenv
# Web search: the provider picks the service, the key always lives at this one config path
#   tavily   → get one from the Tavily console
#   deepseek → DeepSeek's own search (billed per call)
DOUDOU_TOOLS_WEB_SEARCH_PROVIDER=deepseek    # tavily | deepseek (case-insensitive)
DOUDOU_TOOLS_WEB_SEARCH_API_KEY=sk-...
```

```yaml
# config/config.yaml —— domain allow-list for outbound fetching
tools:
  http:
    allowed_domains:
      - docs.python.org      # includes any subdomain
      - "*.github.com"       # subdomains only
```

| Tool | When it is enabled |
| --- | --- |
| `web_search` | `DOUDOU_TOOLS_WEB_SEARCH_API_KEY` is non-empty (the provider only chooses the service) |
| `http_request` | `tools.http.allowed_domains` is non-empty **and** the target domain matches (resolution to a private address is still refused) |

Once configured, one question in Feishu goes through search, and the card shows the `web_search`
call trace along with where each result came from:

<p align="center">
  <img src="image/飞书对话3.png" alt="Feishu conversation: searching today's news" width="820">
</p>

```bash
uv run doudou tools list          # which tools loaded, which groups are off and why
uv run doudou config check        # one-shot check of config, credentials, tools and skills
```

### 4.4 HTTP service

```bash
uv run doudou serve                 # 127.0.0.1:8000 by default, API prefix /api/v1
uv run doudou serve --port 9000 --reload
```

Endpoints:

| Endpoint | Description |
| --- | --- |
| `POST /api/v1/chat` | Single-turn chat, returns the full answer |
| `POST /api/v1/chat/stream` | SSE stream: `start → (delta\|tool_call\|tool_result)* → done` |
| `GET /api/v1/threads` / `GET /api/v1/threads/{id}` | Conversation list / details (full history) |
| `GET /api/v1/threads/{id}/context` | Context usage, budget and compression details (for debugging) |
| `DELETE /api/v1/threads/{id}` | Delete a conversation |
| `GET /api/v1/tools` | Tool list |
| `GET /api/v1/health` / `/health/ready` | Health / readiness probes (**no auth required**) |
| `GET /` | Built-in chat page (conversation list + streaming chat) |
| `GET /docs` | OpenAPI interactive docs |

> Opening `http://127.0.0.1:8000/` in a browser gives you the built-in chat page: past conversations
> on the left, the current model and tool count at the top, replies streamed from `/chat/stream` and
> rendered as Markdown (headings, lists, tables, code blocks). Tool calls show up as chips with
> collapsed results. Tools that need approval (file writes, `shell_exec`) raise an
> "Approve / Reject" card above the input box. `?thread=<id>` opens a specific conversation directly.
> With `DOUDOU_API_TOKEN` enabled, click "Settings" in the top-right and paste the token; the page
> relies on no external CDN.

Conversation history is persisted by default (`storage.history: database`), so **the same thread can
be resumed after a process restart**. Once `DOUDOU_API_TOKEN` is set, every endpoint except the
health probes requires `Authorization: Bearer <token>`.

```powershell
# In Windows PowerShell you must write curl.exe: curl is an alias for Invoke-WebRequest, which has no -X/-d.
curl.exe -X POST http://127.0.0.1:8000/api/v1/chat -H "Content-Type: application/json" `
  -d '{"message":"What time is it now?"}'
```

```bash
curl -X POST http://127.0.0.1:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"What time is it now?"}'
```

### 4.5 Connecting Feishu (optional)

The Feishu channel uses a **WebSocket long connection**: no public address, no tunneling. For the
result, see §1.2.

```dotenv
# .env
DOUDOU_CHANNELS_FEISHU_ENABLED=true
DOUDOU_CHANNELS_FEISHU_APP_ID=cli_xxxxxxxx
DOUDOU_CHANNELS_FEISHU_APP_SECRET=xxxxxxxx
```

In the Feishu open platform, set event subscription to "**Receive events via long connection**" and
subscribe to `im.message.receive_v1`, then run `uv run doudou serve` — the long connection is hosted
by the service itself.

⚠️ **The message permission scopes must be correct**: private chats require
`im:message.p2p_msg:readonly`, and group @-mentions require `im:message.group_at_msg:readonly`. With
only `im:message`, the long connection is established normally and the console reports a successful
"verify long connection" — yet **not a single message arrives** (Feishu filters pushes by
permission). Re-publish the version after changing permissions or events. For the debugging order,
see §2.11 of [`docs/channels.md`](docs/channels.md).

Session isolation defaults to "**one shared context per group**" (`isolate_by_thread: false`), so the
assistant in a group behaves like a colleague. Full steps, @-mention detection, streaming cards and
known limitations: [`docs/channels.md`](docs/channels.md).

### 4.6 Connecting MCP (optional)

MCP turns the tool source from "hard-coded in the code" into "declared in configuration":

```bash
cp config/mcp.servers.example.yaml config/mcp.servers.yaml   # Copy-Item on Windows
uv run doudou mcp list          # what each server actually exposed
uv run doudou mcp check github  # probe a single server
uv run doudou tools list        # tools the agent actually loaded (including MCP)
```

The manifest supports `${ENV}` placeholders (keeping secrets in `.env`), `include_tools` /
`exclude_tools` to narrow the tool surface, and a `read_only` declaration to skip confirmation.
Three transports: `stdio` / `http` / `sse`.

> ⚠️ MCP tools from the adapter are **stateless** — **every call creates a new session**.
> For `stdio` that means spawning a child process per call; `npx`/`uvx`-based servers take about a
> second each time. Prefer `http` for frequently used servers. See §4 of
> [`docs/mcp.md`](docs/mcp.md).

### 4.7 Writing a skill (optional)

Drop a skill with frontmatter into `skills/<name>/SKILL.md` to extend the agent — no code changes
needed:

```markdown
---
name: my-skill
description: What it does + when to use it (this line decides whether it gets loaded)
---

# Body: steps, caveats, which tools to call
```

**Only the index (name + description) enters the system prompt**; the body is read by the model via
`read_skill` when it decides it is needed — so a skill can be long without permanently occupying
context budget.

```bash
uv run doudou skills list                 # index + body size + attachment count
uv run doudou skills show research --body # details and body
uv run doudou skills check                # validate (non-zero exit on errors)
uv run doudou skills reload               # rescan (the HTTP side hot-reloads the prompt)
```

See [`docs/skills.md`](docs/skills.md).

### 4.8 Making it remember you (long-term memory)

The files listed in `agent.memory_files` are injected into the system prompt **on every turn**:

```yaml
agent:
  memory_files:
    - AGENTS.md          # default; point it at your own preferences file if you prefer
```

The assistant can also write to it itself — say "remember that I dislike huge pasted source dumps"
and it calls the `remember` tool to append to that file, **refreshing the prompt immediately** (a
write has side effects, so it asks for approval by default).

### 4.9 Long conversations and the context budget

Three knobs in the `context` section decide "how big the window is":

```yaml
context:
  max_input_tokens: 80000        # token budget for history sent to the model (the single most important number)
  summarize_threshold_ratio: 0.7 # compress once usage exceeds this share of the budget
  offload_threshold_bytes: 20000 # a single tool result above this is written to disk; only a preview + path remain
```

The default 80000 is deliberately conservative; **raise it for large-window models** (300000, say).
Compression **never deletes the original text** — the full history stays available in the database:

```bash
curl http://127.0.0.1:8000/api/v1/threads/{thread_id}/context
```

### 4.10 Other entry points

```bash
uv run doudou ask "question" --quiet     # only the final answer, handy for scripts
```

Feishu setup: [`docs/channels.md`](docs/channels.md); MCP: [`docs/mcp.md`](docs/mcp.md); skill
authoring: [`docs/skills.md`](docs/skills.md).

## 5. Common commands

```bash
uv run doudou serve                        # start the HTTP service
uv run doudou chat                         # interactive terminal chat
uv run doudou ask "question"               # single-turn chat
uv run doudou ask "question" --quiet       # single turn, answer only
uv run doudou tools list [--args]          # list tools
uv run doudou tools show now               # inspect one tool
uv run doudou skills list                  # list discovered skills
uv run doudou config show [--yaml]         # print the effective config (redacted)
uv run doudou config check                 # validate config / credentials / tools / skills
uv run doudou config path                  # print the resolved value of each path
uv run doudou version
make lint / test / fmt                     # see the Makefile
```

## 6. Documentation index

| Document | Contents |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | Layered architecture, module responsibilities, data flow |
| [docs/configuration.md](docs/configuration.md) | Configuration options, precedence, environment variable list |
| [docs/channels.md](docs/channels.md) | Feishu channel setup and sequences |
| [docs/mcp.md](docs/mcp.md) | MCP server configuration and tool assembly |
| [docs/skills.md](docs/skills.md) | Skill directory conventions and authoring guide |
| [docs/tools.md](docs/tools.md) | Custom tool development conventions |
| [docs/security.md](docs/security.md) | Auth, secrets, permissions and audit |
| [CHANGELOG.md](CHANGELOG.md) | Changes, by date |

> The design documents under `docs/` are currently written in Chinese.

## 7. License

[MIT](LICENSE) © 2026 Jiang Le
