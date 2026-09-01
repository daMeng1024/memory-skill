# memory —— 本地记忆库 skill

给 Claude Code / Codex 用的本地记忆层：把**协作记忆**（用户偏好、纠偏、项目约束）、
**领域知识**（自己的笔记目录）和**历史会话**放进一个统一检索层，会话开场自动注入索引，
需要细节时按需召回。

全本地、离线、无常驻进程。检索是 FTS5(trigram) 关键词 + 向量语义的混合召回，
一次 CLI 调用冷启动约 0.7 秒。

## 装

```bash
git clone <本仓库> ~/code/memory-skill
ln -s ~/code/memory-skill ~/.claude/skills/memory     # Codex 是 ~/.codex/skills/memory
~/code/memory-skill/bin/mem init
```

`mem init` 是向导，会逐项问你：

| 问什么 | 默认 | 说明 |
|---|---|---|
| 数据根 | `~/.agent-memory` | 记忆、索引、运行时都放这里，和代码分开 |
| 嵌入模型 | `zh`（bge-small-zh，90MB） | 还有 `en`、`multi`；按语料语言选 |
| 下载源 | `cn`（清华 PyPI + hf-mirror） | 国外网络选 `official` |
| 额外知识目录 | 无 | 你自己的笔记 / SOP 目录，只读索引，原地不动 |
| 要不要挂 hook | 探测到哪个 agent 就默认挂 | 开场注入 + 会话结束入队 |

装完之后在会话里直接说"记忆库"或"上次那个问题"，skill 就会被触发。
也可以让 agent 代你走向导——把这个 skill 装上、说一句"配置记忆库"就行。

非交互（CI 或让 agent 代跑）：

```bash
mem init --home ~/.agent-memory --model zh --mirror cn \
         --knowledge-dir ~/notes --with-claude --no-codex -y
mem init --dry-run -y        # 只打印将写入的配置和将执行的命令
```

## 用

```bash
mem recall "<自然语言问题>"        # 检索，-k 条数，--layer memory|knowledge|session
mem add --type feedback --description "一句话" <<'B' ... B    # 写一条记忆
mem sync                          # 写完收口：对账 + 增量索引 + 报积压
mem archive <name>                # 归档：不再进开场注入，仍可检索
mem doctor                        # 自检（第一行会告诉你代码根和数据根在哪）
```

`mem` 在 `~/.local/bin` 有软链（init 建的），任意目录可调。

## 代码和数据分在两处

```
<本仓库>                          代码，可以随便 clone、更新、分享
  SKILL.md  references/           agent 读的说明
  memlib/  bin/mem  hooks/        引擎、入口、钩子
  config.template.json            init 用的骨架

$MEM_HOME（默认 ~/.agent-memory）  数据，一台机器一份，别提交到公共仓库
  config.json                     本机配置（含绝对路径），init 生成
  memory/<type>/*.md  MEMORY.md   记忆事实源 + 索引页
  .index/                         向量库、模型缓存、venv，全是派生产物
  sessions/  staging/             待抽取队列、待审候选
```

数据根靠这个顺序找：`$MEM_HOME` → `~/.config/mem/home` 指针文件 → `~/.agent-memory`。
**指针文件是主路径**：hook 跑在非交互 shell 里，读不到你在 `.bashrc` 里 export 的变量。

想搬数据：把 `$MEM_HOME` 整个移走，改指针文件，再把 `config.json` 里的 `root` 和
`sources[].path` 改掉（或者 `mem init --force` 重来一遍）。

## 三层来源

| 层 | 装什么 | 谁写 | 可信度 |
|---|---|---|---|
| L1 `memory` | 用户偏好、纠偏、项目约束、资源指针 | `mem add` / agent 原生写入 / `mem promote` | 已人工沉淀，优先 |
| L2 `knowledge` | 你自己的笔记、SOP、踩坑（init 里配的目录） | 沿用你原有流程，本库只读索引 | 线索，要验证 |
| L3 `session` | Claude Code / Codex 的历史会话 | 两个 agent 自动产生 | 噪声最高，只用于回溯 |

只有 L1 会被注入会话开场（按类型优先级和新旧选，预算 `inject.max_bytes`）；
L2/L3 靠召回。索引前有正则脱敏（`config.json` 的 `redaction`，`mem doctor` 跑自测）。

## 卸

```bash
bash <本仓库>/hooks/install.sh --uninstall claude    # codex 同
rm ~/.claude/skills/memory ~/.local/bin/mem ~/.config/mem/home
rm -rf $MEM_HOME        # 记忆也在里面，想留就别删
```

## 更多

- 设计与选型（为什么是 trigram + 去均值向量 + RRF + 分层配额）：[DESIGN.md](DESIGN.md)
- 沉淀、审核、归档、跨 agent 接入：[references/curate.md](references/curate.md)
- 排查与重建：[references/troubleshooting.md](references/troubleshooting.md)
- 安装细节、平台差异、作者本机的配置示例：[references/install.md](references/install.md)
