"""mem 命令行入口。"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from . import chunk as chunkmod
from . import config as cfgmod
from . import sources as sourcesmod
from .redact import Redactor

# search / store 都在模块顶层 import numpy，而 `mem init` 要能在还没建 venv 的
# 裸 python 上跑起来——所以这两个改成用到时再导。

LAYER_ALIAS = {
    "memory": "L1", "l1": "L1",
    "knowledge": "L2", "l2": "L2",
    "session": "L3", "l3": "L3",
}


def _layers(arg: str | None) -> set[str] | None:
    if not arg or arg == "all":
        return None
    out = set()
    for piece in arg.split(","):
        p = piece.strip().lower()
        if p in LAYER_ALIAS:
            out.add(LAYER_ALIAS[p])
        elif p.upper() in ("L1", "L2", "L3"):
            out.add(p.upper())
        else:
            raise SystemExit(f"未知层：{piece}（可用：memory/knowledge/session/all）")
    return out or None


def _open(cfg):
    from .store import Store

    return Store(cfgmod.resolve(cfg, cfg["index"]["db"]))


# ---------------------------------------------------------------- index
def cmd_index(args, cfg):
    redactor = Redactor(cfg)
    store = _open(cfg)
    from .embed import Embedder

    embedder = Embedder(cfg)
    known = {} if args.full else store.known_docs()
    layers = _layers(args.layer)

    icfg = cfg["index"]
    target = icfg.get("chunk_target_chars", 600)
    hard_max = icfg.get("chunk_max_chars", 1200)

    seen: set[str] = set()
    new = skipped = 0
    t0 = time.time()

    batch: list = []
    BATCH_DOCS = 64

    def flush():
        nonlocal batch, new
        if not batch:
            return
        texts = []
        for doc, pieces in batch:
            for heading, text in pieces:
                texts.append(f"{doc.title} / {heading}\n{text}" if heading else f"{doc.title}\n{text}")
        vecs = embedder.encode(texts)
        off = 0
        now = time.time()
        for doc, pieces in batch:
            store.add_doc(doc, pieces, vecs[off : off + len(pieces)], now)
            off += len(pieces)
            new += 1
        store.commit()
        batch = []

    for doc in sourcesmod.iter_documents(cfg, redactor, layers):
        seen.add(doc.doc_id)
        prev = known.get(doc.doc_id)
        if prev is not None and abs(prev - doc.mtime) < 1e-6:
            skipped += 1
            continue
        pieces = chunkmod.chunk_markdown(doc.text, target, hard_max)
        if not pieces:
            continue
        batch.append((doc, pieces))
        if len(batch) >= BATCH_DOCS:
            flush()
            print(f"  ... 已索引 {new} 篇", file=sys.stderr)
    flush()

    pruned = store.prune_missing(seen) if not layers else 0
    n_centered = store.rebuild_center()
    store.set_meta("last_index_at", str(time.time()))
    store.set_meta("center_n", str(n_centered))
    store.set_meta("embedding_model", cfg["embedding"]["model"])
    store.commit()

    print(f"索引完成：新建/更新 {new} 篇，未变跳过 {skipped} 篇，清理失效 {pruned} 篇，耗时 {time.time()-t0:.1f}s")
    print()
    print(f"{'层':<4} {'源':<16} {'文档':>6} {'块':>7}")
    for layer, source, ndocs, nchunks in store.counts_by_layer():
        print(f"{layer:<4} {source:<16} {ndocs:>6} {nchunks:>7}")
    store.close()


# --------------------------------------------------------------- recall
def _agents(raw: str | None) -> set[str] | None:
    """--agent claude,codex → {"claude", "codex"}；不传或 all 表示不过滤。"""
    if not raw or raw.strip().lower() == "all":
        return None
    return {a.strip() for a in raw.split(",") if a.strip()}


def _row_agent(row: dict) -> str:
    meta = json.loads(row["meta"] or "{}")
    return meta.get("agent", "")


def _resume_hint(cfg: dict, agent: str | None, session_id: str) -> str:
    """给会话结果配上它自己那个 agent 的 resume 命令，别对 Codex 会话打印 claude --resume。"""
    spec = (cfg.get("agents") or {}).get(agent or "")
    tpl = spec.get("resume_hint") if spec else None
    return tpl.format(session_id=session_id) if tpl else ""


def cmd_recall(args, cfg):
    from . import search as searchmod
    from .embed import Embedder

    store = _open(cfg)

    wanted = _agents(getattr(args, "agent", None))
    # 过滤发生在召回之后，先多要一些候选，免得筛完不够 k 条
    depth = args.k * 4 if wanted else args.k
    results, stats = searchmod.recall(
        store, Embedder(cfg), args.query, depth, cfg, _layers(args.layer)
    )
    if wanted:
        results = [r for r in results if _row_agent(r) in wanted][: args.k]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        store.close()
        return
    if not results:
        print(f"没有命中。（关键词候选 {stats['lexical']}，语义候选 {stats['semantic']}）")
        store.close()
        return
    print(f"# 记忆召回：{args.query}\n")
    for i, r in enumerate(results, 1):
        via = "".join(["K" if r["in_lex"] else "-", "V" if r["in_sem"] else "-"])
        meta = json.loads(r["meta"] or "{}")
        head = f"{i}. [{r['layer']}/{r['source']} {via}] {r['title']}"
        if r["heading"]:
            head += f" › {r['heading']}"
        print(head)
        loc = r["path"]
        if meta.get("session_id"):
            loc = f"session {meta['session_id']}  ({meta.get('timestamp','')})"
            hint = _resume_hint(cfg, meta.get("agent"), meta["session_id"])
            if hint:
                loc += f"  ← {hint}"
        stale = f" ⚠ {meta['status']}" if meta.get("status") in ("stale", "archived") else ""
        vat = f"  verified_at={meta['verified_at']}" if meta.get("verified_at") else ""
        print(f"   {loc}{vat}{stale}")
        body = r["text"].strip()
        if len(body) > args.width:
            body = body[: args.width] + " …"
        print("   " + body.replace("\n", "\n   "))
        print()
    print(f"（关键词候选 {stats['lexical']}，语义候选 {stats['semantic']}；K=关键词命中 V=语义命中）")
    store.close()


# ------------------------------------------------------------------ add
SLUG = re.compile(r"[^a-z0-9]+")


def cmd_add(args, cfg):
    body = sys.stdin.read().strip() if args.body is None else args.body.strip()
    if not body:
        raise SystemExit("记忆正文为空")
    name = args.name or SLUG.sub("-", args.description.lower()).strip("-")[:48]
    if not name:
        raise SystemExit("无法从 description 推出 name，请显式传 --name")
    dest_dir = cfgmod.resolve(cfg, "staging") if args.staging else cfg["_root"] / "memory" / args.type
    if args.staging:
        dest_dir = dest_dir / time.strftime("%Y%m%d-%H%M%S")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{name}.md"
    dest.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {args.description}\n"
        "metadata:\n"
        f"  type: {args.type}\n"
        f"  created: {time.strftime('%Y-%m-%d')}\n"
        "---\n\n" + body + "\n",
        encoding="utf-8",
    )
    print(f"已写入 {dest}")
    if not args.staging:
        _append_index(cfg, name, args.description, args.type)
        print("已追加 MEMORY.md 索引；记得跑 mem index")


def _append_index(cfg, name: str, desc: str, mtype: str):
    idx = cfg["_root"] / "MEMORY.md"
    line = f"- [{name}](memory/{mtype}/{name}.md) — {desc}\n"
    if idx.exists():
        cur = idx.read_text(encoding="utf-8")
        if f"memory/{mtype}/{name}.md" in cur:
            return
        idx.write_text(cur.rstrip("\n") + "\n" + line, encoding="utf-8")
    else:
        idx.write_text("# 记忆索引\n\n" + line, encoding="utf-8")




# --------------------------------------------------------- review/promote
def cmd_review(args, cfg):
    from .review import run_review

    run_review(cfg, args.limit, args.model, args.timeout, args.max_chars, args.dry_run)


def cmd_promote(args, cfg):
    from .review import run_promote

    run_promote(cfg, args.run_id)


# ------------------------------------------------------------ lint/sync/archive
def cmd_lint(args, cfg):
    from .curate import run_lint

    raise SystemExit(run_lint(cfg))


def cmd_sync(args, cfg):
    from .curate import run_sync

    raise SystemExit(run_sync(cfg, args))


def cmd_archive(args, cfg):
    from .curate import run_archive

    raise SystemExit(run_archive(cfg, args))


# --------------------------------------------------------------- export
def cmd_export(args, cfg):
    """单向导出：WSL2 是事实源，D 盘只是给 Windows Codex 的只读副本。"""
    import subprocess

    ecfg = cfg.get("export", {})
    if not ecfg.get("target"):
        raise SystemExit("没配导出目标：在 config.json 的 export.target 写一个目录")
    target = Path(ecfg["target"])
    parent = target.parent
    if not parent.exists():
        raise SystemExit(f"导出目标的上级目录不存在：{parent}")
    target.mkdir(parents=True, exist_ok=True)

    items = ecfg.get("include", ["memory", "MEMORY.md"])
    srcs = []
    for name in items:
        p = cfg["_root"] / name
        if p.exists():
            srcs.append(str(p) + ("/" if p.is_dir() else ""))
    if not srcs:
        raise SystemExit("没有可导出的内容")

    cmd = ["rsync", "-a", "--delete", "--no-perms", "--no-owner", "--no-group"]
    if args.dry_run:
        cmd.append("--dry-run")
    cmd += srcs + [str(target) + "/"]
    print(" ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise SystemExit(r.returncode)
    if not args.dry_run:
        hint = ecfg.get("readback_hint", "")
        (target / "README.md").write_text(
            "# 记忆库只读副本\n\n"
            f"事实源在 `{cfg['_root']}`，本目录由 `mem export` 单向覆盖，改这里没用。\n"
            + (f"\n检索走：\n\n```\n{hint}\n```\n" if hint else ""),
            encoding="utf-8",
        )
        print(f"已导出到 {target}")


# -------------------------------------------------------------- capture
def cmd_capture(args, cfg):
    """Stop hook 调用：把 transcript 指针记入待抽取队列。只记指针，不做语义处理。"""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    tp = payload.get("transcript_path") or ""
    sid = payload.get("session_id") or ""
    if not tp or not Path(tp).exists():
        return 0

    pending = cfg["_root"] / "sessions" / "pending.jsonl"
    pending.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    if pending.exists():
        for line in pending.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            # 同一会话只保留最新指针，多次 Stop 不重复入队
            if r.get("session_id") != sid:
                rows.append(r)
    rows.append(
        {
            "session_id": sid,
            "agent": getattr(args, "agent", None) or "claude",
            "transcript_path": tp,
            "cwd": payload.get("cwd", ""),
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "size": Path(tp).stat().st_size,
            "status": "pending",
        }
    )
    tmp = pending.with_suffix(".jsonl.tmp")
    tmp.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    tmp.replace(pending)
    return 0


# --------------------------------------------------------------- doctor
def cmd_doctor(args, cfg):
    ok = True
    print("== 位置 ==")
    print(f"  代码根  {cfg['_code_root']}")
    print(f"  数据根  {cfg['_root']}")
    print(f"  配置    {cfg['_config_path']}")


    def check(label, good, detail=""):
        nonlocal ok
        ok = ok and good
        print(f"  [{'ok ' if good else 'FAIL'}] {label}{('  ' + detail) if detail else ''}")

    print("== 运行时 ==")
    check("Python", sys.version_info[:2] >= (3, 11), sys.version.split()[0])
    check("解释器在 venv 内", ".index/.venv" in sys.executable, sys.executable)
    try:
        import numpy  # noqa: F401

        check("numpy", True, numpy.__version__)
    except ImportError:
        check("numpy", False, "未安装")
    try:
        import fastembed  # noqa: F401

        check("fastembed", True, getattr(fastembed, "__version__", "?"))
    except ImportError:
        check("fastembed", False, "未安装")

    print("== SQLite ==")
    import sqlite3

    check("版本", True, sqlite3.sqlite_version)
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
        check("FTS5 trigram", True)
    except Exception as exc:
        check("FTS5 trigram", False, str(exc))

    print("== 脱敏自测 ==")
    for sample, passed, got in Redactor(cfg).self_test():
        check(sample[:44], passed, "-> " + got[:44])

    print("== 索引源 ==")
    redactor = Redactor(cfg)
    for src in cfg["sources"]:
        base = Path(src["path"])
        n = len(list(base.glob(src.get("glob", "**/*")))) if base.exists() else 0
        # L1 是自己攒的，刚建库时为空属正常，不判失败
        good = base.exists() and (n > 0 or src["layer"] == "L1")
        note = "  (空，尚未沉淀记忆)" if n == 0 and src["layer"] == "L1" else ""
        check(f"{src['layer']}/{src['name']}", good, f"{n} 个文件  {src['path']}{note}")

    print("== 索引库 ==")
    db = cfgmod.resolve(cfg, cfg["index"]["db"])
    if db.exists():
        store = _open(cfg)
        rows = store.counts_by_layer()
        check("memory.db", bool(rows), f"{sum(r[3] for r in rows)} 块 / {db.stat().st_size//1024} KB")
        for layer, source, ndocs, nchunks in rows:
            print(f"        {layer} {source:<16} {ndocs:>5} 篇 {nchunks:>6} 块")
        err = store.get_meta("last_semantic_error")
        if err:
            print(f"        上次语义检索错误：{err}")
        store.close()
    else:
        check("memory.db", False, "尚未建库，跑 mem index --full")

    print("== 模型缓存 ==")
    cache = cfgmod.resolve(cfg, cfg["embedding"]["cache_dir"])
    n = len(list(cache.rglob("*.onnx"))) if cache.exists() else 0
    check("ONNX 模型", n > 0, f"{n} 个  {cache}")

    sys.exit(0 if ok else 1)


# ----------------------------------------------------------------- main
def main(argv=None):
    p = argparse.ArgumentParser(prog="mem", description="本地记忆库")
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    from . import setup as setupmod

    setupmod.add_parser(sub)

    pi = sub.add_parser("index", help="建立/更新索引")
    pi.add_argument("--full", action="store_true", help="忽略 mtime，全量重建")
    pi.add_argument("--layer", default=None, help="只索引某层：memory/knowledge/session")
    pi.set_defaults(func=cmd_index)

    pr = sub.add_parser("recall", help="检索记忆")
    pr.add_argument("query")
    pr.add_argument("-k", type=int, default=8)
    pr.add_argument("--layer", default=None, help="memory/knowledge/session/all")
    pr.add_argument("--agent", default=None, help="只看某个 agent 的会话：claude/codex，逗号可组合")
    pr.add_argument("--width", type=int, default=500, help="每条正文截断长度")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_recall)

    pa = sub.add_parser("add", help="新增一条记忆")
    pa.add_argument("--type", required=True, choices=["user", "feedback", "project", "reference"])
    pa.add_argument("--description", required=True)
    pa.add_argument("--name", default=None)
    pa.add_argument("--body", default=None, help="不传则从 stdin 读")
    pa.add_argument("--staging", action="store_true", help="写 staging 待审核而非正式库")
    pa.set_defaults(func=cmd_add)

    pv = sub.add_parser("review", help="把待抽取会话交给对应 agent 的 CLI 起草候选记忆")
    pv.add_argument("--limit", type=int, default=5, help="本次处理多少个会话")
    pv.add_argument("--model", default="sonnet")
    pv.add_argument("--timeout", type=int, default=180)
    pv.add_argument("--max-chars", type=int, default=24000, help="送入模型的会话摘要上限")
    pv.add_argument("--dry-run", action="store_true", help="只看会处理哪些会话，不调模型")
    pv.set_defaults(func=cmd_review)

    pp = sub.add_parser("promote", help="把审核后的 staging 批次合入正式库")
    pp.add_argument("run_id")
    pp.set_defaults(func=cmd_promote)

    ps = sub.add_parser("sync", help="写入后收口：对账 + 索引 + 报积压（--commit 才提交）")
    ps.add_argument("--commit", action="store_true", help="把记忆改动提交到本地库（不 push）")
    ps.add_argument("-m", "--message", default=None, help="提交信息")
    ps.add_argument("--no-index", action="store_true", help="跳过增量索引")
    ps.add_argument("--no-tidy", action="store_true", help="不规整待抽取队列，只报告")
    ps.add_argument("--stale-days", type=int, default=30, help="任务型记忆多久没更新就提示归档")
    ps.set_defaults(func=cmd_sync)

    pl = sub.add_parser("lint", help="只做一致性对账：文件 ↔ 索引行 ↔ frontmatter")
    pl.set_defaults(func=cmd_lint)

    par = sub.add_parser("archive", help="归档一条记忆：不再进开场注入，仍可检索")
    par.add_argument("name", nargs="?", default=None)
    par.add_argument("--list", action="store_true", help="列出已归档的记忆")
    par.add_argument("--undo", action="store_true", help="取消归档")
    par.set_defaults(func=cmd_archive)

    pe = sub.add_parser("export", help="单向导出到 D 盘供 Windows Codex 读")
    pe.add_argument("--dry-run", action="store_true")
    pe.set_defaults(func=cmd_export)

    pc = sub.add_parser("capture", help="会话结束 hook 用：从 stdin 收会话指针入队")
    pc.add_argument("--agent", default="claude", help="会话来自哪个 agent：claude/codex")
    pc.set_defaults(func=cmd_capture)

    pd = sub.add_parser("doctor", help="环境自检")
    pd.set_defaults(func=cmd_doctor)

    args = p.parse_args(argv)
    # init 是唯一在"还没有配置"时也要能跑的子命令
    cfg = cfgmod.load(args.config) if getattr(args, "needs_config", True) else None
    return args.func(args, cfg)


if __name__ == "__main__":
    main()
