# 沉淀、审核与跨 agent 接入

## 写入的两条通道

| 通道 | 怎么发生 | 落点 | 风险 |
|---|---|---|---|
| `mem add` | 明确要沉淀某条结论时手敲 | 正式库（`--staging` 则进待审区），自动追加 `MEMORY.md` 索引行 | 内容由人把关 |
| agent 原生写入 | 用户说“记住 X”，agent 直接 Write 到 `memory/<type>/` | 正式库 | 不经脱敏、不自动索引、不自动入库 |

`~/.claude/projects/*/memory` 全部软链到本库 `memory/`，所以原生写入直接落正式库，
并且跨项目共享。`memory/MEMORY.md` 是指向 `../MEMORY.md` 的软链——两个写入方共用一份索引页。

**两条通道写完都必须 `mem sync`**：对账 → 增量索引 → 报积压。不 sync 的后果是
刚写的记忆检索不到、索引行和文件对不上、文件长期躺在工作区不入库。

## 自动沉淀链路

```
会话结束 hook ──▶ sessions/pending.jsonl ──mem review──▶ staging/<run-id>/*.md
                     （只记指针，无正文）                        │
                                                        人工删掉不要的
                                                                │
                                                   mem promote ▼
                                          memory/<type>/*.md + MEMORY.md
```

硬约束：**模型起草的候选永远落 `staging/`，不直接进事实源**。写错的记忆比没记忆更糟。

```bash
mem review --dry-run       # 先看会处理哪些会话，不调模型
mem review --limit 5       # 起草 5 个会话的候选
mem promote <run-id>       # 人工删完不要的之后再合入
mem sync                   # promote 之后收口
```

审核候选时逐条查：业务结论对不对、有没有把"当时的临时状态"写成长期事实、
有没有 token/密码/cookie/真实账号、任务号和路径是不是还成立。

队列语义：`pending`（待起草）→ `drafted`（已起草待审）→ `promoted`（已合入）。
`mem sync` 会把已了结的行搬进 `sessions/processed.jsonl`，队列文件只留在途的。

## 归档

```bash
mem archive <name>          # 打 status: archived，索引行移到 MEMORY.md 的「## 已归档」
mem archive --list
mem archive <name> --undo
```

归档只影响**开场注入**（不再占预算），不影响检索——召回时仍会出现，标 `⚠ archived`。
`mem sync` 会提示超过 30 天没更新的 `task-*` 记忆作为归档候选，但要不要归档由人判断：
任务合入不等于结论失效。

## 开场注入

`hooks/inject-on-start.sh` 只注入 `MEMORY.md` 的索引行，不注入正文；正文按需召回。
选取逻辑在 `hooks/select-index.py`（只用标准库、系统 python3，不依赖 `.index/.venv`）：

- 类型优先级 `user` → `feedback` → `reference` → `project`，同类型内按 mtime 新的在前
- 跳过 `status: archived` 和「已归档」段
- 预算 `config.json` 的 `inject.max_bytes`（默认 4096），装不下的条目在末尾报数量

想看会注入什么，直接跑 `python3 hooks/select-index.py`。

## 跨 agent 接入

记忆库本身不认 agent，两侧都走通用契约：hook 从 stdin 收 `{session_id, transcript_path}`，
往 stdout 吐 `hookSpecificOutput.additionalContext`。接入第三个 agent 通常只是配置：

1. `config.json` 的 `agents` 加一条：`kind` / `draft_cmd` + `draft_output` / `resume_hint`
2. `config.json` 的 `sources` 加一条 L3 源，带 `agent` 字段（`mem recall --agent` 和
   `mem review` 的分派都靠它）
3. `hooks/install.sh` 的 `case` 加分支，然后 `hooks/install.sh <agent>`

只有当新 agent 的 transcript 格式既不是 Claude 也不是 Codex 的，才需要在 `memlib/sources.py`
加解析器。

已接入：Claude Code（`~/.claude/settings.json`，SessionStart + Stop）、
Codex（`~/.codex/hooks.json`，SessionStart + SessionEnd）。

- Codex 对新增或变更的 hook 要人工授信：启动一次 Codex 在审阅界面 trust，状态落在
  `~/.codex/config.toml` 的 `[hooks.state]`。**没授信的 hook 静默不跑，不报错**，
  配置存在不等于已生效。
- Codex 的 `draft_cmd` 不传 `-m`：`mem review --model` 默认是 Claude 的模型名，
  硬传给自定义 provider 会挂死到超时。

## 导出

`mem export` 把 `memory/`、`MEMORY.md`、`config.json` 单向覆盖到 `config.json` 的
`export.target`，用于给另一个平台（比如 WSL2 → Windows）只读查阅。没配 target 就会直接报错。

**副本是只读的**：不要在副本上写第二套记忆库，也不要在那边另建运行时。
跨平台检索让对方调回事实源这边，比如 Windows 侧：
`wsl.exe bash -lc "mem recall '<问题>'"`。副本的 README 由 export 自动生成，
里面会写清事实源在哪；想自定义回读命令就在 config 里加 `export.readback_hint`。
