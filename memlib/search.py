"""混合召回：FTS5(trigram) 关键词 + numpy 余弦语义，RRF 融合。

两条腿缺一不可：表名、任务号、类名这类精确 token 靠 trigram，
"这个接口为什么偶尔超时"这类自然语言问法靠向量。
"""
from __future__ import annotations

import re

import numpy as np

# trigram 分词器要求匹配串至少 3 字符
LATIN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-.]{2,}")
CJK = re.compile(r"[一-鿿]{2,}")
NGRAM = 3
MAX_TERMS = 12
# 命中率超过这个比例的 n-gram 没有区分度（"的问题"这类），丢掉
MAX_DF_RATIO = 0.25


def candidate_terms(q: str) -> list[str]:
    """中文按 stride=1 的 3-gram 切。

    固定 stride 会切出跨词边界的垃圾片段（"索超时"、"时重试"），
    这些在语料里几乎不出现，命中恒为 0。stride=1 才能保证
    "日志检"、"检索超"、"超时重" 这类真实词片出现在候选里。
    """
    terms: list[str] = []
    for tok in LATIN.findall(q):
        terms.append(tok)
    for run in CJK.findall(q):
        if len(run) < NGRAM:
            continue
        for i in range(len(run) - NGRAM + 1):
            terms.append(run[i : i + NGRAM])
    seen, uniq = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def build_fts_query(store, q: str) -> str:
    """按文档频率筛选候选词：丢掉零命中的和太常见的，保留最有区分度的。"""
    cands = candidate_terms(q)
    if not cands:
        return ""
    total = store.total_chunks() or 1
    scored: list[tuple[int, str]] = []
    for t in cands:
        try:
            n = store.db.execute(
                "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                ('"%s"' % t.replace('"', '""'),),
            ).fetchone()[0]
        except Exception:
            continue
        if n == 0 or n > total * MAX_DF_RATIO:
            continue
        scored.append((n, t))
    if not scored:
        return ""
    scored.sort()  # 越稀有越靠前
    picked = [t for _, t in scored[:MAX_TERMS]]
    return " OR ".join('"%s"' % t.replace('"', '""') for t in picked)


def lexical(store, query: str, depth: int, layers: set[str] | None) -> list[int]:
    fts_q = build_fts_query(store, query)
    if not fts_q:
        return []
    sql = (
        "SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id=f.rowid"
        " WHERE chunks_fts MATCH ?"
    )
    params: list = [fts_q]
    if layers:
        sql += " AND c.layer IN (%s)" % ",".join("?" * len(layers))
        params += list(layers)
    sql += " ORDER BY bm25(chunks_fts) LIMIT ?"
    params.append(depth)
    try:
        return [r["id"] for r in store.db.execute(sql, tuple(params))]
    except Exception:
        return []


def semantic_scored(
    store, qvec: np.ndarray, depth: int, layers: set[str] | None
) -> list[tuple[int, float]]:
    """去均值后再算余弦，返回 (chunk_id, 相似度)。

    bge 系列各向异性很强：不做处理时全库相似度挤在 0.64~0.72 的窄带，
    top-1 和 top-100 只差 0.05，真正相关的文档排不上来。减去全局均值
    能把这个窄带拉开。

    带回相似度是给分层配额用的：向量腿永远会返回 depth 条，哪怕全不相关，
    所以配额席位要靠分数卡一道，不能只看名次。
    """
    ids, mat = store.load_matrix(layers)
    if ids.size == 0 or mat.shape[1] != qvec.shape[0]:
        return []
    center = store.get_center(mat.shape[1])
    if center is not None:
        mat = _l2(mat - center)
        qvec = _l2((qvec - center)[None, :])[0]
    scores = mat @ qvec
    top = np.argsort(-scores)[:depth]
    return [(int(ids[i]), float(scores[i])) for i in top]


def semantic(store, qvec: np.ndarray, depth: int, layers: set[str] | None) -> list[int]:
    return [cid for cid, _ in semantic_scored(store, qvec, depth, layers)]


def _l2(arr: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    np.maximum(norms, 1e-12, out=norms)
    return arr / norms


def rrf(rankings: list[list[int]], k: int = 60) -> list[tuple[int, float]]:
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(fused.items(), key=lambda kv: -kv[1])


def _leg(store, query: str, qvec, depth: int, layers: set[str] | None, rrf_k: int):
    """跑两条腿并融合，返回 (融合后的 id 序列, 关键词命中集, 语义相似度表)。"""
    lex = lexical(store, query, depth, layers)
    sem = semantic_scored(store, qvec, depth, layers) if qvec is not None else []
    fused = rrf([lex, [cid for cid, _ in sem]], rrf_k)
    return [cid for cid, _ in fused], set(lex), dict(sem)


def recall(store, embedder, query: str, k: int, cfg: dict, layers: set[str] | None = None):
    """全库融合 + 分层配额。

    L1 只占全库不到 1% 的块，等权 RRF 下几乎永远排不进 top-k——一条精确的记忆
    会被几十条沾边的历史会话压掉。所以用户没限定层时，给 L1/L2 各留几个席位，
    剩下的名额仍按融合分填。配额席位要么是关键词命中，要么语义分过阈值，
    避免为了凑数塞进无关条目。
    """
    icfg = cfg["index"]
    rcfg = cfg.get("recall", {})
    rrf_k = icfg.get("rrf_k", 60)
    depth = icfg.get("candidate_depth", 30)
    max_per_doc = int(rcfg.get("max_chunks_per_doc", 2) or 0)
    min_sim = float(rcfg.get("quota_min_sim", 0.30))
    # 限定了层就是用户自己在挑，不再叠加配额
    quota = {} if layers else dict(rcfg.get("layer_quota", {}))

    try:
        qvec = embedder.encode_query(query)
    except Exception as exc:  # 模型缺失时退化为纯关键词，不让检索整体失败
        qvec = None
        store.set_meta("last_semantic_error", str(exc)[:500])
        store.commit()

    ranked, lex_hits, sem_hits = _leg(store, query, qvec, depth, layers, rrf_k)
    per_layer: dict[str, list[int]] = {}
    for layer, n in quota.items():
        if n <= 0:
            continue
        ids, lex_l, sem_l = _leg(store, query, qvec, depth, {layer}, rrf_k)
        lex_hits |= lex_l
        sem_hits.update(sem_l)
        per_layer[layer] = ids

    rows = store.get_chunks(sorted({cid for ids in per_layer.values() for cid in ids} | set(ranked)))
    order = {cid: i for i, cid in enumerate(ranked)}
    seen_docs: dict[str, int] = {}
    picked: list[int] = []

    def take(cid: int, quota_seat: bool) -> bool:
        if len(picked) >= k or cid in picked:
            return False
        r = rows.get(cid)
        if r is None:
            return False
        if quota_seat and cid not in lex_hits and sem_hits.get(cid, -1.0) < min_sim:
            return False
        doc = r["doc_id"]
        # 配额席位每篇只给一个块：留着的名额是用来多露一条记忆的，不是同一篇刷屏
        cap = 1 if quota_seat else max_per_doc
        if cap and seen_docs.get(doc, 0) >= cap:
            return False
        seen_docs[doc] = seen_docs.get(doc, 0) + 1
        picked.append(cid)
        return True

    for layer in sorted(per_layer):
        got = 0
        for cid in per_layer[layer]:
            if got >= quota[layer]:
                break
            if take(cid, True):
                got += 1
    for cid in ranked:
        take(cid, False)

    out = []
    for cid in picked:
        r = rows[cid]
        out.append(
            {
                "score": 1.0 / (rrf_k + order.get(cid, len(ranked)) + 1),
                "layer": r["layer"],
                "source": r["source"],
                "path": r["source_path"],
                "title": r["title"],
                "heading": r["heading_path"],
                "text": r["text"],
                "meta": r["meta_json"],
                "in_lex": cid in lex_hits,
                "in_sem": cid in sem_hits,
            }
        )
    return out, {"lexical": len(lex_hits), "semantic": len(sem_hits)}
