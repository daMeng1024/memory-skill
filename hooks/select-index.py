#!/usr/bin/env python3
"""按条目挑选要注入会话的记忆索引行。

为什么不直接 `head -c`：MEMORY.md 的索引行是追加的，按字节从头硬切等于
"越新写的记忆越进不去上下文"——记了等于没记。这里改成按条目选：
先按类型排优先级（协作约束 > 项目事实），同类型内新的在前，装到预算为止。

刻意只用标准库、只走系统 python3：这是 SessionStart 路径上的代码，
不能依赖 .index/.venv——venv 坏掉时应该少注入几条，而不是每次开场静默丢掉全部记忆。

单独跑可以直接看会注入什么：
    python3 hooks/select-index.py [--max-bytes N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAX_BYTES = 4096

# - [标题](memory/<type>/<slug>.md) — 一句话
ENTRY = re.compile(r"^-\s*\[[^\]]*\]\((memory/([a-z]+)/[^)]+\.md)\)")
# 协作约束优先于项目事实：前者管"怎么跟我配合"，错过的代价最大
TYPE_ORDER = {"user": 0, "feedback": 1, "reference": 2, "project": 3}
ARCHIVED_SECTION = re.compile(r"^#+\s*已归档")


def _status(path: Path) -> str:
    """读 frontmatter 顶层 status，形式与 memlib.chunk.split_frontmatter 一致。"""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            if fh.readline().strip() != "---":
                return ""
            for line in fh:
                if line.strip() == "---":
                    break
                if line.startswith((" ", "\t", "-")) or ":" not in line:
                    continue
                k, _, v = line.partition(":")
                if k.strip() == "status":
                    return v.strip().strip("'\"")
    except OSError:
        pass
    return ""


def select(root: Path, max_bytes: int) -> tuple[str, int, int]:
    """返回 (注入正文, 选中条数, 省略条数)。"""
    index = root / "MEMORY.md"
    lines = index.read_text(encoding="utf-8").splitlines()
    header = lines[0] if lines and lines[0].startswith("#") else "# 记忆索引"

    entries = []
    archived_section = False
    for line in lines:
        if line.startswith("#"):
            archived_section = bool(ARCHIVED_SECTION.match(line))
            continue
        m = ENTRY.match(line)
        if not m:
            continue
        if archived_section:
            continue
        rel, mtype = m.group(1), m.group(2)
        f = root / rel
        if not f.exists() or _status(f) == "archived":
            continue
        entries.append((TYPE_ORDER.get(mtype, 9), -f.stat().st_mtime, line))

    entries.sort()
    budget = max_bytes - len(header.encode()) - 1
    picked, dropped = [], 0
    for _, _, line in entries:
        cost = len(line.encode()) + 1
        if cost > budget:
            dropped += 1
            continue
        budget -= cost
        picked.append(line)

    def render(rows, n_dropped):
        text = "\n".join([header, ""] + rows)
        if n_dropped:
            text += f"\n\n（另有 {n_dropped} 条未展示，完整索引见 {index}，或用 mem recall 检索）"
        return text

    body = render(picked, dropped)
    # 尾注本身也算字节：超了就再退掉几条，保证注入体量真的落在预算内
    while picked and len(body.encode()) > max_bytes:
        picked.pop()
        dropped += 1
        body = render(picked, dropped)
    return body, len(picked), dropped


def main() -> int:
    ap = argparse.ArgumentParser(description="挑选注入会话的记忆索引行")
    ap.add_argument("--max-bytes", type=int, default=None)
    ap.add_argument("--root", default=str(ROOT))
    args = ap.parse_args()

    root = Path(args.root)
    max_bytes = args.max_bytes
    if max_bytes is None:
        try:
            cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
            max_bytes = int(cfg.get("inject", {}).get("max_bytes", DEFAULT_MAX_BYTES))
        except (OSError, ValueError):
            max_bytes = DEFAULT_MAX_BYTES

    body, n, dropped = select(root, max_bytes)
    sys.stdout.write(body)
    print(f"\n\n-- 选中 {n} 条，省略 {dropped} 条，{len(body.encode())} 字节", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
