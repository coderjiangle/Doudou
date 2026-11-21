# Skill 编写指引

渐进式披露：系统提示词只放索引，正文由 `read_skill` 按需读取。

## 1. 什么是 Skill

Skill 是**按需加载的领域知识与工作流**，遵循 [Agent Skills 标准](https://agentskills.io/)。
一个 Skill 就是一个目录加一个 `SKILL.md`。

它和工具的区别：**工具提供动作，Skill 提供做法**。需要外部动作时，在 Skill 正文里
写明该调用哪个工具。

### 渐进式披露

```
启动时  ──▶ 只读 frontmatter（name + description），注入系统提示词的系统提示词索引
按需时  ──▶ 模型判断「这个任务需要它」→ 调 read_skill("xxx") 读全文
```

因此：

- **启动上下文只随「技能数量」增长，不随「技能体积」增长**；
- 技能正文可以写得很长、可以带附属文件，不会一直占着上下文；
- 谁来挑技能由模型判断，不需要人预先选好。

## 2. 目录结构

```
skills/
└─ my-skill/
   ├─ SKILL.md          # 必需：frontmatter + 正文
   ├─ reference.md      # 可选：详细参考（正文里让模型用 skill_read_file 读）
   ├─ templates/        # 可选：模板文件
   └─ scripts/          # 可选：脚本
```

一个 Skill 一个目录，目录名建议与 frontmatter 的 `name` 一致。

## 3. SKILL.md 格式

```markdown
---
name: my-skill          # 小写短横线命名，全局唯一
description: 做什么 + 什么时候用（这句决定它会不会被加载）
---

# 正文

## 何时使用
...

## 步骤
1. 用 `web_search` 检索最近 7 天的相关新闻。
2. 用 `write_file` 把结果写入 `/workspace/report.md`。
```

### frontmatter 字段

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `name` | ✅ | 小写短横线命名（`^[a-z0-9]+(-[a-z0-9]+)*$`），全局唯一 |
| `description` | ✅ | **决定模型何时加载它**，务必写清「做什么 + 什么时候用」 |
| 其它字段 | ❌ | 会被解析并出现在 `skills show` 里，但不要依赖 |

`description` 是 Skill 能否被正确触发的关键，建议句式：
「<能力>。当 <触发场景> 时使用。」并包含用户可能说的关键词。

> 名字不合法、缺 `description`、YAML 写坏 —— 这类问题**不会**让服务起不来，
> 但会被记下来并出现在 `doudou skills check` 与 `GET /skills` 的 `errors` 里。
> **必须去看**：静默跳过会让你以为技能已生效。

## 4. 模型能用的三个工具

Skill 数量为 0 时**这三个工具都不注册** —— 不给模型一个永远失败的工具。

| 工具 | 作用 |
| --- | --- |
| `read_skill(name)` | 读某个技能的完整正文 |
| `list_skill_files(name)` | 列出该技能目录下的附属文件 |
| `skill_read_file(name, path)` | 读附属文件（如 `templates/report.md`） |

协议安排：提示词里明确写了「**与当前任务无关的技能不要读**」——
否则模型会出于好奇把所有技能都拉一遍，渐进式披露就白做了。

## 5. 配置

```yaml
agent:
  skills:
    - ../skills/            # 仓库根 skills 目录，可多行
  skill_index_limit: 40     # 提示词里最多列几个技能
```

`skill_index_limit` 存在的理由：索引必须**有界**。技能一多，光是索引就能把
提示词顶坏。超出时会列出前 N 个并说明「另有 X 个未列出」。

## 6. 与记忆（默认 `~/doudou/config/AGENTS.md`）的分工

| | Skill | 记忆 `~/doudou/config/AGENTS.md` |
| --- | --- | --- |
| 加载时机 | 按需（模型判断） | 每轮都注入 |
| 内容 | 特定领域的流程与方法 | 长期偏好与事实 |
| 体量 | 可以较大，含附属文件 | 要精简 |

「我喜欢用中文回复」→ 记忆；「写调研报告的标准流程」→ Skill。

> `memory_files` 可以列多个文件，但**只有第一个**是 `remember` 的回写目标。
> 默认初始化把助手记忆放在 `~/doudou/config/AGENTS.md`；本仓库开发配置也可指向
> `config/AGENTS.md`。仓库根的 `AGENTS.md` 是给编码工具的项目约定，不注入助手提示词。

## 7. 调试

```bash
uv run doudou skills list              # 索引 + 正文大小 + 附件数
uv run doudou skills show research     # 详情
uv run doudou skills show research --body   # 连正文一起看
uv run doudou skills check             # 只校验，有错则非零退出
uv run doudou skills reload            # 重新扫描
```

HTTP 侧：

```
GET  /api/v1/skills                   # 索引（不含正文）
GET  /api/v1/skills/{name}            # 详情；include_body=true 才带正文
POST /api/v1/skills/reload            # 重新扫描
```

## 8. 热重载：这次是真的

`POST /skills/reload` 会**就地更新系统提示词**，无需重启。

为什么 MCP 的 `/mcp/refresh` 做不到而这里可以：技能索引只是提示词里的一段文本，
而 `read_skill` 在调用时才查索引 —— 换掉提示词 + 刷新索引就真的生效了。
MCP 的工具列表是构建 Agent 时固定的，那个必须重启。

触发不灵时先看 `description` 是不是写得太笼统，导致模型不认为该加载。
