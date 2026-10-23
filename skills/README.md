# skills/

Agent Skills 目录（[Agent Skills 标准](https://agentskills.io/)）。
每个子目录是一个 Skill，必须包含 `SKILL.md`。

```
skills/
├─ example-echo/     # 最小结构示例
│  └─ SKILL.md
├─ research/         # 调研工作流（被 config/agents.yaml 的 researcher 子 Agent 引用）
│  └─ SKILL.md
└─ <your-skill>/
   ├─ SKILL.md
   ├─ reference.md   # 可选
   └─ templates/     # 可选
```

编写规范见 [`../docs/skills.md`](../docs/skills.md)。

要点速记：

- frontmatter 的 `description` 决定 Skill **何时被加载**，要写清「做什么 + 什么时候用」。
- Skill 只提供「做法」，需要动作时在正文里指明调用哪个工具。
- 在 `config/config.yaml` 的 `agent.skills` 中声明根目录（默认已指向本目录）后自动生效。
