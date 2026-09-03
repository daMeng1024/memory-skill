"""候选审核：把 staging 里的候选分成"自动拒 / 自动过 / 留给人"三档。

风险是不对称的，所以两档的门槛也不一样：

- **自动拒**判错了，最多漏一条记忆——transcript 还在，随时能重抽，成本接近 0。
- **自动过**判错了，一条错记忆会进正式库、进开场注入，之后每个会话都被它影响，
  而且越久越像既定事实。

所以自动拒可以放开做（有确定性判据就拒），自动过只给"模型自己提出、机器能执行、
全部通过"的断言，而且过了也先挂 status: auto——进正式库、参与召回，但不进开场注入，
等人 `mem approve` 一次才转正。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import config as cfgmod
from .curate import read_frontmatter
from .redact import Redactor

VALID_TYPES = ("user", "feedback", "project", "reference")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
# 正文里带扩展名或以 / 结尾的路径，只认反引号里的，避免把散文切碎
PATH_RE = re.compile(r"`(~?/[^`\s]+)`")

# 阈值是在中文语料上量出来的，而且用的是**去均值后**的余弦（尺度比原始余弦低很多）：
# 逐字重复 0.90，近义重复 0.56~0.63，无关条目 0.17~0.26。
# 查重卡 0.55——判错的代价只是候选进 rejected/ 且报告里写明像谁，人能捞回来；
# 刷 verified_at 卡 0.85——给一条其实已经过时的记忆盖上"刚验过"是真伤害，必须近乎逐字重复才做。
# 换模型或换语言语料都要重量一遍，见 DESIGN.md「可移植性边界」。
DEFAULTS = {
    "dup_similarity": 0.55,
    "reconfirm_similarity": 0.85,
    "auto_types": ["project", "reference"],
    "auto_promote": True,
}


def _acfg(cfg: dict) -> dict:
    out = dict(DEFAULTS)
    out.update(cfg.get("audit") or {})
    return out


# ---------------------------------------------------------------- 断言执行
def check_assertion(a: dict, root: Path) -> tuple[bool, str]:
    """执行一条模型提出的断言。返回 (是否成立, 证据描述)。

    刻意不做自然语言推断：断言由起草那一步显式给出（frontmatter 的 verify），
    这里只负责执行。从散文里正则挖断言太容易挖错，挖错的代价是自动放行一条假记忆。
    """
    kind = (a.get("kind") or "").strip()
    if kind in ("path", "absent_path"):
        target = Path(str(a.get("path", "")).replace("~", str(Path.home()), 1))
        exists = target.exists()
        want = kind == "path"
        return exists == want, f"{'存在' if exists else '不存在'}：{target}"
    if kind in ("command", "absent_command"):
        name = str(a.get("command", "")).strip()
        found = shutil.which(name)
        want = kind == "command"
        return bool(found) == want, f"命令 {name} {'在 ' + found if found else '不在 PATH'}"
    if kind == "grep":
        target = Path(str(a.get("path", "")).replace("~", str(Path.home()), 1))
        pattern = str(a.get("pattern", ""))
        if not target.is_file() or not pattern:
            return False, f"无法检索：{target}"
        try:
            hit = pattern in target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, f"读不了 {target}：{exc}"
        return hit, f"{'命中' if hit else '未命中'} {pattern!r} @ {target}"
    if kind == "git_ref":
        ref = str(a.get("ref", ""))
        repo = str(a.get("repo", root))
        r = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", ref],
                           capture_output=True, text=True)
        return r.returncode == 0, f"git ref {ref} @ {repo} {'存在' if r.returncode == 0 else '不存在'}"
    return False, f"不认识的断言类型：{kind or '(空)'}"


def broken_paths(body: str) -> list[str]:
    """正文里写死的路径，父目录在但自己不在——这是路径写错的强证据。

    两类不算：

    - 父目录也不在的。那多半是远端机器、容器里或别人机器上的路径，本机验不了
      不等于是错的。
    - 根下只有一段的（`/compact`、`/clear`）。这些几乎都是斜杠命令而不是路径，
      而根目录必然存在，不排掉就会稳定误伤所有提到斜杠命令的候选。真正的根下
      单段目录（/etc、/tmp）本来就存在，不会被判成缺失。
    """
    bad = []
    for raw in PATH_RE.findall(body):
        if raw.startswith("/") and raw.count("/") == 1:
            continue
        p = Path(raw.replace("~", str(Path.home()), 1))
        if not p.exists() and p.parent.exists() and p.parent != p:
            bad.append(raw)
    return bad


# ---------------------------------------------------------------- 相似度
def _existing_similar(cfg, body: str, threshold: float):
    """跟正式库里已有的记忆比一比。返回 (最像的那条路径, 相似度) 或 None。

    只比 memory 源（真正的记忆），不比 README / SKILL 这些文档源。
    索引可能不是最新的——这只影响"查重漏掉"，不会造成误拒。
    """
    try:
        from . import search
        from .embed import Embedder
        from .store import Store
    except ImportError:
        return None
    db = cfgmod.resolve(cfg, cfg["index"]["db"])
    if not db.exists():
        return None
    store = Store(db)
    try:
        vec = Embedder(cfg).encode_query(body[:1000])
        hits = search.semantic_scored(store, vec, 20, {"L1"})
        rows = store.get_chunks([c for c, _ in hits])
        for cid, score in hits:
            r = rows.get(cid)
            if r is None or r["source"] != "memory":
                continue
            return (Path(r["source_path"]), score) if score >= threshold else None
    except Exception:
        return None
    finally:
        store.close()
    return None


# ---------------------------------------------------------------- 判定
def judge(cfg, path: Path, redactor: Redactor) -> dict:
    """给一个候选定档。返回 {verdict, reasons, evidence, similar}。"""
    acfg = _acfg(cfg)
    meta = read_frontmatter(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    body = text.split("---", 2)[-1].strip()
    mtype, _, slug = path.stem.partition("__")
    reasons: list[str] = []

    # 1. 脱敏：候选正文从没被 scrub 过，promote 之后会直接进 git
    if redactor.scrub(body) != body or redactor.scrub(meta.get("description", "")) != meta.get("description", ""):
        reasons.append("正文命中脱敏规则，疑似含凭据")

    # 2. 结构
    if mtype not in VALID_TYPES:
        reasons.append(f"type 不合法：{mtype}")
    if not SLUG_RE.match(slug or ""):
        reasons.append(f"slug 不合法：{slug}")
    if not meta.get("description"):
        reasons.append("缺 description")
    if mtype in ("feedback", "project"):
        for token in ("**Why:**", "**How to apply:**"):
            if token not in body:
                reasons.append(f"缺 {token}")

    # 3. 路径写错
    for bad in broken_paths(body):
        reasons.append(f"路径不存在（父目录在）：{bad}")

    # 4. 重名
    dest = cfg["_root"] / "memory" / mtype / f"{slug}.md"
    if dest.exists():
        reasons.append(f"正式库已有同名条目：memory/{mtype}/{slug}.md")

    if reasons:
        return {"verdict": "reject", "reasons": reasons, "evidence": [], "similar": None}

    # 5. 语义查重
    similar = _existing_similar(cfg, body, float(acfg["dup_similarity"]))
    if similar:
        return {"verdict": "dup", "reasons": [f"与 {similar[0].name} 相似度 {similar[1]:.2f}"],
                "evidence": [], "similar": similar}

    # 6. 自动过：类型受限 + 模型提出的断言全部通过
    evidence: list[str] = []
    if acfg["auto_promote"] and mtype in acfg["auto_types"]:
        try:
            asserts = json.loads(meta.get("verify") or "[]")
        except (json.JSONDecodeError, TypeError):
            asserts = []
        if isinstance(asserts, list) and asserts:
            results = [check_assertion(a, cfg["_root"]) for a in asserts if isinstance(a, dict)]
            if results and all(ok for ok, _ in results):
                return {"verdict": "auto", "reasons": [],
                        "evidence": [ev for _, ev in results], "similar": None}
            evidence = [("✓ " if ok else "✗ ") + ev for ok, ev in results]

    return {"verdict": "review", "reasons": [], "evidence": evidence, "similar": None}


# ---------------------------------------------------------------- 落地
def _promote_auto(cfg, path: Path, evidence: list[str]) -> Path:
    """自动过的条目直接进正式库，但挂 status: auto——不进开场注入。"""
    from .cli import _append_index

    mtype, _, slug = path.stem.partition("__")
    dest_dir = cfg["_root"] / "memory" / mtype
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{slug}.md"

    from .curate import upsert_front

    dest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    upsert_front(dest, {
        "status": "auto",
        "verified_at": time.strftime("%Y-%m-%d"),
        "verified_by": "mem-audit",
        "evidence": json.dumps(evidence, ensure_ascii=False),
    })

    desc = read_frontmatter(dest).get("description", "")
    _append_index(cfg, slug, desc, mtype)
    path.unlink()
    return dest


def _reconfirm(cfg, target: Path) -> None:
    """已有条目被再次确认：只刷 verified_at，不引入任何新事实。"""
    from .curate import upsert_front

    upsert_front(target, {"verified_at": time.strftime("%Y-%m-%d")})


def run_audit(cfg, args) -> int:
    root = cfg["_root"]
    staging = root / "staging"
    runs = [staging / args.run_id] if args.run_id else sorted(p for p in staging.glob("*") if p.is_dir())
    runs = [r for r in runs if r.is_dir()]
    if not runs:
        print("没有待审批次")
        return 0

    acfg = _acfg(cfg)
    redactor = Redactor(cfg)
    totals = {"auto": 0, "reject": 0, "dup": 0, "review": 0}

    for run in runs:
        files = sorted(run.glob("*.md"))
        if not files:
            continue
        print(f"== {run.name}（{len(files)} 条候选）==")
        for f in files:
            v = judge(cfg, f, redactor)
            totals[v["verdict"]] += 1
            mark = {"auto": "自动过", "reject": "自动拒", "dup": "重复", "review": "留给人"}[v["verdict"]]
            print(f"  [{mark}] {f.name}")
            for r in v["reasons"]:
                print(f"          - {r}")
            for e in v["evidence"]:
                print(f"          证据 {e}")
            if args.dry_run:
                continue
            if v["verdict"] == "reject":
                (run / "rejected").mkdir(exist_ok=True)
                f.rename(run / "rejected" / f.name)
            elif v["verdict"] == "dup":
                target, score = v["similar"]
                if score >= float(acfg["reconfirm_similarity"]):
                    _reconfirm(cfg, target)
                    print(f"          → 已给 {target.name} 刷 verified_at")
                (run / "rejected").mkdir(exist_ok=True)
                f.rename(run / "rejected" / f.name)
            elif v["verdict"] == "auto":
                dest = _promote_auto(cfg, f, v["evidence"])
                print(f"          → memory/{dest.parent.name}/{dest.name}（status: auto，暂不进注入）")

    print()
    print(f"自动过 {totals['auto']}，自动拒 {totals['reject']}，重复 {totals['dup']}，留给人 {totals['review']}")
    if args.dry_run:
        print("（dry-run，没有移动任何文件）")
        return 0
    if totals["review"]:
        print(f"剩下的看一眼再 mem promote <run-id>；不要的直接删")
    if totals["auto"]:
        print("自动过的条目已进库但不进开场注入，抽查后 mem approve <name> 转正")
    if totals["auto"] or totals["dup"]:
        print("记得跑 mem sync 让索引跟上")
    return 0
