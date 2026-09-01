"""索引前脱敏。规则来自 config.json 的 redaction 段。

原则：宁可多打码，也不让密钥进库。索引是派生产物，误伤可以靠回看原文补救，
泄露不行。
"""
from __future__ import annotations

import re


class Redactor:
    def __init__(self, cfg: dict):
        rc = cfg.get("redaction", {})
        self._patterns = [(re.compile(p), r) for p, r in rc.get("patterns", [])]
        self._skip = [re.compile(p, re.IGNORECASE) for p in rc.get("skip_path_patterns", [])]
        self._self_test = rc.get("self_test", [])

    def skip_path(self, path: str) -> bool:
        return any(p.search(path) for p in self._skip)

    def scrub(self, text: str) -> str:
        for pat, repl in self._patterns:
            text = pat.sub(repl, text)
        return text

    def self_test(self) -> list[tuple[str, bool, str]]:
        """返回 [(样本, 是否通过, 脱敏结果)]。leak 片段必须从结果中消失。"""
        out = []
        for sample, leak in self._self_test:
            got = self.scrub(sample)
            out.append((sample, leak not in got, got))
        return out
