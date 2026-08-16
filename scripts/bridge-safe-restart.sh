#!/bin/zsh
# 安全重啟 ai.studio.hermes-bridge:等活回合歸零才動手。
# 背景(2026-08-04 實害):kickstart 會無聲殺掉進行中的人格回合(canonical 總結
# 永遠不落地,TG 進度短句成孤兒 → 使用者看到重複/斷尾)。本腳本輪詢 /health 的
# turns_in_flight,歸零才重啟;超過上限(預設 600s)則明講並中止,絕不硬殺。
# 用法:scripts/bridge-safe-restart.sh [max_wait_seconds]
set -u
MAX=${1:-600}
T0=$(date +%s)
while true; do
  N=$(curl -s -m 5 http://127.0.0.1:8081/health | /usr/bin/python3 -c "import json,sys
try: print(json.load(sys.stdin).get('turns_in_flight', 0))
except Exception: print(-1)")
  if [ "$N" = "0" ]; then break; fi
  if [ "$N" = "-1" ]; then echo "⚠️ health 讀不到(bridge 掛了?),直接重啟"; break; fi
  EL=$(( $(date +%s) - T0 ))
  if [ "$EL" -ge "$MAX" ]; then
    echo "✗ 等了 ${EL}s 仍有 $N 個活回合 — 不硬殺,中止。加大上限或稍後再試。"
    exit 1
  fi
  echo "… $N 個活回合進行中,已等 ${EL}s(上限 ${MAX}s)"
  sleep 10
done
rm -f /tmp/hermes-bridge-watchdog.fails
launchctl kickstart -k "gui/$(id -u)/ai.studio.hermes-bridge" || exit 1
sleep 6
curl -s -m 8 http://127.0.0.1:8081/health | head -c 160; echo
echo "✓ 安全重啟完成"
# 部署後煙測(2026-08-16 接上,交接待辦):重啟不是終點,顯示層不變式
# 要當場驗過才算部署完成 —— 8 月那七顆「底層正常、給使用者看的數字是
# 錯的」bug 全是人踩到才知道。煙測失敗 = exit 2,呼叫端(人或 cron)
# 會看到;bridge 活著所以不回滾,但必須當場修或回報。
# ⚠️ 注意:kickstart 不重讀 plist EnvironmentVariables;若這次重啟是為了
# 吃新 env,請改用 bootout+bootstrap(見 docs/HANDOFF 慣例)。
SMOKE="$(dirname "$0")/post-deploy-smoke.py"
if [ -f "$SMOKE" ]; then
  TOKEN=$(/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:BRIDGE_TOKEN' \
    "$HOME/Library/LaunchAgents/ai.studio.hermes-bridge.plist" 2>/dev/null || true)
  if BRIDGE_TOKEN="$TOKEN" /usr/bin/env python3 "$SMOKE"; then
    echo "✓ 部署後煙測全過"
  else
    echo "✗ 部署後煙測有紅 — bridge 已重啟但顯示層不變式壞了,立刻查" >&2
    exit 2
  fi
fi
