#!/bin/bash
# install.sh [--uninstall] <claude|codex>
# 把记忆库的两个 hook 注册到指定 agent，或摘掉。幂等：已指向本目录的注册会原地更新。
#
# 接入第 N 个 agent 的三步：
#   1. config.json 的 agents 段加一条（kind / draft_cmd / resume_hint）
#   2. config.json 的 sources 加一条 L3 源，带 agent 字段
#   3. 在下面的 case 里加一个分支，写该 agent 的配置文件
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INJECT="$HOOK_DIR/inject-on-start.sh"
CAPTURE="$HOOK_DIR/capture-on-end.sh"

MODE="install"
if [ "${1:-}" = "--uninstall" ]; then
    MODE="uninstall"; shift
fi
AGENT="${1:-}"
if [ "$MODE" = "install" ]; then FMT="已注册到 %s: %s\n"; else FMT="已从 %s 卸载: %s\n"; fi

usage() { echo "用法: install.sh [--uninstall] <claude|codex>" >&2; exit 2; }
[ -n "$AGENT" ] || usage

backup() {
    [ -f "$1" ] || return 0
    cp "$1" "$1.bak.$(date +%Y%m%d-%H%M%S)"
}

# 两个 agent 的注册格式不同（Claude 用 matcher，Codex 还要 statusMessage），
# 但"按 marker 找旧注册、有就更新没有就追加、卸载时按 marker 摘掉"这套逻辑是共用的。
run_py() {
    MODE="$MODE" INJECT="$INJECT" CAPTURE="$CAPTURE" CFG="$1" FLAVOR="$2" python3 - <<'PY'
import collections, json, os
from pathlib import Path

mode, cfg_path = os.environ["MODE"], Path(os.environ["CFG"])
inject, capture, flavor = os.environ["INJECT"], os.environ["CAPTURE"], os.environ["FLAVOR"]

if cfg_path.exists():
    d = json.loads(cfg_path.read_text(encoding="utf-8") or "{}",
                   object_pairs_hook=collections.OrderedDict)
else:
    # 新机器上这两个文件本来就可能不存在，建一个最小骨架，别让安装在这里断掉
    d = collections.OrderedDict()
    if flavor == "codex":
        d["description"] = "User-level Codex lifecycle hooks."
    d["hooks"] = collections.OrderedDict()
hooks = d.setdefault("hooks", collections.OrderedDict())


def upsert(event, matcher, command, timeout, marker, msg=None):
    entries = hooks.setdefault(event, [])
    for entry in entries:
        for h in entry.get("hooks", []):
            if marker in h.get("command", ""):
                h["command"] = command
                h["timeout"] = timeout
                if msg:
                    h["statusMessage"] = msg
                if matcher is not None:
                    entry["matcher"] = matcher
                return "更新"
    entry = collections.OrderedDict()
    if matcher is not None:
        entry["matcher"] = matcher
    h = collections.OrderedDict([("type", "command"), ("command", command), ("timeout", timeout)])
    if msg:
        h["statusMessage"] = msg
    entry["hooks"] = [h]
    entries.append(entry)
    return "新增"


def remove(event, marker):
    entries = hooks.get(event, [])
    kept, dropped = [], 0
    for entry in entries:
        left = [h for h in entry.get("hooks", []) if marker not in h.get("command", "")]
        dropped += len(entry.get("hooks", [])) - len(left)
        if left:
            entry["hooks"] = left
            kept.append(entry)
    if kept:
        hooks[event] = kept
    else:
        hooks.pop(event, None)
    return "摘掉" if dropped else "本来就没有"


end_event = "Stop" if flavor == "claude" else "SessionEnd"
if mode == "uninstall":
    a = remove("SessionStart", "inject-on-start")
    b = remove(end_event, "capture-on-end")
else:
    msg_in = "Loading local memory index" if flavor == "codex" else None
    msg_out = "Queueing session for memory review" if flavor == "codex" else None
    a = upsert("SessionStart", "startup|resume", f'bash "{inject}"', 10, "inject-on-start", msg_in)
    b = upsert(end_event, None, f'bash "{capture}" {flavor}', 15, "capture-on-end", msg_out)

cfg_path.parent.mkdir(parents=True, exist_ok=True)
cfg_path.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"  SessionStart: {a}\n  {end_event}: {b}")
PY
}

case "$AGENT" in
claude)
    CFG="$HOME/.claude/settings.json"
    backup "$CFG"
    run_py "$CFG" claude
    printf "$FMT" "Claude Code" "$CFG"
    ;;
codex)
    CFG="$HOME/.codex/hooks.json"
    backup "$CFG"
    run_py "$CFG" codex
    printf "$FMT" "Codex" "$CFG"
    if [ "$MODE" = "install" ]; then
        echo
        echo "⚠ Codex 对新增/变更的 hook 需要人工授信，装完不等于生效："
        echo "  启动一次 codex，在 hook 审阅界面 trust，授信状态会写进 ~/.codex/config.toml 的 [hooks.state]。"
        echo "  没授信的 hook 静默不跑，不会报错。"
    fi
    ;;
*)
    usage
    ;;
esac
