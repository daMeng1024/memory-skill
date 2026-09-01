"""自动沉淀：把待抽取会话交给 claude -p 起草候选记忆，落 staging 等人工审核。

自动写入永远落 staging，绝不直接改事实源——这是整个写入链路的硬约束。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .redact import Redactor
from .sources import _read_claude_jsonl, _read_codex_jsonl

READERS = {"claude_jsonl": _read_claude_jsonl, "codex_jsonl": _read_codex_jsonl}

MIN_DIGEST = 200

PROMPT = """你在为一个本地记忆库起草候选记忆条目。下面是一段 {agent_label} 会话记录。

请只提取**跨会话仍然有价值**的事实，每条一个独立条目：
- user: 用户的身份、偏好、工作习惯
- feedback: 用户给出的纠偏，或明确确认过的做法（必须写清楚原因）
- project: 项目约束、在办目标、外部系统事实（相对日期一律转成绝对日期）
- reference: 外部资源指针（URL、看板、工单）

不要提取：
- 代码结构、历史修复、git 记录、CLAUDE.md 里已有的内容
- 只对这次会话有意义的临时状态
- 任何凭据、token、密码、cookie、真实账号

输出严格的 JSON 数组，不要 markdown 代码块包裹。每个元素：
{{"type": "user|feedback|project|reference", "name": "kebab-case-短slug", "description": "一句话摘要", "body": "正文；feedback 和 project 必须包含 **Why:** 与 **How to apply:** 两行"}}

没有值得记的就输出 []。

会话记录（{sid}，{when}）：
---
{digest}
---"""


def _agent_spec(cfg, agent: str) -> dict:
    """取该 agent 的解析器与起草命令；未知 agent 一律按 claude 处理，保持旧行为。"""
    agents = cfg.get("agents") or {}
    return agents.get(agent) or agents.get("claude") or {}


def _digest(cfg, path: Path, redactor, max_chars: int, agent: str = "claude") -> str:
    """长会话取头尾，不能只截开头。

    结论、纠偏和最终决定几乎都在会话后半段；直接 text[:max_chars] 会把
    一个 8 MB 的 transcript 截成开头的探索过程，正好丢掉最该沉淀的部分。
    """
    spec = _agent_spec(cfg, agent)
    kind = spec.get("kind", "claude_jsonl")
    reader = READERS.get(kind, _read_claude_jsonl)
    src = {"name": f"{agent}-session", "layer": "L3", "agent": agent, "path": str(path.parent)}
    parts = [doc.text for doc in reader(cfg, src, path, redactor)]
    text = "\n\n".join(parts)
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.3)
    tail = max_chars - head
    return (
        text[:head]
        + f"\n\n……（中间省略 {len(text) - max_chars} 字符）……\n\n"
        + text[-tail:]
    )


def _draft(spec: dict, prompt: str, model: str, timeout: int) -> str:
    """调该 agent 的 CLI 起草候选条目，返回模型最终输出的原始文本。

    两种取输出的方式：claude -p 直接给 stdout；codex exec 走 -o 落文件
    （它的 stdout 混着事件日志）。codex 侧用 --ephemeral，免得起草过程
    自己落一份 rollout，下轮又被当成待抽取会话——自噬循环。
    """
    cmd = list(spec.get("draft_cmd") or ["claude", "-p", "--model", "{model}", "--output-format", "text"])
    mode = spec.get("draft_output", "stdout")
    if mode == "file":
        with tempfile.TemporaryDirectory(prefix="mem-draft-") as td:
            outfile = str(Path(td) / "last-message.txt")
            argv = [a.format(model=model, outfile=outfile) for a in cmd]
            subprocess.run(
                argv, input=prompt, capture_output=True, text=True, timeout=timeout
            )
            out = Path(outfile)
            return out.read_text(encoding="utf-8") if out.exists() else ""
    argv = [a.format(model=model, outfile="") for a in cmd]
    proc = subprocess.run(
        argv, input=prompt, capture_output=True, text=True, timeout=timeout
    )
    return proc.stdout


def run_review(cfg, limit: int, model: str, timeout: int, max_chars: int, dry_run: bool):
    root = cfg["_root"]
    pending_path = root / "sessions" / "pending.jsonl"
    if not pending_path.exists():
        print("待抽取队列为空。")
        return

    rows = []
    for line in pending_path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    todo = [r for r in rows if r.get("status") == "pending"][:limit]
    if not todo:
        print(f"没有待抽取会话（队列共 {len(rows)} 条，均已处理）。")
        return

    redactor = Redactor(cfg)
    sig = hashlib.sha256("".join(r["session_id"] for r in todo).encode()).hexdigest()[:12]
    run_id = time.strftime("%Y%m%d-%H%M%S-") + sig
    out_dir = root / "staging" / run_id

    print(f"待抽取 {len(todo)} 个会话（队列共 {len(rows)} 条）：\n")
    drafted = 0
    for r in todo:
        sid = (r.get("session_id") or "?")[:8]
        tp = Path(r["transcript_path"])
        if not tp.exists():
            if not dry_run:
                r["status"] = "missing"
            print(f"  - {sid}  跳过：transcript 已不存在 {tp}")
            continue
        agent = r.get("agent") or "claude"
        spec = _agent_spec(cfg, agent)
        digest = _digest(cfg, tp, redactor, max_chars, agent)
        if len(digest) < MIN_DIGEST:
            if not dry_run:
                r["status"] = "too-short"
            print(f"  - {sid}  跳过：有效内容仅 {len(digest)} 字符（阈值 {MIN_DIGEST}），没有可沉淀的东西")
            continue
        prompt = PROMPT.format(
            sid=sid, when=r.get("captured_at", ""), digest=digest,
            agent_label=spec.get("label", agent),
        )
        if dry_run:
            print(f"  - {sid}  将起草（{agent}），摘要 {len(digest)} 字符")
            continue
        try:
            raw = _draft(spec, prompt, model, timeout)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            print(f"  ! {r['session_id'][:8]} 起草失败（{agent}）：{exc}")
            continue
        items = _parse(raw)
        if items is None:
            print(f"  ! {r['session_id'][:8]} 返回无法解析，跳过")
            continue
        for item in items:
            if _write_candidate(out_dir, item, r):
                drafted += 1
        r["status"] = "drafted"
        r["run_id"] = run_id
        print(f"  {r['session_id'][:8]}  起草 {len(items)} 条")

    if dry_run:
        print("\n（dry-run，未调用模型，未改动队列状态）")
        return

    pending_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    if drafted:
        print(f"\n共起草 {drafted} 条候选，落在：{out_dir}")
        print("人工审核：删掉不要的 .md，然后")
        print(f"  {root}/bin/mem promote {run_id}")
    else:
        print("\n没有产出候选条目。")


def _parse(stdout: str):
    s = stdout.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1].rsplit("```", 1)[0]
    start, end = s.find("["), s.rfind("]")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


VALID = {"user", "feedback", "project", "reference"}


def _write_candidate(out_dir: Path, item: dict, row: dict) -> bool:
    if not isinstance(item, dict):
        return False
    mtype = item.get("type")
    name = (item.get("name") or "").strip()
    desc = (item.get("description") or "").strip()
    body = (item.get("body") or "").strip()
    if mtype not in VALID or not name or not desc or not body:
        return False
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{mtype}__{name}.md"
    dest.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {desc}\n"
        "metadata:\n"
        f"  type: {mtype}\n"
        f"  created: {time.strftime('%Y-%m-%d')}\n"
        f"  from_session: {row.get('session_id','')}\n"
        "---\n\n" + body + "\n",
        encoding="utf-8",
    )
    return True


def run_promote(cfg, run_id: str):
    root = cfg["_root"]
    src_dir = root / "staging" / run_id
    if not src_dir.is_dir():
        raise SystemExit(f"没有这个批次：{src_dir}")
    files = sorted(src_dir.glob("*.md"))
    if not files:
        print("该批次已无候选（可能都被删掉了），清理目录。")
        shutil.rmtree(src_dir)
        return

    from .cli import _append_index

    moved = 0
    for f in files:
        mtype, _, rest = f.stem.partition("__")
        if mtype not in VALID or not rest:
            print(f"  ! 文件名不合规，跳过：{f.name}")
            continue
        dest_dir = root / "memory" / mtype
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{rest}.md"
        if dest.exists():
            print(f"  ! 已存在，跳过：{dest.relative_to(root)}（需要更新请手动合并）")
            continue
        text = f.read_text(encoding="utf-8")
        dest.write_text(text, encoding="utf-8")
        desc = ""
        for line in text.splitlines():
            if line.startswith("description:"):
                desc = line.partition(":")[2].strip()
                break
        _append_index(cfg, rest, desc, mtype)
        moved += 1
        print(f"  + memory/{mtype}/{rest}.md")

    shutil.rmtree(src_dir)
    _mark(cfg, run_id)
    print(f"\n合入 {moved} 条，staging/{run_id} 已清理。")
    print(f"接着跑：{root}/bin/mem index")


def _mark(cfg, run_id: str):
    p = cfg["_root"] / "sessions" / "pending.jsonl"
    if not p.exists():
        return
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("run_id") == run_id:
            r["status"] = "promoted"
        rows.append(r)
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
