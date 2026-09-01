"""fastembed 封装。模型、缓存目录和下载源全部来自 config，代码里不预设地域。"""
from __future__ import annotations

import os

import numpy as np

from .config import resolve


class Embedder:
    def __init__(self, cfg: dict):
        ec = cfg["embedding"]
        self.model_name = ec["model"]
        self.batch_size = ec.get("batch_size", 32)
        self.query_prefix = ec.get("query_prefix", "")
        cache = resolve(cfg, ec.get("cache_dir", ".index/models"))
        self.cache_dir = str(cache)
        os.environ.setdefault("HF_ENDPOINT", ec.get("hf_endpoint", "https://huggingface.co"))
        # 模型已解压到本地时切离线：否则 fastembed 每次启动都去探测 HF，
        # 失败后再回落到 GCS，白等几秒还刷一屏 ERROR。
        if any(cache.glob("*/model*.onnx")):
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(
                model_name=self.model_name, cache_dir=self.cache_dir
            )
        return self._model

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        vecs = list(self.model.embed(texts, batch_size=self.batch_size))
        arr = np.asarray(vecs, dtype=np.float32)
        return _l2(arr)

    def encode_query(self, text: str) -> np.ndarray:
        return self.encode([self.query_prefix + text])[0]


def _l2(arr: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    np.maximum(norms, 1e-12, out=norms)
    return arr / norms
