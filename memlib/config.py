"""配置加载与数据根解析。config.json 是纯 JSON，不引第三方解析器。

代码和数据是分开的：代码（本仓库）可以随便 clone 到任何位置，数据（记忆、索引、
运行时）在每台机器自己的 MEM_HOME 里。两者靠下面的解析顺序对上。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent
POINTER = Path.home() / ".config" / "mem" / "home"
DEFAULT_HOME = Path.home() / ".agent-memory"
TEMPLATE = CODE_ROOT / "config.template.json"

NOT_INITIALIZED = """记忆库还没初始化（没找到 {path}）。

跑一次向导：
    {mem} init

已经装过、只是这个 shell 找不到：把数据根写进 ~/.config/mem/home，或者设 MEM_HOME 环境变量。"""


def mem_home() -> Path:
    """数据根：$MEM_HOME > ~/.config/mem/home 指针 > ~/.agent-memory。

    指针文件是给 hook 用的——hook 跑在非交互 shell 里，读不到用户 shell 里
    export 的环境变量，只靠 MEM_HOME 会静默失效。
    """
    env = os.environ.get("MEM_HOME")
    if env:
        return Path(env).expanduser()
    if POINTER.is_file():
        line = POINTER.read_text(encoding="utf-8").strip()
        if line:
            return Path(line).expanduser()
    return DEFAULT_HOME


def config_path(explicit: str | os.PathLike | None = None) -> Path:
    return Path(explicit) if explicit else mem_home() / "config.json"


def load(path: str | os.PathLike | None = None) -> dict:
    cfg_path = config_path(path)
    if not cfg_path.is_file():
        raise SystemExit(
            NOT_INITIALIZED.format(path=cfg_path, mem=CODE_ROOT / "bin" / "mem")
        )
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    # root 缺省就用 config.json 所在目录：init 写的配置带 root，手搬过的也不会错位
    cfg["_root"] = Path(cfg.get("root") or cfg_path.parent)
    cfg["_code_root"] = CODE_ROOT
    cfg["_config_path"] = cfg_path
    return cfg


def resolve(cfg: dict, rel: str) -> Path:
    """把 config 里的相对路径按数据根展开。"""
    p = Path(rel)
    return p if p.is_absolute() else cfg["_root"] / p
