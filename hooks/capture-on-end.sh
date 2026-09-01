#!/bin/bash
# capture-on-end.sh <agent>
# 会话结束 hook：把 transcript 指针记入记忆库待抽取队列。
# Claude Code 挂 Stop，Codex 挂 SessionEnd——两边载荷都带 session_id 和
# transcript_path，capture 按 session_id 去重，重复触发只更新同一行。
# agent 由安装时显式传入，不做路径嗅探。
# 只记指针，抽取交给 `mem review`——hook 里不能跑重活。
# 任何失败都静默 exit 0，绝不拖垮会话。

AGENT="${1:-claude}"
# 代码根由脚本位置推出；数据根由 bin/mem 自己解析（$MEM_HOME > 指针文件 > 默认）。
CODE_ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
MEM="$CODE_ROOT/bin/mem"

[ -x "$MEM" ] || exit 0
timeout 10 "$MEM" capture --agent "$AGENT" >/dev/null 2>&1
exit 0
