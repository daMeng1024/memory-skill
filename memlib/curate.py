"""写侧收口：lint / sync / archive。

记忆库有两条写入通道——`mem add` 和 agent 原生 Write——两条都只管把文件落下来，
落完谁去跑索引、谁去对账、谁去提交，全靠人记得。结果就是索引查不到刚写的记忆、
文件躺在工作区几天不入库、待抽取队列只增不减。

`mem sync` 是这三件事的统一收口：对账 → 索引 → 报告积压。默认不碰 git，
提交要显式 `--commit`。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

VALID_TYPES = ("user", "feedback", "project", "reference")
ENTRY = re.compile(r"^-\s*\[([^\]]*)\]\((memory/([a-z]+)/([^)]+)\.md)\)")
ARCHIVED_HEADING = "## 已归档"
STALE_DAYS = 30


# ---------------------------------------------------------------- 基础解析
def read_frontmatter(path: Path) -> dict:
    """解析一层嵌套的 frontmatter：顶层 key 直接取，metadata 下的取到 metadata.*。

    chunk.split_frontmatter 只取顶层（够索引用），这里要校验 metadata.type，
    所以单独解析，不去动那个函数的既有语义。
    """
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    if not text.startswith("---"):
        return out
    lines = text.splitlines()[1:]
    section = None
    for line in lines:
        if line.strip() == "---":
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        k, _, v = line.partition(":")
        v = v.strip().strip("'\"")
        if line.startswith((" ", "\t")):
            if section:
                out[f"{section}.{k.strip()}"] = v
            continue
        section = k.strip() if not v else None
        if v:
            out[k.strip()] = v
    return out


def index_entries(root: Path) -> list[tuple[str, str, str, str]]:
    """MEMORY.md 里的索引行 → [(标题, 相对路径, type, slug)]。"""
    idx = root / "MEMORY.md"
    if not idx.exists():
        return []
    out = []
    for line in idx.read_text(encoding="utf-8").splitlines():
        m = ENTRY.match(line)
        if m:
            out.append((m.group(1), m.group(2), m.group(3), m.group(4)))
    return out


def memory_files(root: Path) -> list[Path]:
    return sorted(
        p for t in VALID_TYPES for p in (root / "memory" / t).glob("*.md") if p.is_file()
    )


# ---------------------------------------------------------------- lint
def lint(cfg) -> list[str]:
    """文件 ↔ 索引行 ↔ frontmatter 三者对账，返回问题清单。"""
    root = cfg["_root"]
    problems: list[str] = []

    entries = index_entries(root)
    linked: dict[str, int] = {}
    for _, rel, mtype, slug in entries:
        linked[rel] = linked.get(rel, 0) + 1
        if mtype not in VALID_TYPES:
            problems.append(f"索引行 type 不合法：{rel}")
        if not (root / rel).exists():
            problems.append(f"索引行指向的文件不存在：{rel}")
    for rel, n in linked.items():
        if n > 1:
            problems.append(f"索引里有 {n} 条重复行：{rel}")

    for f in memory_files(root):
        rel = f"memory/{f.parent.name}/{f.name}"
        if rel not in linked:
            problems.append(f"有文件但索引里没有：{rel}")
        meta = read_frontmatter(f)
        if not meta:
            problems.append(f"缺 frontmatter：{rel}")
            continue
        for key in ("name", "description"):
            if not meta.get(key):
                problems.append(f"frontmatter 缺 {key}：{rel}")
        mtype = meta.get("metadata.type") or meta.get("type")
        if not mtype:
            problems.append(f"frontmatter 缺 metadata.type：{rel}")
        elif mtype != f.parent.name:
            problems.append(f"metadata.type={mtype} 与所在目录 {f.parent.name} 不一致：{rel}")
        if meta.get("name") and meta["name"] != f.stem:
            problems.append(f"frontmatter name={meta['name']} 与文件名 {f.stem} 不一致：{rel}")
    return problems


def run_lint(cfg) -> int:
    problems = lint(cfg)
    if not problems:
        n_files = len(memory_files(cfg["_root"]))
        print(f"[ok ] 一致性检查通过：{n_files} 个文件 / {len(index_entries(cfg['_root']))} 条索引行")
        return 0
    print(f"[FAIL] {len(problems)} 个一致性问题：")
    for p in problems:
        print(f"  - {p}")
    return 1


# ---------------------------------------------------------------- 队列
def _agent_of(path: str) -> str:
    return "codex" if "codex" in path else "claude"


def tidy_queue(cfg, apply: bool) -> tuple[dict, int, int]:
    """给老行补 agent 字段，把已了结的行搬进 processed.jsonl。

    队列文件早期没有 agent 字段，review 一律回落 claude——现存老行恰好都是
    claude 会话所以没踩雷，但换成 codex 的老行就会用错解析器。顺手补掉。
    """
    root = cfg["_root"]
    pending = root / "sessions" / "pending.jsonl"
    if not pending.exists():
        return {}, 0, 0
    rows = []
    for line in pending.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    filled = 0
    for r in rows:
        if not r.get("agent"):
            r["agent"] = _agent_of(r.get("transcript_path", ""))
            filled += 1

    live = [r for r in rows if r.get("status") in ("pending", "drafted")]
    done = [r for r in rows if r.get("status") not in ("pending", "drafted")]

    if apply and (filled or done):
        if done:
            with (root / "sessions" / "processed.jsonl").open("a", encoding="utf-8") as fh:
                for r in done:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp = pending.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in live), encoding="utf-8"
        )
        tmp.replace(pending)

    stats: dict[str, int] = {}
    for r in live:
        key = f"{r.get('agent','?')}/{r.get('status','?')}"
        stats[key] = stats.get(key, 0) + 1
    return stats, filled, len(done)


def _earliest(cfg) -> str:
    p = cfg["_root"] / "sessions" / "pending.jsonl"
    if not p.exists():
        return ""
    times = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("status") in ("pending", "drafted") and r.get("captured_at"):
            times.append(r["captured_at"])
    return min(times) if times else ""


# ---------------------------------------------------------------- sync
def _git(root: Path, *args: str) -> tuple[int, str]:
    r = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )
    return r.returncode, (r.stdout + r.stderr).strip()


def run_sync(cfg, args) -> int:
    root = cfg["_root"]
    print("== 一致性 ==")
    rc = run_lint(cfg)

    print("\n== 索引 ==")
    if args.no_index:
        print("  跳过（--no-index）")
    else:
        from .cli import cmd_index

        class _A:
            full = False
            layer = "memory"

        cmd_index(_A(), cfg)
        print("  注：删过记忆文件的话，要再跑一次不带 --layer 的 mem index 才会清理失效文档")

    print("\n== 待抽取队列 ==")
    stats, filled, moved = tidy_queue(cfg, apply=not args.no_tidy)
    if filled or moved:
        verb = "已" if not args.no_tidy else "待"
        print(f"  {verb}补 agent 字段 {filled} 行，{verb}归档已了结 {moved} 行 → sessions/processed.jsonl")
    if stats:
        print("  在途：" + "，".join(f"{k} {v}" for k, v in sorted(stats.items())))
        earliest = _earliest(cfg)
        if earliest:
            print(f"  最早一条 {earliest}；抽取记忆跑 mem review")
    else:
        print("  空")

    staging = sorted(p for p in (root / "staging").glob("*") if p.is_dir())
    if staging:
        print(f"  staging 残留 {len(staging)} 个批次：" + "，".join(p.name for p in staging))
        print("  审核后跑 mem promote <run-id>，不要的直接删目录")

    print("\n== 过期候选 ==")
    cutoff = time.time() - args.stale_days * 86400
    stale = [
        f
        for f in memory_files(root)
        if f.stem.startswith("task-")
        and f.stat().st_mtime < cutoff
        and read_frontmatter(f).get("status") != "archived"
    ]
    if stale:
        print(f"  {len(stale)} 条任务型记忆超过 {args.stale_days} 天没更新，确认收尾后可归档：")
        for f in stale:
            print(f"    mem archive {f.stem}")
    else:
        print(f"  没有超过 {args.stale_days} 天的任务型记忆")

    print("\n== 版本库 ==")
    if not (root / ".git").exists():
        # 数据根不一定是 git 仓库——新装的机器就不是，这不是错误
        print("  数据根不是 git 仓库，跳过（想给记忆留版本历史就在数据根 git init）")
        return rc
    code, out = _git(root, "status", "--porcelain", "--", "memory", "MEMORY.md")
    if code != 0:
        print(f"  git 不可用：{out[:200]}")
    elif not out:
        print("  干净，无未提交的记忆改动")
    else:
        files = out.splitlines()
        print(f"  {len(files)} 个未提交改动：")
        for line in files[:20]:
            print(f"    {line}")
        if len(files) > 20:
            print(f"    …另有 {len(files)-20} 个")
        if args.commit:
            if rc != 0:
                print("  一致性检查没过，不提交")
                return rc
            msg = args.message or f"memory: sync {len(files)} 条记忆改动"
            c1, o1 = _git(root, "add", "--", "memory", "MEMORY.md")
            if c1 != 0:
                print(f"  git add 失败：{o1[:300]}")
                return 1
            c2, o2 = _git(root, "commit", "-m", msg)
            print(f"  {o2.splitlines()[0] if o2 else ''}")
            if c2 != 0:
                return 1
            print("  已提交（不 push）")
        else:
            print("  提交：mem sync --commit（只提交不 push）")
    return rc


# ---------------------------------------------------------------- archive
def _find(root: Path, name: str) -> Path | None:
    for t in VALID_TYPES:
        p = root / "memory" / t / f"{name}.md"
        if p.exists():
            return p
    return None


def _set_status(path: Path, status: str | None) -> None:
    """在 frontmatter 里写入/删除顶层 status，其余内容原样不动。"""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
    if not lines or not lines[0].startswith("---") or end is None:
        raise SystemExit(f"没有可解析的 frontmatter：{path}")
    body = [l for l in lines[1:end] if not l.startswith(("status:", "archived_at:"))]
    if status:
        anchor = next((i for i, l in enumerate(body) if l.startswith("description:")), -1)
        add = [f"status: {status}\n", f"archived_at: {time.strftime('%Y-%m-%d')}\n"]
        body[anchor + 1 : anchor + 1] = add
    path.write_text("".join([lines[0]] + body + lines[end:]), encoding="utf-8")


def _move_index_line(root: Path, rel: str, to_archive: bool) -> bool:
    idx = root / "MEMORY.md"
    lines = idx.read_text(encoding="utf-8").splitlines()
    target = next((i for i, l in enumerate(lines) if ENTRY.match(l) and ENTRY.match(l).group(2) == rel), None)
    if target is None:
        return False
    line = lines.pop(target)
    if to_archive:
        if ARCHIVED_HEADING not in lines:
            while lines and not lines[-1].strip():
                lines.pop()
            lines += ["", ARCHIVED_HEADING, ""]
        lines.append(line)
    else:
        head = lines.index(ARCHIVED_HEADING) if ARCHIVED_HEADING in lines else len(lines)
        insert = head
        while insert > 0 and not lines[insert - 1].strip():
            insert -= 1
        lines.insert(insert, line)
        if ARCHIVED_HEADING in lines and not any(
            ENTRY.match(l) for l in lines[lines.index(ARCHIVED_HEADING) :]
        ):
            h = lines.index(ARCHIVED_HEADING)
            del lines[h:]
    idx.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    return True


def run_archive(cfg, args) -> int:
    root = cfg["_root"]
    if args.list:
        rows = [(f, read_frontmatter(f)) for f in memory_files(root)]
        rows = [(f, m) for f, m in rows if m.get("status") == "archived"]
        if not rows:
            print("没有已归档的记忆")
            return 0
        for f, m in rows:
            print(f"  {f.stem}  (archived_at={m.get('archived_at','?')})  {m.get('description','')}")
        return 0

    if not args.name:
        raise SystemExit("要归档哪条？用法：mem archive <name> / mem archive --list")
    path = _find(root, args.name)
    if path is None:
        raise SystemExit(f"找不到这条记忆：{args.name}（memory/<type>/{args.name}.md）")
    rel = f"memory/{path.parent.name}/{path.name}"
    meta = read_frontmatter(path)

    if args.undo:
        if meta.get("status") != "archived":
            print(f"{args.name} 本来就不是归档状态")
            return 0
        _set_status(path, None)
        _move_index_line(root, rel, to_archive=False)
        print(f"已取消归档：{rel}")
    else:
        if meta.get("status") == "archived":
            print(f"{args.name} 已经是归档状态")
            return 0
        _set_status(path, "archived")
        _move_index_line(root, rel, to_archive=True)
        print(f"已归档：{rel}")
        print("  仍可被 mem recall 检索到（结果标 ⚠ archived），但不再进开场注入")
    print("  记得跑 mem sync 让索引跟上")
    return 0
