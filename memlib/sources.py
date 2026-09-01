"""索引源遍历：Markdown 文件与 Claude Code 会话 transcript。

统一产出 Document：一个逻辑文档，后续再切块。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import chunk as chunkmod

# 会话里由系统注入、对回忆无价值的包裹
NOISE = re.compile(
    r"<(local-command-caveat|command-name|command-message|command-args|"
    r"local-command-stdout|system-reminder|persisted-context)>.*?</\1>",
    re.DOTALL,
)


@dataclass
class Document:
    source: str
    layer: str
    path: str
    doc_id: str
    title: str
    text: str
    mtime: float
    meta: dict = field(default_factory=dict)


def _src_agent(src: dict, fallback: str) -> str:
    """会话记录归属哪个 agent。

    以 config.json 里源上的 agent 字段为准，缺省回落到解析器自己的默认值——
    老配置没有这个字段时行为不变。
    """
    return src.get("agent") or fallback


def iter_documents(cfg: dict, redactor, only_layers: set[str] | None = None):
    for src in cfg["sources"]:
        if only_layers and src["layer"] not in only_layers:
            continue
        base = Path(src["path"])
        if not base.exists():
            continue
        kind = src.get("kind", "markdown")
        for path in sorted(base.glob(src.get("glob", "**/*"))):
            if not path.is_file():
                continue
            sp = str(path)
            if redactor.skip_path(sp):
                continue
            if kind == "markdown":
                doc = _read_markdown(src, path, redactor)
                if doc:
                    yield doc
            elif kind == "claude_jsonl":
                yield from _read_claude_jsonl(cfg, src, path, redactor)
            elif kind == "codex_jsonl":
                yield from _read_codex_jsonl(cfg, src, path, redactor)


def _read_markdown(src, path: Path, redactor) -> Document | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not raw.strip():
        return None
    meta, body = chunkmod.split_frontmatter(raw)
    title = meta.get("title") or _first_heading(body) or path.stem
    return Document(
        source=src["name"],
        layer=src["layer"],
        path=str(path),
        doc_id=str(path),
        title=title,
        text=redactor.scrub(body),
        mtime=path.stat().st_mtime,
        meta=meta,
    )


def _first_heading(body: str) -> str | None:
    for line in body.splitlines():
        m = chunkmod.HEADING.match(line)
        if m:
            return m.group(2)
    return None


def _block_text(content) -> str:
    """把 message.content 归一成纯文本，丢掉 tool_use / tool_result。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text") or "")
    return "\n".join(parts)


def _read_claude_jsonl(cfg, src, path: Path, redactor):
    """把一次 user prompt + 其后的 assistant 文本合成一个可回忆的回合。"""
    scfg = cfg.get("session", {})
    min_chars = scfg.get("min_chars", 40)
    max_chars = scfg.get("max_chars", 4000)

    turns: list[dict] = []
    cur: dict | None = None
    session_id = path.stem
    cwd = branch = ""

    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("isMeta") or d.get("isSidechain"):
                continue
            t = d.get("type")
            if t not in ("user", "assistant"):
                continue
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            text = NOISE.sub("", _block_text(msg.get("content"))).strip()
            if not text:
                continue
            cwd = d.get("cwd") or cwd
            branch = d.get("gitBranch") or branch
            session_id = d.get("sessionId") or session_id
            if t == "user":
                if cur:
                    turns.append(cur)
                cur = {"ts": d.get("timestamp", ""), "prompt": text, "reply": []}
            elif cur is not None:
                cur["reply"].append(text)
    if cur:
        turns.append(cur)

    mtime = path.stat().st_mtime
    for i, turn in enumerate(turns):
        body = "问：" + turn["prompt"]
        reply = "\n".join(turn["reply"]).strip()
        if reply:
            body += "\n\n答：" + reply
        if len(body) < min_chars:
            continue
        body = body[:max_chars]
        yield Document(
            source=src["name"],
            layer=src["layer"],
            path=str(path),
            doc_id=f"{path}#{i}",
            title=turn["prompt"].splitlines()[0][:60],
            text=redactor.scrub(body),
            mtime=mtime,
            meta={
                "session_id": session_id,
                "timestamp": turn["ts"],
                "cwd": cwd,
                "git_branch": branch,
                "agent": _src_agent(src, "claude"),
                "turn": i,
            },
        )


# --- Codex ------------------------------------------------------------------
# Codex 的 rollout 格式与 Claude 完全不同：{type, timestamp, payload}，
# 正文在 payload.type == "message" 里，内容块是 input_text / output_text。
AGENTS_BLOCK = re.compile(r"\A#\s*AGENTS\.md instructions.*?</INSTRUCTIONS>", re.DOTALL)
CODEX_NOISE = re.compile(
    r"<(environment_context|user_instructions|INSTRUCTIONS)>.*?</\1>", re.DOTALL
)


def _codex_meta(path: Path) -> dict | None:
    """读 session_meta。只有 thread_source == 'user' 的才是真实人机对话。

    1001 个 rollout 文件里 840 个是 subagent 自动化会话（审批评估占 797 个），
    正文全是 APPROVAL REQUEST / TRANSCRIPT DELTA 模板，索引进去只会淹掉真内容。
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 40:
                    break
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "session_meta":
                    return d.get("payload") or {}
    except OSError:
        return None
    return None


def _codex_text(payload: dict) -> str:
    parts = []
    for b in payload.get("content") or []:
        if isinstance(b, dict) and b.get("type") in ("input_text", "output_text"):
            parts.append(b.get("text") or "")
    text = "\n".join(parts)
    text = AGENTS_BLOCK.sub("", text)
    text = CODEX_NOISE.sub("", text)
    return text.strip()


def _read_codex_jsonl(cfg, src, path: Path, redactor):
    scfg = cfg.get("session", {})
    min_chars = scfg.get("min_chars", 40)
    max_chars = scfg.get("max_chars", 4000)

    meta = _codex_meta(path)
    if not meta or meta.get("thread_source") != "user":
        return

    session_id = meta.get("id") or path.stem
    cwd = meta.get("cwd", "")
    started = meta.get("timestamp", "")

    turns: list[dict] = []
    cur: dict | None = None
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "response_item":
                continue
            p = d.get("payload") or {}
            if p.get("type") != "message":
                continue
            role = p.get("role")
            if role not in ("user", "assistant"):
                continue  # developer 角色是规则注入，丢掉
            text = _codex_text(p)
            if not text:
                continue
            if role == "user":
                if cur:
                    turns.append(cur)
                cur = {"ts": d.get("timestamp", started), "prompt": text, "reply": []}
            elif cur is not None:
                cur["reply"].append(text)
    if cur:
        turns.append(cur)

    mtime = path.stat().st_mtime
    for i, turn in enumerate(turns):
        body = "问：" + turn["prompt"]
        reply = "\n".join(turn["reply"]).strip()
        if reply:
            body += "\n\n答：" + reply
        if len(body) < min_chars:
            continue
        yield Document(
            source=src["name"],
            layer=src["layer"],
            path=str(path),
            doc_id=f"{path}#{i}",
            title=turn["prompt"].splitlines()[0][:60],
            text=redactor.scrub(body[:max_chars]),
            mtime=mtime,
            meta={
                "session_id": session_id,
                "timestamp": turn["ts"],
                "cwd": cwd,
                "agent": _src_agent(src, "codex"),
                "turn": i,
            },
        )
