# 排查手册

第一步永远是 `mem doctor`：运行时、SQLite FTS5、脱敏自测、索引源文件数、库内块数、
模型缓存，逐项列出来。

## 症状对照

| 症状 | 先看 |
|---|---|
| 检索不到刚写的记忆 | 没跑 `mem sync`（或 `mem index`）。索引按 mtime 增量，不会自动触发 |
| 索引行和文件对不上 | `mem lint`：孤儿文件、死链接、重复行、frontmatter 的 type/name 不一致 |
| 结果全是 `K-` 没有 `V` | 语义腿挂了。`doctor` 看"上次语义检索错误"，多半是模型缓存丢了或 venv 坏了 |
| 结果全是 `-V` 没有 `K` | 查询里没有 df 落在 (0, 25%] 的词片，纯自然语言问法就是这样，正常 |
| `没有命中` | 两条腿都空。看 `doctor` 的块数，库空了就 `mem index --full` |
| 检索慢（>3s） | fastembed 在联网探测 HuggingFace。确认 `.index/models` 下有 `.onnx`，有则会自动切离线 |
| 删了记忆文件但还能搜到 | `prune_missing` 只在不带 `--layer` 的全量遍历生效，跑一次 `mem index` |
| 开场看不到某条记忆 | 注入是按预算选的，跑 `python3 hooks/select-index.py` 看它是否被省略或已归档 |

## 只读沙箱下的 `unable to open database file`

`doctor` 和 `recall` 初始化 SQLite 时会设置 WAL，需要对 `.index/` 有写权限。
在 Codex 的只读沙箱里报这个错，**先判定为运行环境限制，不要删库或重建**。
需要诊断就对 `memory.db` 做临时副本再只读打开；副本能打开、`immutable=1` 完整性检查通过，
只能证明副本可读，不能替代宿主环境的可写验证。

## 重建

```bash
mem index            # 增量，按 mtime
mem index --full     # 全量重建，几十 MB 的库要跑几分钟，只在明确授权时做
```

`.index/` 全是派生产物，出怪问题可以整个删掉重建。运行时重建：

```bash
uv venv --python 3.13 .index/.venv
UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
  uv pip install --python .index/.venv/bin/python fastembed numpy
```

`bin/mem` 硬编码走 `.index/.venv/bin/python`（3.13）：系统 `/usr/bin/python3` 是 3.14 且
无 pip、无 ensurepip，任何 fallback 都会静默退化。pypi.org 直连不通，必须走清华源。
模型 `BAAI/bge-small-zh-v1.5` 缓存在 `.index/models`，已缓存时自动切 `HF_HUB_OFFLINE=1`。

## 迁移 / 换机

**换机器**：代码仓库 clone 一份，然后 `mem init` 重来一遍（会重建 venv 和模型缓存）。
想带上旧记忆，就把旧机器的 `$MEM_HOME/memory/` 和 `MEMORY.md` 拷过去再 `mem sync`。
`.index/` 不要拷——里面是绝对路径 + 平台相关的 venv，重新索引比修它快。

**只是挪数据根**：把 `$MEM_HOME` 整个移走，然后三件事——改 `~/.config/mem/home` 指针、
改 `config.json` 里的 `root` 和 `sources[].path`（L1 那两条指向新位置）、重跑 `mem index`。
hook 和 `~/.local/bin/mem` 不用动，它们指的是代码根。

**挪代码根**：重跑 `hooks/install.sh <agent>`（注册里写的是绝对路径），
重建 `~/.local/bin/mem` 软链，重建 `~/.claude/skills/memory` 和 `~/.codex/skills/memory`。
数据根不受影响。

哪一处漏改的表现都是"静默不工作"而不是报错，所以改完跑一次 `mem doctor` 和
`python3 hooks/select-index.py --root "$MEM_HOME"` 回读。
