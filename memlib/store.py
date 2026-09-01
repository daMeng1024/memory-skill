"""SQLite 存储：chunks + FTS5(trigram) + 向量 BLOB。

向量检索用 numpy 暴力余弦，不引 sqlite-vec —— 这个量级下多一个 C 扩展
只增加兼容风险，换不到收益。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY,
    doc_id       TEXT NOT NULL,
    source       TEXT NOT NULL,
    layer        TEXT NOT NULL,
    source_path  TEXT NOT NULL,
    title        TEXT,
    heading_path TEXT,
    text         TEXT NOT NULL,
    ord          INTEGER NOT NULL,
    meta_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_layer ON chunks(layer);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
    USING fts5(text, content='chunks', content_rowid='id', tokenize='trigram');

CREATE TABLE IF NOT EXISTS vectors (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    dim      INTEGER NOT NULL,
    vec      BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS docs (
    doc_id      TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    source      TEXT NOT NULL,
    layer       TEXT NOT NULL,
    mtime       REAL NOT NULL,
    n_chunks    INTEGER NOT NULL,
    indexed_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS meta_vec (k TEXT PRIMARY KEY, vec BLOB NOT NULL);
"""


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # ---- 增量判断 -------------------------------------------------
    def known_docs(self) -> dict[str, float]:
        return {r["doc_id"]: r["mtime"] for r in self.db.execute("SELECT doc_id, mtime FROM docs")}

    def drop_doc(self, doc_id: str):
        rows = [r["id"] for r in self.db.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
        for cid in rows:
            self.db.execute("INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', ?, (SELECT text FROM chunks WHERE id=?))", (cid, cid))
        self.db.execute("DELETE FROM vectors WHERE chunk_id IN (SELECT id FROM chunks WHERE doc_id=?)", (doc_id,))
        self.db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        self.db.execute("DELETE FROM docs WHERE doc_id=?", (doc_id,))

    def prune_missing(self, seen: set[str]) -> int:
        gone = [d for d in self.known_docs() if d not in seen]
        for doc_id in gone:
            self.drop_doc(doc_id)
        return len(gone)

    # ---- 写入 -----------------------------------------------------
    def add_doc(self, doc, pieces: list[tuple[str, str]], vecs: np.ndarray, now: float):
        self.drop_doc(doc.doc_id)
        meta = json.dumps(doc.meta, ensure_ascii=False)
        for i, ((heading, text), vec) in enumerate(zip(pieces, vecs)):
            cur = self.db.execute(
                "INSERT INTO chunks(doc_id, source, layer, source_path, title, heading_path, text, ord, meta_json)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (doc.doc_id, doc.source, doc.layer, doc.path, doc.title, heading, text, i, meta),
            )
            cid = cur.lastrowid
            self.db.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?,?)", (cid, text))
            self.db.execute(
                "INSERT INTO vectors(chunk_id, dim, vec) VALUES(?,?,?)",
                (cid, int(vec.shape[0]), vec.astype(np.float32).tobytes()),
            )
        self.db.execute(
            "INSERT OR REPLACE INTO docs(doc_id, source_path, source, layer, mtime, n_chunks, indexed_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (doc.doc_id, doc.path, doc.source, doc.layer, doc.mtime, len(pieces), now),
        )

    def commit(self):
        self.db.commit()

    def set_meta(self, k: str, v: str):
        self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v))

    def get_meta(self, k: str) -> str | None:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else None

    # ---- 读取 -----------------------------------------------------

    def total_chunks(self) -> int:
        return self.db.execute("SELECT count(*) FROM chunks").fetchone()[0]

    def get_center(self, dim: int):
        """全局均值向量，用于消除嵌入各向异性。缺失时返回 None（退化为原始余弦）。"""
        row = self.db.execute("SELECT vec FROM meta_vec WHERE k='center'").fetchone()
        if row is None:
            return None
        vec = np.frombuffer(row["vec"], dtype=np.float32)
        return vec if vec.shape[0] == dim else None

    def rebuild_center(self):
        ids, mat = self.load_matrix(None)
        if ids.size == 0:
            return 0
        center = mat.mean(axis=0).astype(np.float32)
        self.db.execute("INSERT OR REPLACE INTO meta_vec(k, vec) VALUES('center', ?)", (center.tobytes(),))
        return int(ids.size)

    def counts_by_layer(self) -> list[tuple[str, str, int, int]]:
        return [
            (r["layer"], r["source"], r["n_docs"], r["n_chunks"])
            for r in self.db.execute(
                "SELECT layer, source, COUNT(DISTINCT doc_id) n_docs, COUNT(*) n_chunks"
                " FROM chunks GROUP BY layer, source ORDER BY layer, source"
            )
        ]

    def load_matrix(self, layers: set[str] | None = None):
        sql = "SELECT v.chunk_id, v.vec FROM vectors v JOIN chunks c ON c.id=v.chunk_id"
        params: tuple = ()
        if layers:
            sql += " WHERE c.layer IN (%s)" % ",".join("?" * len(layers))
            params = tuple(layers)
        ids, blobs = [], []
        for r in self.db.execute(sql, params):
            ids.append(r["chunk_id"])
            blobs.append(r["vec"])
        if not ids:
            return np.array([], dtype=np.int64), np.zeros((0, 0), dtype=np.float32)
        mat = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(ids), -1)
        return np.asarray(ids, dtype=np.int64), mat

    def get_chunks(self, ids: list[int]) -> dict[int, sqlite3.Row]:
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        return {
            r["id"]: r
            for r in self.db.execute(f"SELECT * FROM chunks WHERE id IN ({q})", tuple(ids))
        }
