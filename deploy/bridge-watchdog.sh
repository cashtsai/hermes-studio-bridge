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
# 連續 3 次(跨 3 分鐘)探測失敗才 kickstart —— 單次慢(負載尖峰)不動手。
# 狀態檔記連續失敗次數;kickstart 後歸零。
#
# ── 2026-11-20:只探 /health 是不夠的 ────────────────────────────────────
#
# 那天 bridge 卡死 **17.5 小時**,而這支一次都沒觸發。原因:
#
#   /health(不碰檔案)        → 200,3 毫秒
#   /app/v2/sessions(要讀檔) → 逾時,0 bytes
#
# 20 條 uvloop 檔案 I/O 執行緒全部停在 `open()` 這個 syscall 上,
# 檔案 I/O 池 100% 耗盡 → 要碰檔案的端點全部排不進去,純記憶體的照常回。
# 最後是機主自己發現「指令送不進去」才知道,不是監控告訴他。
#
# 所以**深探一定要打會碰檔案的端點**。淺探留著是因為它便宜,而且能區分
# 「整個行程沒反應」與「只有檔案 I/O 卡住」—— 日誌裡看得出是哪一種。
set -u
HEALTH_URL="http://127.0.0.1:8081/health"
DEEP_URL="http://127.0.0.1:8081/app/v2/sessions"
PLIST=~/Library/LaunchAgents/ai.studio.hermes-bridge.plist
STATE=/tmp/hermes-bridge-watchdog.fails
LOG=~/Library/Logs/hermes-bridge-watchdog.log
LABEL="ai.studio.hermes-bridge"

# 深探逾時 12 秒:健康時這支端點是 0.25 秒等級,卡住時是無限 ——
# 12 秒遠在雜訊之外,又不會讓負載尖峰誤判。
probe_fail=""
if ! curl -s -m 10 -o /dev/null "$HEALTH_URL"; then
    probe_fail="health"
else
    TOKEN=$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:BRIDGE_TOKEN" "$PLIST" 2>/dev/null)
    if [ -n "$TOKEN" ]; then
        if ! curl -s -m 12 -o /dev/null -H "Authorization: Bearer $TOKEN" "$DEEP_URL"; then
            probe_fail="sessions(檔案 I/O)"
        fi
    fi
fi

if [ -z "$probe_fail" ]; then
    rm -f "$STATE"
    exit 0
fi

fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE"
echo "$(date '+%F %T') probe failed: $probe_fail ($fails/3)" >> "$LOG"
if [ "$fails" -ge 3 ]; then
    # **重啟前先留屍檢。** 2026-11-20 那次事故,等我們察覺時行程已經被殺,
    # 只能從 C 層堆疊猜,Python 到底卡在哪一行始終沒查出來。`sample` 不需要
    # root(py-spy 在 macOS 需要),所以放得進這支以使用者身分跑的腳本。
    PID=$(pgrep -f "uvicorn bridge:app" | head -1)
    if [ -n "$PID" ]; then
        DUMP=~/Library/Logs/hermes-bridge-wedged-$(date '+%Y%m%d-%H%M%S').sample.txt
        sample "$PID" 3 -file "$DUMP" >/dev/null 2>&1
        echo "$(date '+%F %T') 取樣存於 $DUMP" >> "$LOG"
    fi
    echo "$(date '+%F %T') kickstart $LABEL (連續 $fails 次失敗:$probe_fail)" >> "$LOG"
    rm -f "$STATE"
    launchctl kickstart -k "gui/$(id -u)/$LABEL"
fi
