#!/bin/zsh
# bridge watchdog — GIL 死結防線第二層(第一層 = bridge.py 的 sqlite 連線
# try/finally 收斂,fix/sqlite-gc-deadlock)。
#
# 背景:2026-07-25 bridge 兩度卡死 —— 主執行緒 GC 回收「未關閉的 sqlite3
# 連線」時 stmt_dealloc → take_gil 自我等待,全執行緒凍結、100% CPU 空轉,
# /health 完全不回應但行程活著 → launchd KeepAlive 看不出來,一掛 2~4 小時
# (sample 存 ~/Library/Logs/hermes-bridge-deadlock-20260725*.sample.txt)。
#
# 這支由 launchd 每 60s 跑一次(獨立行程,不受 bridge 的 GIL 影響):
# /health 連續 3 次(跨 3 分鐘)逾時才 kickstart —— 單次慢(負載尖峰)不動手。
# 狀態檔記連續失敗次數;kickstart 後歸零。
set -u
HEALTH_URL="http://127.0.0.1:8081/health"
STATE=/tmp/hermes-bridge-watchdog.fails
LOG=~/Library/Logs/hermes-bridge-watchdog.log
LABEL="ai.studio.hermes-bridge"

if curl -s -m 10 -o /dev/null "$HEALTH_URL"; then
    rm -f "$STATE"
    exit 0
fi

fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE"
echo "$(date '+%F %T') health timeout ($fails/3)" >> "$LOG"
if [ "$fails" -ge 3 ]; then
    echo "$(date '+%F %T') kickstart $LABEL (連續 $fails 次無回應)" >> "$LOG"
    rm -f "$STATE"
    launchctl kickstart -k "gui/$(id -u)/$LABEL"
fi
