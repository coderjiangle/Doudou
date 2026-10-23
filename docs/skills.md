# Skill 编写指引

## 1. 什么是 Skill

Skill 是**按需加载的领域知识与工作流**，遵循 [Agent Skills 标准](https://agentskills.io/)。
在 deepagents 里，`skills=` 会生成一个 `SkillsMiddleware`：

- 启动时只读取每个 `SKILL.md` 的 **frontmatter**（`name` + `description`），进入系统提示词，开销很小；
- 模型判断当前任务需要某技能时，才去读该 Skill 的完整正文与附属文件 —— 即**渐进式披露**。

它和工具的区别：**工具提供动作，Skill 提供做法**。需要外部动作时，Skill 里写明该调用哪个工具。

## 2. 目录结构

```
skills/
└─ example-echo/
   ├─ SKILL.md          # 必需：frontmatter + 正文
   ├─ reference.md      # 可选：详细参考（正文里让它按需 read_file）
   ├─ templates/        # 可选：模板文件
   └─ scripts/          # 可选：脚本
```

一个 Skill 一个目录，目录名建议与 frontmatter 的 `name` 一致。

## 3. SKILL.md 格式

```markdown
---
name: example-echo
description: 示例技能，演示 Skill 的最小结构与编写规范。当用户要求演示技能机制时使用。
---

# 正文

## 何时使用
...

## 步骤
1. ...
2. ...

## 注意
...
```

### frontmatter 字段

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `name` | ✅ | 小写短横线命名，全局唯一 |
| `description` | ✅ | **决定模型何时加载它**，务必写清「做什么 + 什么时候用」 |
| 其他自定义字段 | ❌ | 会被解析但不要依赖，避免与标准冲突 |

`description` 是 Skill 能否被正确触发的关键，建议句式：
「<能力>。当 <触发场景> 时使用。」并包含用户可能说的关键词。

## 4. 配置

```yaml
# config/config.yaml
agent:
  skills:
    - ../skills/            # 仓库根 skills 目录，可多行
```

子 Agent 可以有自己独立的 Skill 集（`config/agents.yaml` 的 `subagents[].skills`），
此时只有该子 Agent 能看到这些 Skill。

## 5. 和工具的配合

Skill 本身不执行动作。需要动作时，在正文里指明使用的工具：

```markdown
## 步骤
1. 用 `web_search` 检索最近 7 天的相关新闻。
2. 用 `write_file` 把结果写入 `/workspace/report.md`。
3. 用 `http_request` 把文件内容提交到内部系统。
```

也可以给 Skill 绑定专属工具（`SkillsMiddleware(tools=[...])`），
即在 `agent/middleware.py` 的角色化 Skill 配置里声明。

## 6. 与记忆（AGENTS.md）的分工

| | Skill | 记忆 `AGENTS.md` |
| --- | --- | --- |
| 加载时机 | 按需（模型判断） | 每次启动都加载 |
| 内容 | 特定领域的流程与方法 | 长期偏好与事实 |
| 体量 | 可以较大，含附属文件 | 要精简 |

「我喜欢用中文回复」→ 记忆；「写调研报告的标准流程」→ Skill。

## 7. 调试

```powershell
uv run doudou skills list     # 列出发现的 Skill（已可用）
```

计划在 M6 补上 `doudou skills show <name>`（打印 frontmatter 与正文）、
`doudou skills check`（仅校验）与 `POST /api/v1/skills/reload` 热重载。

触发不灵时通常是 `description` 写得不够具体，导致模型不认为该加载。

> **注意**：当前（M1）只实现了 Skill 的**发现与校验**。
> 交给模型的渐进式加载在 **M6** 落地，做法是「把索引注入系统提示词 + 提供 `read_skill` 工具」，
> **不依赖 deepagents**（见 docs/roadmap.md M6）。
