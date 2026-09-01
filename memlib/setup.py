"""mem init：把一份代码装成一台机器上能跑的记忆库。

只依赖标准库——它要能在还没建 venv 的裸 python 上跑起来。

九步，每步都幂等：探测环境 → 选模型 → 写配置和指针 → 建目录树 → 建运行时 →
拉模型并首次索引 → 注册 hook → 建 mem 软链 → 自检。
装到一半失败可以重跑，已完成的步骤会被跳过（除非 --force）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

from .config import CODE_ROOT, DEFAULT_HOME, POINTER, TEMPLATE

# 候选模型：fastembed 支持的一小撮，按语言给默认。dim 只是记录，写库用的是向量实际长度
MODELS = OrderedDict([
    ("zh", {
        "model": "BAAI/bge-small-zh-v1.5", "dim": 512, "size": "90MB",
        "prefix": "为这个句子生成表示以用于检索相关文章：",
        "desc": "中文优先（默认）",
    }),
    ("en", {
        "model": "BAAI/bge-small-en-v1.5", "dim": 384, "size": "67MB",
        "prefix": "Represent this sentence for searching relevant passages: ",
        "desc": "英文优先",
    }),
    ("multi", {
        "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "dim": 384, "size": "220MB", "prefix": "",
        "desc": "多语言（~50 种），体积大一些",
    }),
])

MIRRORS = {
    "cn": {"pypi": "https://pypi.tuna.tsinghua.edu.cn/simple", "hf": "https://hf-mirror.com"},
    "official": {"pypi": None, "hf": "https://huggingface.co"},
}

TYPES = ("user", "feedback", "project", "reference")


# ---------------------------------------------------------------- 探测
def detect() -> dict:
    home = Path.home()
    d = {
        "wsl": "microsoft" in Path("/proc/version").read_text().lower()
        if Path("/proc/version").exists() else False,
        "uv": shutil.which("uv"),
        "claude_projects": home / ".claude" / "projects",
        "claude_settings": home / ".claude" / "settings.json",
        "codex_dirs": [],
        "python": None,
    }
    for p in (home / ".codex",):
        if p.exists() and any(p.glob("*sessions")):
            d["codex_dirs"].append(p)
    if d["wsl"]:
        for p in Path("/mnt/c/Users").glob("*/.codex/sessions"):
            if p.is_dir():
                d["codex_dirs"].append(p)
    # venv 用的解释器：优先 3.13/3.12/3.11，都没有就用当前的
    for ver in ("3.13", "3.12", "3.11"):
        exe = shutil.which(f"python{ver}")
        if exe:
            d["python"] = (ver, exe)
            break
    if not d["python"] and sys.version_info[:2] >= (3, 11):
        d["python"] = (f"{sys.version_info.major}.{sys.version_info.minor}", sys.executable)
    return d


def _ask(prompt: str, default: str, yes: bool) -> str:
    if yes or not sys.stdin.isatty():
        return default
    got = input(f"{prompt} [{default}]: ").strip()
    return got or default


def _ask_yn(prompt: str, default: bool, yes: bool) -> bool:
    if yes or not sys.stdin.isatty():
        return default
    got = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not got else got.startswith("y")


# ---------------------------------------------------------------- 配置
def build_config(home: Path, model_key: str, mirror: str, knowledge: list[Path],
                 detected: dict, export_target: str | None) -> dict:
    cfg = json.loads(TEMPLATE.read_text(encoding="utf-8"), object_pairs_hook=OrderedDict)
    cfg.pop("_readme", None)
    cfg["root"] = str(home)

    m = MODELS[model_key]
    cfg["embedding"]["model"] = m["model"]
    cfg["embedding"]["dim"] = m["dim"]
    cfg["embedding"]["hf_endpoint"] = MIRRORS[mirror]["hf"]
    cfg["embedding"]["query_prefix"] = m["prefix"]

    srcs = [
        OrderedDict([("name", "memory"), ("layer", "L1"), ("kind", "markdown"),
                     ("path", str(home / "memory")), ("glob", "*/*.md")]),
        OrderedDict([("name", "memory-doc"), ("layer", "L1"), ("kind", "markdown"),
                     ("path", str(home)), ("glob", "*.md")]),
        # 把本仓库的说明也索引进来，库才能回答"我自己怎么用"
        OrderedDict([("name", "skill-doc"), ("layer", "L1"), ("kind", "markdown"),
                     ("path", str(CODE_ROOT)), ("glob", "**/*.md")]),
    ]
    for k in knowledge:
        srcs.append(OrderedDict([("name", k.name.lower() or "knowledge"), ("layer", "L2"),
                                 ("kind", "markdown"), ("path", str(k)), ("glob", "**/*.md")]))
    if detected["claude_projects"].exists():
        srcs.append(OrderedDict([("name", "claude-session"), ("layer", "L3"),
                                 ("kind", "claude_jsonl"), ("agent", "claude"),
                                 ("path", str(detected["claude_projects"])), ("glob", "**/*.jsonl")]))
    for i, p in enumerate(detected["codex_dirs"]):
        srcs.append(OrderedDict([("name", f"codex-session{'' if i == 0 else f'-{i}'}"),
                                 ("layer", "L3"), ("kind", "codex_jsonl"), ("agent", "codex"),
                                 ("path", str(p)),
                                 ("glob", "*sessions/**/*.jsonl" if p.name == ".codex" else "**/*.jsonl")]))
    cfg["sources"] = srcs
    if export_target:
        cfg["export"] = OrderedDict([("target", export_target), ("include", ["memory", "MEMORY.md", "config.json"])])
    return cfg


def scaffold(home: Path) -> None:
    for t in TYPES:
        (home / "memory" / t).mkdir(parents=True, exist_ok=True)
    (home / "sessions").mkdir(exist_ok=True)
    (home / "staging").mkdir(exist_ok=True)
    (home / ".index").mkdir(exist_ok=True)
    idx = home / "MEMORY.md"
    if not idx.exists():
        idx.write_text("# 记忆索引\n\n", encoding="utf-8")
    # agent 原生写入认 memory/ 为根、索引写在根下，软链让两个写入方落到同一份
    link = home / "memory" / "MEMORY.md"
    if not link.exists() and not link.is_symlink():
        link.symlink_to("../MEMORY.md")


# ---------------------------------------------------------------- 运行时
def build_venv(home: Path, mirror: str, detected: dict) -> Path:
    venv = home / ".index" / ".venv"
    py = venv / "bin" / "python"
    if py.exists():
        print("  venv 已存在，跳过")
        return py
    if not detected["python"]:
        raise SystemExit("找不到 python ≥3.11，装一个再来")
    ver, exe = detected["python"]
    env = dict(os.environ)
    idx = MIRRORS[mirror]["pypi"]
    if idx:
        env["UV_DEFAULT_INDEX"] = idx
    if detected["uv"]:
        _run(["uv", "venv", "--python", ver, str(venv)], env)
        _run(["uv", "pip", "install", "--python", str(py), "fastembed", "numpy"], env)
    else:
        _run([exe, "-m", "venv", str(venv)], env)
        cmd = [str(py), "-m", "pip", "install", "fastembed", "numpy"]
        if idx:
            cmd += ["-i", idx]
        _run(cmd, env)
    return py


def _run(cmd: list[str], env: dict | None = None) -> None:
    print("  $ " + " ".join(cmd))
    r = subprocess.run(cmd, env=env)
    if r.returncode != 0:
        raise SystemExit(f"命令失败（{r.returncode}）：{' '.join(cmd)}")


def _mem_env(home: Path) -> dict:
    env = dict(os.environ)
    env["MEM_HOME"] = str(home)
    env["PYTHONPATH"] = str(CODE_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["TOKENIZERS_PARALLELISM"] = "false"
    # 同 bin/mem：别让 cwd 抢在 CODE_ROOT 前面（3.11+）
    env["PYTHONSAFEPATH"] = "1"
    return env


# ---------------------------------------------------------------- 主流程
def run_init(args) -> int:
    yes = args.yes
    detected = detect()

    print("== 1/9 探测环境 ==")
    print(f"  平台      {'WSL2' if detected['wsl'] else sys.platform}")
    print(f"  uv        {detected['uv'] or '未安装（回落 venv + pip）'}")
    print(f"  python    {detected['python'][0] if detected['python'] else '找不到 ≥3.11'}")
    print(f"  Claude    {detected['claude_projects']}{'' if detected['claude_projects'].exists() else '（不存在，跳过）'}")
    print(f"  Codex     {', '.join(str(p) for p in detected['codex_dirs']) or '未发现'}")

    home = Path(args.home).expanduser() if args.home else Path(
        _ask("\n数据根（记忆和索引放哪）", str(DEFAULT_HOME), yes)).expanduser()
    cfg_path = home / "config.json"
    fresh = args.force or not cfg_path.exists()
    if not fresh:
        print(f"\n{cfg_path} 已存在——进入补齐模式，只做缺失的步骤（要重写配置加 --force）")

    print("\n== 2/9 选模型 ==")
    model_key = args.model
    if fresh and not model_key:
        for k, m in MODELS.items():
            print(f"  {k:<6} {m['model']:<58} {m['size']:>6}  {m['desc']}")
        model_key = _ask("选一个", "zh", yes)
    model_key = model_key or "zh"
    if model_key not in MODELS:
        raise SystemExit(f"未知模型键：{model_key}（可选：{'/'.join(MODELS)}）")
    mirror = args.mirror or _ask("下载源（cn=清华+hf-mirror，official=官方）", "cn", yes)
    if mirror not in MIRRORS:
        raise SystemExit(f"未知下载源：{mirror}")
    print(f"  → {MODELS[model_key]['model']}，源 {mirror}")

    knowledge = [Path(k).expanduser() for k in (args.knowledge_dir or [])]
    if fresh and not knowledge and not yes and sys.stdin.isatty():
        raw = _ask("额外的知识目录（L2，逗号分隔，可留空）", "", yes)
        knowledge = [Path(x.strip()).expanduser() for x in raw.split(",") if x.strip()]
    for k in knowledge:
        if not k.is_dir():
            print(f"  ! 知识目录不存在，仍会写进配置：{k}")

    cfg = build_config(home, model_key, mirror, knowledge, detected, args.export_target)

    if args.dry_run:
        print("\n== dry-run：将写入的配置 ==")
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        print(f"\n将写指针：{POINTER} → {home}")
        print(f"将建 venv：{home}/.index/.venv（{'uv' if detected['uv'] else 'venv+pip'}）")
        print("将拉模型并首次索引；hook 与 mem 软链按参数决定。未做任何改动。")
        return 0

    print("\n== 3/9 写配置与指针 ==")
    home.mkdir(parents=True, exist_ok=True)
    if fresh:
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  写入 {cfg_path}")
    else:
        print(f"  保留现有 {cfg_path}")
    if args.no_pointer:
        print(f"  跳过指针（--no-pointer）；调用时要显式 MEM_HOME={home}")
    else:
        old = POINTER.read_text(encoding="utf-8").strip() if POINTER.is_file() else ""
        if old and old != str(home) and not _ask_yn(
            f"  指针现在指向 {old}，改成 {home}？", False, yes
        ):
            print("  保留原指针不动")
        else:
            POINTER.parent.mkdir(parents=True, exist_ok=True)
            POINTER.write_text(str(home) + "\n", encoding="utf-8")
            print(f"  指针 {POINTER} → {home}")

    print("\n== 4/9 建目录树 ==")
    scaffold(home)
    print(f"  memory/{{{','.join(TYPES)}}}、sessions、staging、MEMORY.md 就位")

    print("\n== 5/9 建运行时 ==")
    py = build_venv(home, mirror, detected)

    print("\n== 6/9 拉模型并首次索引 ==")
    # 只索引 L1+L2：会话层动辄上千个 transcript，第一次全量能跑十几分钟，
    # 卡在安装流程里体验很差。留给用户自己挑时间补。
    _run([str(py), "-m", "memlib.cli", "index", "--layer", "memory,knowledge"], _mem_env(home))
    print("  会话层（L3）还没索引——它通常是最大的一层，挑个时间单独跑：mem index")

    print("\n== 7/9 注册 hook ==")
    if args.no_hooks:
        print("  跳过（--no-hooks）")
    else:
        for agent, want in (("claude", args.with_claude), ("codex", args.with_codex)):
            ask_default = bool(detected["claude_projects"].exists()) if agent == "claude" else bool(detected["codex_dirs"])
            if want is None:
                want = _ask_yn(f"  给 {agent} 挂 SessionStart/结束钩子？", ask_default, yes)
            if want:
                _run(["bash", str(CODE_ROOT / "hooks" / "install.sh"), agent])

    print("\n== 8/9 建 mem 软链 ==")
    link = Path.home() / ".local" / "bin" / "mem"
    target = CODE_ROOT / "bin" / "mem"
    if args.no_link_bin:
        print("  跳过（--no-link-bin）")
    elif link.is_symlink() and link.resolve() == target.resolve():
        print(f"  已指向本仓库：{link}")
    elif link.exists() or link.is_symlink():
        print(f"  ! {link} 已存在且指向别处，没动它。手动调用：{target}")
    else:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        print(f"  {link} → {target}")

    print("\n== 9/9 自检 ==")
    subprocess.run([str(py), "-m", "memlib.cli", "doctor"], env=_mem_env(home))

    print(f"""
装好了。数据根 {home}

  mem recall "<问题>"                  检索
  mem add --type feedback --description "..." <<'B' ... B    写一条
  mem sync                             写完收口（对账 + 索引）
  mem index                            补上会话层索引（第一次可能要几分钟）

用法细节让 agent 读 skill，或直接看 {CODE_ROOT}/README.md""")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("init", help="初始化：探测环境、选模型、建运行时、注册钩子")
    p.add_argument("--home", default=None, help="数据根，默认 ~/.agent-memory")
    p.add_argument("--model", default=None, choices=list(MODELS), help="模型：zh/en/multi")
    p.add_argument("--mirror", default=None, choices=list(MIRRORS), help="下载源：cn/official")
    p.add_argument("--knowledge-dir", action="append", default=None, help="额外的 L2 知识目录，可重复")
    p.add_argument("--export-target", default=None, help="只读副本导出目录（可选）")
    p.add_argument("--with-claude", dest="with_claude", action="store_true", default=None)
    p.add_argument("--no-claude", dest="with_claude", action="store_false")
    p.add_argument("--with-codex", dest="with_codex", action="store_true", default=None)
    p.add_argument("--no-codex", dest="with_codex", action="store_false")
    p.add_argument("--no-hooks", action="store_true", help="不注册任何 hook")
    p.add_argument("--no-link-bin", action="store_true", help="不建 ~/.local/bin/mem 软链")
    p.add_argument("--no-pointer", action="store_true", help="不写 ~/.config/mem/home 指针")
    p.add_argument("--force", action="store_true", help="重写已存在的 config.json")
    p.add_argument("--dry-run", action="store_true", help="只打印将写入的配置和将执行的命令")
    p.add_argument("-y", "--yes", action="store_true", help="非交互，全部用默认值")
    p.set_defaults(func=lambda args, cfg: sys.exit(run_init(args)), needs_config=False)
