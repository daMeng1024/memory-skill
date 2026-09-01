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
                                                          mem audit
                                        ┌───────────────────┼───────────────────┐
                                    rejected/            自动过              留在原地
                                   （附拒绝理由）      （附执行过的证据）      （人看）
                                                          │                    │
                                                memory/<type>/*.md        人工删掉不要的
                                                status: auto                   │
                                                能召回、不进注入          mem promote ▼
                                                          │           memory/<type>/*.md
                                                 mem approve <name>    + MEMORY.md
                                                          ▼
                                                       转正，进注入
```

硬约束：**模型起草的候选永远落 `staging/`，不直接进事实源**。写错的记忆比没记忆更糟。

```bash
mem review --dry-run       # 先看会处理哪些会话，不调模型
mem review --limit 5       # 起草 5 个会话的候选
mem audit <run-id>         # 自动分档（--dry-run 只判定不动文件）
mem promote <run-id>       # 人工看完剩下的再合入
mem sync                   # 收口
```

## 自动审核（mem audit）

风险是不对称的，所以两档门槛不一样：**自动拒**判错了最多漏一条，transcript 还在，
随时能重抽；**自动过**判错了会让一条错记忆进正式库、进开场注入，之后每个会话都被它影响。

**自动拒**（有确定性判据就拒，候选移到 `staging/<run-id>/rejected/`，报告里写明理由）：

| 判据 | 说明 |
|---|---|
| 命中脱敏规则 | 候选正文从没被 scrub 过，promote 之后直接进 git。这条优先级最高 |
| 缺 `**Why:**` / `**How to apply:**` | `feedback` 和 `project` 的硬性结构要求 |
| slug 不合法、缺 description | 格式 |
| 路径写错 | 正文里反引号包的路径**父目录存在但自己不存在**——这是写错的强证据。父目录也不在的不算，那多半是远端或别人机器上的路径 |
| 正式库已有同名条目 | 需要更新就手动合并，不靠 promote 覆盖 |
| 与已有条目语义重复 | 相似度 ≥ `audit.dup_similarity`；超过 `reconfirm_similarity` 时顺手给旧条目刷 `verified_at`（只刷时间戳，不引入新事实） |

**自动过**（三个条件同时成立才放行）：

1. 类型在 `audit.auto_types` 里（默认只有 `project` / `reference`）
2. 候选自带 `verify` 断言，且**全部执行通过**
3. 上面所有自动拒的判据都没命中

断言由起草那一步的模型显式给出（`review.py` 的 PROMPT 里有格式），`audit` 只负责执行——
从散文里正则挖断言太容易挖错，挖错的代价是放行一条没验过的记忆。支持的断言：

```jsonc
{"kind": "path",            "path": "/abs/or/~/path"}        // 存在
{"kind": "absent_path",     "path": "..."}                   // 不存在
{"kind": "command",         "command": "psql"}               // 在 PATH 里
{"kind": "absent_command",  "command": "mysql"}              // 不在 PATH 里
{"kind": "grep",            "path": "...", "pattern": "字面量"}
{"kind": "git_ref",         "repo": "/abs/repo", "ref": "master"}
```

**`user` 和 `feedback` 永远不会被自动放行**：偏好和纠偏是人定的规矩，外部没有任何证据
能验证它，机器无权代判。

**安全阀**：自动放行的条目进正式库时挂 `status: auto`，**参与召回（结果标 `⚠ auto`）
但不进开场注入**。注入是无条件进每个会话上下文的，召回是按需的——错了也只在你主动
查到时才见到。抽查过了 `mem approve <name>` 转正；不对就 `mem archive <name>` 或直接删。

```bash
mem approve --list         # 待转正的条目 + 它们各自执行过的证据
mem approve <name>         # 转正
```

`mem sync` 会报还有几条待转正。

人工审核剩下那些时，逐条查：业务结论对不对、有没有把"当时的临时状态"写成长期事实、
任务号和路径是不是还成立。

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
