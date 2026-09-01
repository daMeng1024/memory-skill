---
name: memory
description: 用本机记忆库（mem CLI）检索用户偏好、项目约束、领域知识、踩坑、SOP 和历史会话，以及沉淀、归档、对账和维护这个库；没装时引导用户完成安装与模型/路径配置。用户提到“记忆库”“之前”“上次”“以前”“记得吗”“本机记忆方案”，要求记住或沉淀已确认结论、处理待审候选与积压队列，或要装/配这套记忆库时使用。
---

# 本地记忆库

入口是 `mem`（装好后软链在 `~/.local/bin`），代码在本 skill 目录，数据在 `$MEM_HOME`。
它是一个检索层，不是当前代码、数据库、日志或外部系统的事实替代品。

## 第零步：确认装没装

`mem doctor` 的第一段会打印代码根、数据根和配置路径。出现"记忆库还没初始化"
或者 `mem` 根本不存在，就是没装——**这时不要去猜路径、不要手写 config.json**，
走引导：

1. 问用户四件事（都有默认值，用户说"随便"就用默认）：
   - 数据根放哪（默认 `~/.agent-memory`）
   - 语料主要是什么语言（`zh` 中文 / `en` 英文 / `multi` 多语言，决定嵌入模型）
   - 下载源（国内 `cn` / 国外 `official`）
   - 要不要给 Claude / Codex 挂钩子；有没有额外的笔记目录要一起索引
2. 用参数式一次装好（避免交互式在 agent 环境里卡住）：

```bash
<skill 目录>/bin/mem init --home ~/.agent-memory --model zh --mirror cn \
    --with-claude --with-codex --knowledge-dir ~/notes -y
```

3. 装完回读 `mem doctor`，把数据根、模型、索引块数报给用户。
   Codex 的 hook 需要用户手动 trust，装完要提醒。

先看会发生什么就加 `--dry-run`。已经装过再跑 init 是补齐模式，只做缺的步骤。

## 先判定任务边界

- 用户说“看下”“先分析”“只读”或“先出方案”时，只做检索和分析；不跑 `mem init`、`mem index`、
  `mem add`、`mem review`、`mem promote`、`mem sync --commit`、`mem export`，不改配置和 Hook。
- 只有用户明确说“记住 X”“沉淀一下”时才写入。“把它整理成 Skill”是改 Skill，不是写记忆。
- 记忆里的路径、函数、开关、工单状态都要回读当前代码、配置或数据确认。历史条目是线索，不是现状。

## 三层来源和可信度

| 层 | 内容 | 事实源 | 使用口径 |
|---|---|---|---|
| L1 `memory` | 用户偏好、纠偏、项目约束、资源指针 | `$MEM_HOME/memory/<type>/*.md` + `MEMORY.md` | 已人工沉淀，优先作为协作约束，仍可能过期 |
| L2 `knowledge` | 领域知识、踩坑、SOP、改动记录、复盘 | init 里配的笔记目录（`config.json` 的 L2 源） | 只读索引，必须结合当前状态验证 |
| L3 `session` | Claude Code / Codex 历史会话回合 | 各 agent 的 transcript JSONL | 噪声和敏感度最高，只用于找“上次怎么做的” |

`.index/`、`staging/`、`sessions/*.jsonl` 是派生产物和队列，不是已确认事实。
配了 `export.target` 的话，那个副本是 `mem export` 单向覆盖出来的，改它不影响事实源。
本机各路径的实际值看 `mem doctor` 第一段，别硬记。

## 检索

```bash
mem recall "完整的自然语言问题"                    # 默认全层，L1/L2 有保底席位
mem recall "..." --layer memory|knowledge|session  # 明确只要某一层时才加
mem recall "上次这个问题怎么处理的" --layer session --agent claude,codex
```

默认召回已经给 L1 留 3 席、L2 留 2 席（`config.json` 的 `recall.layer_quota`），
不需要为了让记忆露出来手动加 `--layer`；限定 `--layer` 时配额自动关闭，就是纯融合排序。
配额席位有相关性门槛（`recall.quota_min_sim`），所以问法很泛、或用词和记忆里不一致时
L1 可能一条都不出——**你有理由相信存在相关记忆却没看到，就换个贴近记忆用词的说法再查一次，
或补一次 `--layer memory`**。
完整问句同时喂关键词腿和向量腿，精确任务号、表名、类名可以直接当查询词。

读结果时看三处：层标记（L1 是结论、L2/L3 是线索）、`K/V`（关键词命中 / 语义命中）、
`⚠ stale` 和 `⚠ archived`（历史参考，不是现状）。`README.md`/`DESIGN.md` 命中的是方案说明，
不是业务记忆。

## 形成结论

1. 从召回结果拿到路径、会话 ID、时间戳。
2. 回读当前代码、配置、数据库、日志或外部页面确认候选仍成立。
3. 输出分成“当前已验证事实”和“记忆中的待验证线索”；冲突以现状为准。
4. 任何疑似 token、secret、cookie、密码、真实账号都不回显——索引前虽有正则脱敏，
   但正则不是保证。

## 写入

两条通道，都以 `mem sync` 收口：

```bash
mem add --type feedback --description "一句话摘要" --name kebab-slug <<'BODY'
正文。feedback/project 要写清 **Why:** 和 **How to apply:**。
BODY

mem add --type project --description "..." --staging <<'BODY'   # 不确定的先进待审区
BODY

mem sync            # 对账 + 增量索引 + 报队列/staging/未提交积压（不碰 git）
mem sync --commit   # 需要单独授权：提交到本地库，不 push
```

类型只有 `user` / `feedback` / `project` / `reference`。
agent 原生写入（直接 Write 到 `$MEM_HOME/memory/<type>/`）也算通道之一，**写完同样要 `mem sync`**：
它不会自己更新索引，也不会入库。

任务收尾、结论被推翻的记忆用 `mem archive <name>` 归档：仍可检索（标 `⚠ archived`），
但不再占开场注入的预算。`--undo` 可还原。

自动沉淀链路（会话结束 Hook → `mem review` 起草 → `staging/` → 人工审核 → `mem promote`）
和跨 agent 接入见 [references/curate.md](references/curate.md)。
模型起草的候选和历史会话都是不可信输入，审核时要查业务正确性和敏感信息。

## 排查

`mem doctor` 是第一步。常见故障、只读沙箱下的 `unable to open database file`、
索引与运行时重建、迁移机器要改的绝对路径，见
[references/troubleshooting.md](references/troubleshooting.md)。

当前已知边界：召回没有时间衰减（老结论和新结论同权）；增量索引按 mtime，不会在召回前
自动检查源文件；`prune_missing` 只在不带 `--layer` 的全量遍历时生效。除非用户要求修，
否则只报告，不顺手改运行时。
