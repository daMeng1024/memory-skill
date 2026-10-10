"""本地看板：块数量、占用、关键词、检索，以及人工审核。

`mem web` 起一个只监听 127.0.0.1 的 HTTP 服务，页面是同目录的 web.html。
数据库以 mode=ro 打开，看板自己从不写库；`mem index` 同时在写时读到的是 WAL 快照。

审核页的写操作（转正 / 归档 / 合入 / sync）一律转调 `mem` 子命令，不在这里重写
一遍业务逻辑；唯一自己做的写是把 staging 候选在批次目录和它的 rejected/ 之间挪动。

模块顶层只 import 标准库：main() 每条子命令都会 import 本模块来注册参数，
而 `mem init` 要能在还没建 venv 的裸 python 上跑。
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config as cfgmod

PAGE = Path(__file__).with_name("web.html")
# 监听非回环地址时，只放行这些来源：本机 + Tailscale（CGNAT 100.64/10 与它的 IPv6 段）。
# 办公局域网同样能连到 0.0.0.0，但这里是全部记忆原文外加写操作，不能对它开放
DEFAULT_ALLOW = ("127.0.0.0/8", "::1/128", "100.64.0.0/10", "fd7a:115c:a1e0::/48")
LAYERS = ("L1", "L2", "L3")

# 库内各表按用途归组，页面上看「向量占多少、倒排占多少」比看表名直观
TABLE_GROUPS = (
    ("vectors", "向量"),
    ("meta_vec", "向量"),
    ("chunks_fts", "关键词倒排"),
    ("chunks", "正文"),
    ("docs", "文档表"),
    ("idx_", "B 树索引"),
    ("sqlite_autoindex", "B 树索引"),
)

# 热词统计。库里的倒排是 trigram 三字切片，不是分词，中文这里只能按 n-gram 近似
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
CJK_RUN = re.compile(r"[一-鿿]+")
STOP_EN = frozenset(
    """the and for with this that from are not you was were has have had but
    all any can will would should could into out use used using its our your
    true false null none http https www com org html json md txt
    """.split()
)
# 含这些字的片段基本是虚词粘连（"的问题"、"了一下"），直接丢
CJK_DROP_ANY = frozenset("的了吗呢吧啊么着")
CJK_DROP_EDGE = frozenset("是在和也就都这那个们把被")
HOT_KINDS = ("word", "cjk2", "cjk3")


def _cjk_ok(g: str) -> bool:
    if any(ch in CJK_DROP_ANY for ch in g):
        return False
    return g[0] not in CJK_DROP_EDGE and g[-1] not in CJK_DROP_EDGE


def _dir_bytes(path: Path) -> int | None:
    if not path.exists():
        return None
    total = 0
    for base, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(base, f)).st_size
            except OSError:
                pass
    return total


def _file_bytes(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


class State:
    """一个服务进程只有一份：只读连接、嵌入模型、各类缓存。

    连接按线程收发（一个慢客户端不能拖住别人），但接口处理都在 self.lock 里串行：
    sqlite 连接、模型和缓存都不是线程安全的，单人用也不需要并发处理。
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.root: Path = cfg["_root"]
        self.db_path = cfgmod.resolve(cfg, cfg["index"]["db"])
        if not self.db_path.is_file():
            raise SystemExit(f"索引库不存在：{self.db_path}（先跑 mem index --full）")
        self.store = _ro_store(self.db_path)
        self.lock = threading.Lock()
        self._embedder = None
        self._hot: dict = {}
        self._dirs: tuple[float, dict] | None = None

    @property
    def embedder(self):
        if self._embedder is None:
            from .embed import Embedder

            self._embedder = Embedder(self.cfg)
        return self._embedder

    def stamp(self) -> str | None:
        return self.store.get_meta("last_index_at")

    def dir_sizes(self) -> dict:
        # 模型和 venv 文件多，遍历一次要几百毫秒，一分钟内复用
        now = time.time()
        if self._dirs and now - self._dirs[0] < 60:
            return self._dirs[1]
        ecfg = self.cfg["embedding"]
        sizes = {
            "models": _dir_bytes(cfgmod.resolve(self.cfg, ecfg.get("cache_dir", ".index/models"))),
            "venv": _dir_bytes(self.root / ".index" / ".venv"),
            "memory": _dir_bytes(self.root / "memory"),
            "staging": _dir_bytes(self.root / "staging"),
        }
        self._dirs = (now, sizes)
        return sizes


def _ro_store(db_path: Path):
    from .store import Store

    class ReadOnlyStore(Store):
        """复用 Store 的读方法。构造时不建表、不切 WAL，写方法置空。

        search.recall 在模型缺失时会 set_meta 记一笔错误，只读连接上那会直接抛错，
        所以这里吞掉；看板上的错误改由接口返回。
        """

        def __init__(self, path: Path):
            # 跨线程使用由 State.lock 保证串行
            self.db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self._mat: dict = {}
            self._mat_stamp = None

        def set_meta(self, k, v):
            pass

        def commit(self):
            pass

        def load_matrix(self, layers=None):
            # 每次检索都从 SQLite 读一遍全部向量（几十 MB）太慢；索引更新前一直复用
            stamp = self.get_meta("last_index_at")
            if stamp != self._mat_stamp:
                self._mat.clear()
                self._mat_stamp = stamp
            key = tuple(sorted(layers)) if layers else ()
            if key not in self._mat:
                self._mat[key] = super().load_matrix(layers)
            return self._mat[key]

    return ReadOnlyStore(db_path)


def _layer_set(raw: str | None) -> set[str] | None:
    if not raw or raw == "all":
        return None
    picked = {p.strip().upper() for p in raw.split(",")} & set(LAYERS)
    return picked or None


def _scope_filter(scope: str | None) -> tuple[str, tuple]:
    """scope 可以是层（L1/L2/L3）或来源名，返回 SQL 片段和参数。"""
    if not scope or scope == "all":
        return "", ()
    if scope in LAYERS:
        return " WHERE layer=?", (scope,)
    return " WHERE source=?", (scope,)


# ---------------------------------------------------------------- 概览
def api_overview(st: State, q: dict) -> dict:
    db = st.store.db
    by_source = [
        dict(r)
        for r in db.execute(
            "SELECT layer, source, COUNT(DISTINCT doc_id) docs, COUNT(*) chunks,"
            " SUM(length(text)) chars, CAST(AVG(length(text)) AS INT) avg_len,"
            " MAX(length(text)) max_len"
            " FROM chunks GROUP BY layer, source ORDER BY layer, chunks DESC"
        )
    ]
    hist = [
        {"from": r[0], "n": r[1]}
        for r in db.execute(
            "SELECT (length(text)/100)*100 b, COUNT(*) FROM chunks GROUP BY b ORDER BY b"
        )
    ]

    groups: Counter = Counter()
    try:
        for name, size in db.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"):
            label = next((g for p, g in TABLE_GROUPS if name.startswith(p)), "其他")
            groups[label] += size
    except sqlite3.Error:
        pass  # 编译时没带 dbstat 的 sqlite 只给文件级大小

    stamp = st.stamp()
    meta = {r["k"]: r["v"] for r in db.execute("SELECT k, v FROM meta")}
    return {
        "db_path": str(st.db_path),
        "files": {
            "db": _file_bytes(st.db_path),
            "wal": _file_bytes(st.db_path.with_name(st.db_path.name + "-wal")),
            **st.dir_sizes(),
        },
        "tables": [{"group": g, "bytes": n} for g, n in groups.most_common()],
        "by_source": by_source,
        "hist": hist,
        "totals": {
            "chunks": sum(r["chunks"] for r in by_source),
            "docs": sum(r["docs"] for r in by_source),
            "chars": sum(r["chars"] or 0 for r in by_source),
        },
        "last_index_at": float(stamp) if stamp else None,
        "embedding_model": meta.get("embedding_model") or st.cfg["embedding"].get("model"),
        "last_semantic_error": meta.get("last_semantic_error"),
        "backlog": _backlog(st.root),
        "memory_types": Counter(m["type"] for m in _memory_items(st.root)),
    }


def _backlog(root: Path) -> dict:
    staging = root / "staging"
    runs = [d for d in staging.iterdir() if d.is_dir()] if staging.is_dir() else []
    queue: Counter = Counter()
    pending = root / "sessions" / "pending.jsonl"
    if pending.is_file():
        for line in pending.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                queue[json.loads(line).get("status") or "?"] += 1
            except ValueError:
                queue["坏行"] += 1
    return {
        "staging_runs": len(runs),
        "staging_files": sum(len(list(d.rglob("*.md"))) for d in runs),
        "queue": dict(queue.most_common()),
    }


# ---------------------------------------------------------------- 检索
def api_search(st: State, q: dict) -> dict:
    from . import search as searchmod

    text = (q.get("q") or "").strip()
    if not text:
        return {"items": []}
    mode = q.get("mode") or "hybrid"
    k = max(1, min(int(q.get("k") or 10), 100))
    layers = _layer_set(q.get("layer"))
    store = st.store
    t0 = time.perf_counter()
    out = {
        "mode": mode,
        "fts_query": searchmod.build_fts_query(store, text),
        "items": [],
    }

    if mode == "hybrid":
        try:
            results, stats = searchmod.recall(store, st.embedder, text, k, st.cfg, layers)
        except Exception as exc:  # 召回内部异常也要让页面看到原因
            out["error"] = str(exc)
            return out
        out["stats"] = stats
        for r in results:
            hit = store.db.execute(
                "SELECT id, doc_id FROM chunks WHERE source_path=? AND text=? LIMIT 1",
                (r["path"], r["text"]),
            ).fetchone()
            out["items"].append(
                _item(r, hit["id"] if hit else None, hit["doc_id"] if hit else None,
                      via="".join(["K" if r["in_lex"] else "", "V" if r["in_sem"] else ""]))
            )
    elif mode == "keyword":
        ids = searchmod.lexical(store, text, k, layers)
        rows = store.get_chunks(ids)
        out["items"] = [_row_item(rows[i], via="K") for i in ids if i in rows]
    elif mode == "semantic":
        try:
            qvec = st.embedder.encode_query(text)
        except Exception as exc:
            out["error"] = f"嵌入模型不可用：{exc}"
            return out
        scored = searchmod.semantic_scored(store, qvec, k, layers)
        rows = store.get_chunks([cid for cid, _ in scored])
        out["items"] = [
            _row_item(rows[cid], via="V", sim=round(sim, 4)) for cid, sim in scored if cid in rows
        ]
    else:
        out["error"] = f"未知模式：{mode}"
    out["ms"] = round((time.perf_counter() - t0) * 1000)
    return out


def _item(r: dict, cid, doc_id, via: str, sim: float | None = None) -> dict:
    try:
        meta = json.loads(r.get("meta") or "{}")
    except ValueError:
        meta = {}
    return {
        "id": cid,
        "doc_id": doc_id,
        "layer": r["layer"],
        "source": r["source"],
        "path": r["path"],
        "title": r["title"],
        "heading": r["heading"],
        "text": r["text"],
        "meta": meta,
        "via": via,
        "sim": sim,
    }


def _row_item(row: sqlite3.Row, via: str, sim: float | None = None) -> dict:
    r = {
        "layer": row["layer"],
        "source": row["source"],
        "path": row["source_path"],
        "title": row["title"],
        "heading": row["heading_path"],
        "text": row["text"],
        "meta": row["meta_json"],
    }
    return _item(r, row["id"], row["doc_id"], via, sim)


# ---------------------------------------------------------------- 关键词
def api_term(st: State, q: dict) -> dict:
    """一个词在库里的命中分布，以及检索的关键词腿会怎么拆它、丢掉哪些片段。"""
    from . import search as searchmod

    term = (q.get("t") or "").strip()
    if not term:
        return {}
    db = st.store.db
    total = st.store.total_chunks() or 1
    like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    dist = [
        dict(r)
        for r in db.execute(
            "SELECT layer, source, COUNT(*) chunks, COUNT(DISTINCT doc_id) docs FROM chunks"
            " WHERE text LIKE ? ESCAPE '\\' GROUP BY layer, source ORDER BY layer, chunks DESC",
            (like,),
        )
    ]

    pieces = []
    for p in searchmod.candidate_terms(term):
        try:
            n = db.execute(
                "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                ('"%s"' % p.replace('"', '""'),),
            ).fetchone()[0]
        except sqlite3.Error:
            n = 0
        if n == 0:
            status = "zero"
        elif n > total * searchmod.MAX_DF_RATIO:
            status = "common"
        else:
            status = "kept"
        pieces.append({"term": p, "chunks": n, "ratio": n / total, "status": status})
    return {
        "term": term,
        "total": total,
        "hits": sum(r["chunks"] for r in dist),
        "dist": dist,
        "pieces": pieces,
        "fts_query": searchmod.build_fts_query(st.store, term),
        "max_df_ratio": searchmod.MAX_DF_RATIO,
        "max_terms": searchmod.MAX_TERMS,
    }


def api_hotwords(st: State, q: dict) -> dict:
    """按块的文档频率统计高频词：一个词在多少个块里出现过，而不是出现了多少次。"""
    scope = q.get("scope") or "all"
    kind = q.get("kind") if q.get("kind") in HOT_KINDS else "word"
    limit = max(10, min(int(q.get("limit") or 100), 500))
    key = (st.stamp(), scope, kind)
    if key not in st._hot:
        t0 = time.perf_counter()
        where, params = _scope_filter(scope)
        df: Counter = Counter()
        n_chunks = 0
        for (text,) in st.store.db.execute("SELECT text FROM chunks" + where, params):
            n_chunks += 1
            df.update(_grams(text, kind))
        st._hot[key] = (n_chunks, df, round((time.perf_counter() - t0) * 1000))
    n_chunks, df, ms = st._hot[key]
    # 和检索关键词腿同一把尺子：命中超过 MAX_DF_RATIO 的片段检索时会被丢掉，没有区分度
    ratio = _max_df_ratio()
    words = [
        {"w": w, "df": n, "common": n > n_chunks * ratio}
        for w, n in df.most_common(limit + sum(1 for v in df.values() if v > n_chunks * ratio))
    ]
    if q.get("hide_common") == "1":
        words = [w for w in words if not w["common"]]
    return {
        "scope": scope,
        "kind": kind,
        "chunks": n_chunks,
        "ms": ms,
        "max_df_ratio": ratio,
        "n_common": sum(1 for v in df.values() if v > n_chunks * ratio),
        "words": words[:limit],
    }


def _max_df_ratio() -> float:
    from . import search as searchmod

    return searchmod.MAX_DF_RATIO


def _grams(text: str, kind: str) -> set[str]:
    if kind == "word":
        return {w for w in WORD.findall(text) if w.lower() not in STOP_EN and not w.isdigit()}
    n = 2 if kind == "cjk2" else 3
    out = set()
    for run in CJK_RUN.findall(text):
        for i in range(len(run) - n + 1):
            g = run[i : i + n]
            if _cjk_ok(g):
                out.add(g)
    return out


# ---------------------------------------------------------------- 记忆 / 文档
def _memory_items(root: Path) -> list[dict]:
    from .curate import memory_files, read_frontmatter

    items = []
    for f in memory_files(root):
        m = read_frontmatter(f)
        items.append(
            {
                "name": m.get("name") or f.stem,
                "description": m.get("description") or "",
                "type": m.get("metadata.type") or f.parent.name,
                "created": m.get("metadata.created") or m.get("created") or "",
                "status": m.get("status") or "",
                "verified_at": m.get("verified_at") or m.get("metadata.verified_at") or "",
                "path": str(f),
                "bytes": f.stat().st_size,
                "mtime": f.stat().st_mtime,
            }
        )
    return items


def api_memories(st: State, q: dict) -> dict:
    from .curate import read_frontmatter

    items = _memory_items(st.root)
    indexed = {
        r["source_path"]: r["n_chunks"]
        for r in st.store.db.execute("SELECT source_path, n_chunks FROM docs WHERE source='memory'")
    }
    for it in items:
        it["chunks"] = indexed.get(it["path"])  # None = 还没进索引，要跑 mem sync

    staging = []
    sdir = st.root / "staging"
    if sdir.is_dir():
        for run in sorted((d for d in sdir.iterdir() if d.is_dir()), reverse=True):
            for f in sorted(run.rglob("*.md")):
                m = read_frontmatter(f)
                staging.append(
                    {
                        "run": run.name,
                        "file": str(f.relative_to(run)),
                        "name": m.get("name") or f.stem,
                        "description": m.get("description") or "",
                        "type": m.get("metadata.type") or f.stem.split("__", 1)[0],
                        "status": m.get("status") or "",
                    }
                )
    return {"items": items, "staging": staging}


def api_doc(st: State, q: dict) -> dict:
    db = st.store.db
    doc_id = q.get("id") or ""
    doc = db.execute("SELECT * FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
    if doc is None:
        return {"error": f"索引里没有这个文档：{doc_id}"}
    chunks = db.execute(
        "SELECT id, ord, heading_path, text, title, meta_json FROM chunks WHERE doc_id=? ORDER BY ord",
        (doc_id,),
    ).fetchall()
    first = chunks[0] if chunks else None
    try:
        meta = json.loads(first["meta_json"] or "{}") if first else {}
    except ValueError:
        meta = {}
    return {
        "doc": dict(doc),
        "title": first["title"] if first else "",
        "meta": meta,
        "chunks": [
            {"id": c["id"], "ord": c["ord"], "heading": c["heading_path"], "text": c["text"]}
            for c in chunks
        ],
    }


# ---------------------------------------------------------------- 审核
RUN_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]+$")
STALE_DAYS = 30


def _safe_name(name: str) -> str:
    """记忆名 / 文件名只能是单段，不能带路径分隔符，也不能被当成命令行选项。"""
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith(("-", ".")) or "\x00" in name:
        raise ValueError(f"非法名称：{name!r}")
    return name


def _run_dir(st: State, run: str) -> Path:
    if not RUN_RE.match(run or ""):
        raise ValueError(f"非法批次号：{run!r}")
    d = st.root / "staging" / run
    if not d.is_dir():
        raise ValueError(f"批次不存在：{run}")
    return d


def _candidate(f: Path, run: str) -> dict:
    from .curate import read_frontmatter

    m = read_frontmatter(f)
    return {
        "run": run,
        "file": f.name,
        "name": m.get("name") or f.stem.split("__", 1)[-1],
        "description": m.get("description") or "",
        "type": m.get("metadata.type") or f.stem.split("__", 1)[0],
        "created": m.get("metadata.created") or "",
        "from_session": m.get("metadata.from_session") or "",
        "body": f.read_text(encoding="utf-8", errors="replace"),
        "mtime": f.stat().st_mtime,
    }


def api_review(st: State, q: dict) -> dict:
    from .curate import lint, memory_files, read_frontmatter

    root = st.root
    runs = []
    sdir = root / "staging"
    if sdir.is_dir():
        for d in sorted((d for d in sdir.iterdir() if d.is_dir()), reverse=True):
            runs.append(
                {
                    "run": d.name,
                    "candidates": [_candidate(f, d.name) for f in sorted(d.glob("*.md"))],
                    "rejected": [_candidate(f, d.name) for f in sorted((d / "rejected").glob("*.md"))],
                }
            )

    auto, stale = [], []
    cutoff = time.time() - STALE_DAYS * 86400
    for f in memory_files(root):
        m = read_frontmatter(f)
        status = m.get("status") or ""
        if status == "auto":
            try:
                evidence = json.loads(m.get("evidence") or "[]")
            except ValueError:
                evidence = [m.get("evidence")]
            auto.append(
                {
                    "name": f.stem,
                    "type": f.parent.name,
                    "description": m.get("description") or "",
                    "verified_at": m.get("verified_at") or "",
                    "evidence": evidence,
                    "body": f.read_text(encoding="utf-8", errors="replace"),
                    "path": str(f),
                }
            )
        # 判据和 mem sync 的「过期候选」一致：task- 开头、超期未改、没归档
        if f.stem.startswith("task-") and f.stat().st_mtime < cutoff and status != "archived":
            stale.append(
                {
                    "name": f.stem,
                    "type": f.parent.name,
                    "description": m.get("description") or "",
                    "mtime": f.stat().st_mtime,
                    "path": str(f),
                }
            )

    git = None
    if (root / ".git").exists():
        r = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--", "memory", "MEMORY.md"],
            capture_output=True, text=True, timeout=20,
        )
        git = r.stdout.splitlines() if r.returncode == 0 else [f"git 不可用：{r.stderr.strip()[:200]}"]

    return {
        "runs": runs,
        "auto": auto,
        "stale": stale,
        "stale_days": STALE_DAYS,
        "lint": lint(st.cfg),
        "git": git,
    }


def _mem(st: State, *args: str, timeout: int = 600) -> dict:
    """转调 mem CLI：和终端里敲命令完全同一条路径，含它的校验和提示。"""
    exe = st.cfg["_code_root"] / "bin" / "mem"
    env = dict(os.environ, MEM_HOME=str(st.root))
    t0 = time.perf_counter()
    try:
        r = subprocess.run(
            [str(exe), *args], capture_output=True, text=True, timeout=timeout,
            env=env, cwd=str(st.root),
        )
        out, code = (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).strip(), r.returncode
    except subprocess.TimeoutExpired:
        out, code = f"超时（{timeout}s）", -1
    return {
        "ok": code == 0,
        "code": code,
        "cmd": "mem " + " ".join(args),
        "output": out,
        "ms": round((time.perf_counter() - t0) * 1000),
    }


def _move(src: Path, dest: Path, cmd: str) -> dict:
    if not src.is_file():
        raise ValueError(f"文件不存在：{src.name}")
    if dest.exists():
        raise ValueError(f"目标已存在：{dest}")
    dest.parent.mkdir(exist_ok=True)
    shutil.move(str(src), str(dest))
    return {"ok": True, "code": 0, "cmd": cmd, "output": f"{src} → {dest}", "ms": 0}


def api_action(st: State, body: dict) -> dict:
    act = body.get("action")
    if act == "approve":
        return _mem(st, "approve", _safe_name(body.get("name")))
    if act == "archive":
        return _mem(st, "archive", _safe_name(body.get("name")))
    if act == "promote":
        _run_dir(st, body.get("run"))
        return _mem(st, "promote", body["run"])
    if act == "audit_dry":
        _run_dir(st, body.get("run"))
        return _mem(st, "audit", body["run"], "--dry-run")
    if act == "sync":
        return _mem(st, "sync")
    if act in ("reject", "restore"):
        d = _run_dir(st, body.get("run"))
        name = _safe_name(body.get("file"))
        if not name.endswith(".md"):
            raise ValueError("只能移动 .md 候选")
        if act == "reject":
            return _move(d / name, d / "rejected" / name, f"拒绝 staging/{d.name}/{name}")
        return _move(d / "rejected" / name, d / name, f"捞回 staging/{d.name}/rejected/{name}")
    raise ValueError(f"未知操作：{act!r}")


ROUTES = {
    "/api/overview": api_overview,
    "/api/search": api_search,
    "/api/term": api_term,
    "/api/hotwords": api_hotwords,
    "/api/memories": api_memories,
    "/api/doc": api_doc,
    "/api/review": api_review,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "mem-web"
    timeout = 30  # 半开 / 不发完请求的连接 30 秒后断开

    def _local(self) -> bool:
        """来源 IP 在白名单网段，且 Host 头可信。

        Host 只认字面 IP、localhost 和 --allow-host 显式给的名字（如 MagicDNS 名）：
        挡住 DNS rebinding——外部网页把自己的域名解析到本机地址后就能跨站读接口，
        而 rebinding 只能借域名，借不了字面 IP。
        """
        try:
            client = ipaddress.ip_address(self.client_address[0].removeprefix("::ffff:"))
        except ValueError:
            return False
        if not any(client in net for net in self.server.allow_nets):
            return False
        return _trusted_host(self.headers.get("Host") or "", self.server.allow_hosts)

    def do_POST(self):
        # 写操作再加两道：自定义头让跨站请求必须先过 CORS 预检（本服务不应答预检），
        # Origin 带了就必须是本页
        origin = self.headers.get("Origin")
        if (
            not self._local()
            or self.headers.get("X-Mem-Web") != "1"
            or (origin and origin != f"http://{self.headers.get('Host')}")
        ):
            return self._send(403, b"forbidden", "text/plain; charset=utf-8")
        if urlparse(self.path).path != "/api/action":
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        body: dict = {}
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
            with self.server.state.lock:
                res, code = api_action(self.server.state, body), 200
        except ValueError as exc:
            res, code = {"ok": False, "error": str(exc)}, 400
        except Exception as exc:
            res, code = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500
        act = body.get("action") if isinstance(body, dict) else "?"
        print(f"[action] {act} → {res.get('cmd', '')} ok={res.get('ok')} {res.get('error', '')}", flush=True)
        data = json.dumps(res, ensure_ascii=False).encode("utf-8")
        self._send(code, data, "application/json; charset=utf-8")

    def do_GET(self):
        if not self._local():
            return self._send(403, b"forbidden", "text/plain; charset=utf-8")
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        handler = ROUTES.get(url.path)
        if handler is None:
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        params = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            with self.server.state.lock:
                body, code = handler(self.server.state, params), 200
        except Exception as exc:
            body, code = {"error": f"{type(exc).__name__}: {exc}"}, 500
        data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, data, "application/json; charset=utf-8")

    def _send(self, code: int, data: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


def _trusted_host(raw: str, allow_hosts: set[str]) -> bool:
    host = raw.strip().lower()
    if host.startswith("["):  # [::1]:8770
        host = host[1 : host.find("]")] if "]" in host else ""
    else:
        host = host.rsplit(":", 1)[0]
    if host == "localhost" or host in allow_hosts:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _local_ips(nets) -> list[str]:
    """列出落在白名单网段里的本机 IPv4，只用于启动时打印访问地址。"""
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    ips = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            ip = ipaddress.ip_address(parts[3].split("/")[0])
            if any(ip in n for n in nets) and not ip.is_loopback:
                ips.append(f"{parts[1]} {ip}")
    return ips


def run_web(args, cfg) -> None:
    try:
        nets = [ipaddress.ip_network(c, strict=False) for c in (args.allow or DEFAULT_ALLOW)]
    except ValueError as exc:
        raise SystemExit(f"--allow 不是合法网段：{exc}")
    state = State(cfg)
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
        httpd.daemon_threads = True
    except OSError as exc:
        raise SystemExit(f"{args.host}:{args.port} 起不来：{exc}（换端口：mem web --port N）")
    httpd.state = state
    httpd.allow_nets = nets
    httpd.allow_hosts = {h.strip().lower() for h in args.allow_host or [] if h.strip()}
    print(f"记忆库看板：监听 {args.host}:{args.port}（Ctrl+C 退出）", flush=True)
    print("放行来源：" + "、".join(str(n) for n in nets), flush=True)
    if args.host in ("127.0.0.1", "localhost"):
        print(f"  http://127.0.0.1:{args.port}", flush=True)
    else:
        print(f"  http://127.0.0.1:{args.port}", flush=True)
        for item in _local_ips(nets) if args.host in ("0.0.0.0", "") else [f"- {args.host}"]:
            dev, ip = item.split(" ", 1)
            print(f"  http://{ip}:{args.port}  ({dev})" if dev != "-" else f"  http://{ip}:{args.port}", flush=True)
        for h in sorted(httpd.allow_hosts):
            print(f"  http://{h}:{args.port}", flush=True)
        print("  注意：页面含全部记忆原文且能执行写操作，白名单网段里的任何设备都能访问", flush=True)
    print(f"索引库：{state.db_path}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        state.store.close()


def add_parser(sub) -> None:
    p = sub.add_parser("web", help="本地看板：块数量、占用、关键词、检索、人工审核")
    p.add_argument("--port", type=int, default=8770, help="监听端口")
    p.add_argument(
        "--host", default="127.0.0.1",
        help="监听地址，默认只本机；0.0.0.0 = 所有网卡，但仍只放行 --allow 网段",
    )
    p.add_argument(
        "--allow", action="append", default=None, metavar="CIDR",
        help="放行的来源网段，可重复；默认本机 + Tailscale（100.64.0.0/10、fd7a:115c:a1e0::/48）",
    )
    p.add_argument(
        "--allow-host", action="append", default=None, metavar="NAME",
        help="额外信任的 Host 名（如 Tailscale MagicDNS 名），字面 IP 和 localhost 默认信任",
    )
    p.set_defaults(func=run_web)
