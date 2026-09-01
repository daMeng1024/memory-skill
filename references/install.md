# 安装细节与平台差异

主流程在 [README](../README.md)，这里是会踩到的地方。

## init 的九步，各自会失败在哪

| 步 | 干什么 | 常见失败 |
|---|---|---|
| 1 探测 | 平台、`uv`、python≥3.11、Claude/Codex 目录 | 找不到 python≥3.11：先装一个，init 不会替你装 |
| 2 选模型 | zh / en / multi，写 `query_prefix` | 选错了没关系，`mem init --force` 重来，但要 `mem index --full` 重建 |
| 3 写配置 | `$MEM_HOME/config.json` + `~/.config/mem/home` 指针 | 指针写不进去（`~/.config` 权限）→ 之后每个 shell 都得自己设 `MEM_HOME` |
| 4 目录树 | `memory/<type>`、`sessions`、`staging`、索引页软链 | — |
| 5 运行时 | `uv venv` + `fastembed numpy`，没 uv 就 `venv`+`pip` | 网络。`--mirror cn` 走清华源 |
| 6 模型 + 首次索引 | 下模型，索引 L1+L2 | 见下面"模型下载" |
| 7 hook | 调 `hooks/install.sh` | Codex 装完要人工 trust |
| 8 软链 | `~/.local/bin/mem` | 已存在且指向别处时只提示不覆盖 |
| 9 自检 | `mem doctor` | — |

每步幂等，装到一半失败直接重跑 `mem init`，已完成的会跳过（改配置要 `--force`）。

## 模型下载

`--mirror cn` 把 `HF_ENDPOINT` 设成 `hf-mirror.com`。实测这个镜像**也可能失败**，
fastembed 会自动回落到备用源继续下——日志里出现一行红色 `Could not download model
from HuggingFace ... Falling back to other sources` 但下载条继续走，属于正常，不用管。

模型落在 `$MEM_HOME/.index/models`。只要那儿有 `.onnx`，之后启动自动切
`HF_HUB_OFFLINE=1`，不再联网探测（否则每次冷启动白等几秒）。

换模型要 `mem index --full` 全量重建：向量维度和语义空间都变了，旧向量没法用。

## 首次索引为什么不带会话层

`mem init` 只索引 L1+L2。会话层（Claude/Codex 的 transcript）在用过一阵子的机器上
动辄一两千个文件、上万个块，第一次要跑十几分钟——卡在安装流程里体验太差。
装完挑个时间自己跑 `mem index` 补上，之后都是增量。

## 平台

- **Linux / WSL2**：主路径，开发和验证都在这上面
- **WSL2 额外一条**：init 会探测 `/mnt/c/Users/*/.codex/sessions`，
  把 Windows 侧 Codex 的会话也纳入索引；不想要就从 `config.json` 的 `sources` 删掉
- **macOS**：没实测过。理论上只有 `uv venv` 和路径探测两处相关，应该能跑
- **Windows 原生**：不支持。Windows 侧请调回 WSL，别在 Windows 另建一套运行时

## 数据根为什么和代码分开

代码是**可移植**的（clone 到哪都行，随时更新），数据是**长在这台机器上**的
（绝对路径、编译好的 onnxruntime、你的私人记忆）。混在一起的后果是：
把 skill 分享给同事时会连私人记忆一起给出去，而换机器时又会拖着一个必然作废的 venv。

数据根解析顺序：`$MEM_HOME` → `~/.config/mem/home` 指针 → `~/.agent-memory`。
**指针文件是主路径**，因为 hook 跑在非交互 shell 里读不到用户环境变量——
很多 shell 配置（比如 `.bashrc` 顶部的非交互早退守卫）会让 `export` 的变量完全不可见。

## 一份完整配置示例（WSL2）

`mem init` 生成的就是这个形状，贴出来是方便你手改。路径按自己的来：

```jsonc
{
  "root": "/home/you/.agent-memory",           // 数据根，init 写进去的
  "sources": [
    { "name": "memory",     "layer": "L1", "path": "<root>/memory", "glob": "*/*.md" },
    { "name": "memory-doc", "layer": "L1", "path": "<root>",        "glob": "*.md" },
    { "name": "skill-doc",  "layer": "L1", "path": "/home/you/code/memory-skill", "glob": "**/*.md" },
    // L2：你自己的笔记目录，原地只读索引，不搬家。有几个写几个
    { "name": "notes",    "layer": "L2", "path": "/home/you/notes",          "glob": "**/*.md" },
    { "name": "pitfalls", "layer": "L2", "path": "/mnt/d/vault/pitfalls",    "glob": "**/*.md" },
    { "name": "sop",      "layer": "L2", "path": "/mnt/d/vault/sop",         "glob": "**/*.md" },
    // L3：两个 agent 的会话。WSL 里通常有三个目录——Claude、WSL 侧 Codex、Windows 侧 Codex
    { "name": "claude-session", "layer": "L3", "kind": "claude_jsonl", "agent": "claude",
      "path": "/home/you/.claude/projects", "glob": "**/*.jsonl" },
    { "name": "codex-session",  "layer": "L3", "kind": "codex_jsonl",  "agent": "codex",
      "path": "/home/you/.codex", "glob": "*sessions/**/*.jsonl" },
    { "name": "codex-session-win", "layer": "L3", "kind": "codex_jsonl", "agent": "codex",
      "path": "/mnt/c/Users/you/.codex/sessions", "glob": "**/*.jsonl" }
  ],
  "export": { "target": "/mnt/d/vault/memory" }  // 可选：给另一个平台只读查阅
}
```

几个值得单独说的：

- **`skill-doc` 源**指向本仓库自己的文档目录，作用是让库能回答"我自己怎么用"——
  `mem recall "记忆库怎么装"` 会命中 README / SKILL.md
- **L2 不搬家**：只读索引你已有的笔记目录，原来的编辑器、同步、备份流程都不受影响
- **`redaction.skip_path_patterns` 加你自己项目的文件名**：任何一看名字就知道装着账号、
  token、cookie 的配置文件（各家叫法不同），加进去后整块跳过不索引。
  这类规则是你项目专有的，加在本机 `config.json`，不要提到 `config.template.json` 里

如果你的 agent 是 per-project 记忆（比如 Claude Code 的 `~/.claude/projects/*/memory`），
把那些目录软链到 `$MEM_HOME/memory`，原生写入就会直接落进本库、并且跨项目共享。
新开一个项目目录要手动补这条链。
