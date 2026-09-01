"""Markdown 切块：按标题层级切，保留 heading_path 作为语义上下文。"""
from __future__ import annotations

import re

FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def split_frontmatter(text: str) -> tuple[dict, str]:
    """极简 frontmatter 解析：只取顶层 `key: value`，够用于 status/verified_at。"""
    m = FRONTMATTER.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if line.startswith((" ", "\t", "-")) or ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip().strip("'\"")
        if v:
            meta[k.strip()] = v
    return meta, text[m.end():]


def chunk_markdown(text: str, target: int = 600, hard_max: int = 1200) -> list[tuple[str, str]]:
    """返回 [(heading_path, body)]。"""
    lines = text.splitlines()
    stack: list[str] = []
    blocks: list[tuple[str, list[str]]] = []
    cur: list[str] = []
    cur_path = ""
    in_fence = False

    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else HEADING.match(line)
        if m:
            if cur and any(s.strip() for s in cur):
                blocks.append((cur_path, cur))
            level = len(m.group(1))
            stack = stack[: level - 1] + [m.group(2)]
            cur_path = " / ".join(stack)
            cur = []
        else:
            cur.append(line)
    if cur and any(s.strip() for s in cur):
        blocks.append((cur_path, cur))

    out: list[tuple[str, str]] = []
    for path, body_lines in blocks:
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        for piece in _pack(body, target, hard_max):
            out.append((path, piece))
    return out


def _pack(body: str, target: int, hard_max: int) -> list[str]:
    """段落打包到 target 附近；超长单段按 hard_max 硬切。"""
    paras = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    out: list[str] = []
    buf = ""
    for p in paras:
        while len(p) > hard_max:
            out.append(p[:hard_max])
            p = p[hard_max:]
        if not buf:
            buf = p
        elif len(buf) + len(p) + 2 <= target:
            buf = buf + "\n\n" + p
        else:
            out.append(buf)
            buf = p
    if buf:
        out.append(buf)
    return out
