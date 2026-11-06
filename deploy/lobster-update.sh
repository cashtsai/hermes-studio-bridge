#!/usr/bin/env bash
# 龍蝦 bridge 自動更新器 —— 艦隊規模化的硬前提(2026-11-02)。
#
# ## 為什麼存在
# 裝機時 clone main 一次之後就凍住;今天十台機器=十次手工外科 patch
# (2026-11-02 SG 龍蝦實際就是這樣修的)。商品化要的是:釘版 release、
# 自動跟版、**敗了自己滾回來**,人只在 Appsmith 面板上按「推這版」。
#
# ## 怎麼運作
#   releases/<tag>/   ← 每版一個完整目錄(含 venv)
#   current → symlink ← 原子切換;舊版留著 = 回滾永遠一步之遙
# 1. 問 origin 的 `lobster-v*` tag,取語意版本最高者
# 2. 與 current/.lobster-release 相同 → 無事退場
# 3. 新版 checkout 進 releases/<tag>,跑**同一支官方安裝器**
#    (「第一次裝與重跑走同一條路徑」的老紀律,不另造第二套安裝邏輯)
# 4. 切 symlink → 重啟 → **行為探活**(真打 API,不信 is-active)
# 5. 探活失敗 → 切回舊版 → 重啟 → 留痕。壞版本活不過 60 秒。
#
# ## 刻意不做的
# - 不追 main:main 是開發線,艦隊只認 tag(發版=打 tag,有意識的動作)
# - 不自動刪舊版:磁碟便宜,回滾無價;清理交給未來的保留策略(留 3 版)
set -uo pipefail

REPO="${POCKET_BRIDGE_REPO:-https://github.com/cashtsai/hermes-studio-bridge.git}"
BASE="${POCKET_LOBSTER_BASE:-$HOME/.local/share/pocket-lobster/bridge}"
CURRENT="$BASE/current"
RELEASES="$BASE/releases"
SNAPSHOTS="$BASE/snapshots"
SRC="$HOME/pocket-bridge"                 # 與 cloud-init 的 setup 腳本共用
UNIT="${POCKET_BRIDGE_LABEL:-pocket-bridge}"
ENV_FILE="${POCKET_ENV_DIR:-$HOME/.config/pocket-lobster}/bridge.env"
CRASH_STATE_DIR="${POCKET_BRIDGE_CRASH_STATE_DIR:-$HOME/.local/state/pocket-lobster/bridge}"
PYTHON="${POCKET_PYTHON:-python3}"

log() { printf '[lobster-update] %s\n' "$*"; }
die() { printf '[lobster-update] ✗ %s\n' "$*" >&2; exit 1; }

if [ -f "$ENV_FILE" ]; then
  # The installer keeps the database paths and port in this 0600 file. Never
  # print its contents; only the named variables are consumed below.
  . "$ENV_FILE"
  UNIT="${POCKET_BRIDGE_LABEL:-$UNIT}"
  CRASH_STATE_DIR="${POCKET_BRIDGE_CRASH_STATE_DIR:-$CRASH_STATE_DIR}"
  PYTHON="${POCKET_PYTHON:-$PYTHON}"
fi

reset_crash_loop() {
  rm -f "$CRASH_STATE_DIR/crash-loop.hold" "$CRASH_STATE_DIR/failures"
}

snapshot_sqlite() {
  local source="$1" destination="$2"
  [ -f "$source" ] || return 0
  mkdir -p "$(dirname "$destination")"
  "$PYTHON" - "$source" "$destination" <<'PY'
import sqlite3
import sys

source, destination = sys.argv[1:]
src = sqlite3.connect(source, timeout=30)
dst = sqlite3.connect(destination, timeout=30)
try:
    src.backup(dst)
    dst.commit()
finally:
    dst.close()
    src.close()
PY
  chmod 600 "$destination" 2>/dev/null || true
  log "SQLite snapshot: $source → $destination"
}

snapshot_databases() {
  local stamp="$1"
  local canon="${POCKET_CANON_DB:-$HOME/.local/share/pocket-agent/canonical.db}"
  local accounts="${POCKET_ACCOUNTS_DB:-$HOME/.local/share/pocket-agent/accounts.db}"
  mkdir -p "$SNAPSHOTS"
  if command -v "$PYTHON" >/dev/null 2>&1; then
    snapshot_sqlite "$canon" "$SNAPSHOTS/$stamp-canonical.db"
    snapshot_sqlite "$accounts" "$SNAPSHOTS/$stamp-accounts.db"
  else
    log "⚠️ 找不到 $PYTHON，略過 SQLite snapshot(程式碼回滾仍會執行)"
  fi
}

# ── 1. 最新 release tag ─────────────────────────────────────────────────
latest="$(git ls-remote --tags --refs "$REPO" 'lobster-v*' 2>/dev/null \
  | awk -F/ '{print $NF}' | sort -V | tail -1)"
[ -n "$latest" ] || { log "沒有任何 lobster-v* tag —— 無事退場(發版=打 tag)"; exit 0; }

current_tag=""
[ -f "$CURRENT/.lobster-release" ] && current_tag="$(cat "$CURRENT/.lobster-release")"
if [ "$latest" = "$current_tag" ]; then
  log "已在最新版 $latest"; exit 0
fi
log "更新:${current_tag:-<未釘版>} → $latest"

# ── 2. 舊佈局遷移(current 是實體目錄 → 搬進 releases 當回滾點)──────────
mkdir -p "$RELEASES"
if [ -d "$CURRENT" ] && [ ! -L "$CURRENT" ]; then
  legacy="$RELEASES/legacy-$(date +%Y%m%d%H%M%S)"
  log "遷移舊佈局:current(實體目錄)→ $legacy"
  mv "$CURRENT" "$legacy"
  printf '%s\n' "${current_tag:-legacy}" > "$legacy/.lobster-release" 2>/dev/null || true
  ln -sfn "$legacy" "$CURRENT"
  # 立刻重啟一次確認 symlink 佈局本身能跑 —— 分開驗,壞了才知道是哪一步。
  systemctl --user restart "$UNIT" || die "symlink 遷移後重啟失敗"
fi
prev_target="$(readlink "$CURRENT" 2>/dev/null || true)"

# SQLite online backup is safe while the service is live. Do not stop the
# existing release before fetch/install succeeds; a failed download must leave
# the known-good version serving traffic.
SNAPSHOT_STAMP="$(date +%Y%m%d%H%M%S)-${current_tag:-legacy}"
snapshot_databases "$SNAPSHOT_STAMP"
reset_crash_loop

# ── 3. 取碼 + 以官方安裝器建新版目錄 ───────────────────────────────────
[ -d "$SRC/.git" ] || { mkdir -p "$SRC"; git -C "$SRC" init -q; \
  git -C "$SRC" remote add origin "$REPO" 2>/dev/null || true; }
git -C "$SRC" fetch --depth 1 origin "refs/tags/$latest:refs/tags/$latest" \
  || die "fetch $latest 失敗"
git -C "$SRC" checkout -q --force "refs/tags/$latest" || die "checkout 失敗"

NEW="$RELEASES/$latest"
log "安裝 $latest → $NEW(官方安裝器,同一條路徑)"
# ── systemd unit 快照(2026-11-06 真機事故)────────────────────────────
# 安裝器會**改寫 user unit**(ExecStart 指到新版才有的 bridge-supervisor.sh
# 之類)再啟動服務 —— 但 current 還指舊版,新 ExecStart 在舊目錄裡不存在
# → crash loop → 安裝器探活失敗退出 → 本腳本「舊版不動」只保了 symlink,
# **unit 已經被改走了**,服務就此卡死(v0.4.1→v0.5.0 在 70.14 實踩)。
# 回滾必須連 unit 一起滾。
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_SNAP="$(mktemp -d)"
cp -a "$UNIT_DIR"/pocket-*.service "$UNIT_DIR"/pocket-*.timer "$UNIT_SNAP"/ 2>/dev/null || true
restore_units() {
  cp -a "$UNIT_SNAP"/. "$UNIT_DIR"/ 2>/dev/null || true
  systemctl --user daemon-reload
  systemctl --user reset-failed "$UNIT" 2>/dev/null || true
  systemctl --user restart "$UNIT" 2>/dev/null || true
}
if ! POCKET_BRIDGE_INSTALL_ROOT="$NEW" POCKET_BRIDGE_VENV="$NEW/venv" \
     POCKET_BRIDGE_RUN_ROOT="$CURRENT" POCKET_BRIDGE_RUN_VENV="$CURRENT/venv" \
     bash "$SRC/deploy/install-ubuntu.sh" >"$BASE/update-$latest.log" 2>&1; then
  log "✗ 安裝器失敗 —— 回復 unit 快照並重啟舊版(log:$BASE/update-$latest.log)"
  restore_units
  exit 1
fi
printf '%s\n' "$latest" > "$NEW/.lobster-release"

# ── 4. 原子切換 + 重啟 ─────────────────────────────────────────────────
systemctl --user stop "$UNIT" 2>/dev/null || true
ln -sfn "$NEW" "$CURRENT"
reset_crash_loop
systemctl --user restart "$UNIT"

# ── 5. 行為探活(不信 is-active —— 驗收探行為不探存活)────────────────
ok=0
if [ -f "$ENV_FILE" ]; then . "$ENV_FILE"; fi
for i in $(seq 1 20); do
  sleep 3
  code="$(curl -s -m 4 -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer ${BRIDGE_TOKEN:-}" \
    http://127.0.0.1:${POCKET_BRIDGE_PORT:-8081}/app/v2/discovery 2>/dev/null)"
  [ "$code" = "200" ] && { ok=1; break; }
done

if [ "$ok" = "1" ]; then
  log "✅ $latest 上線(行為探活 200)"
  exit 0
fi

# ── 6. 回滾 ─────────────────────────────────────────────────────────────
log "✗ 新版探活失敗 —— 回滾到 ${prev_target:-<無>}(含 unit 快照)"
restore_units
if [ -n "$prev_target" ] && [ -d "$prev_target" ]; then
  systemctl --user stop "$UNIT" 2>/dev/null || true
  ln -sfn "$prev_target" "$CURRENT"
  reset_crash_loop
  systemctl --user restart "$UNIT"
  log "已回滾並重啟;壞版本留在 $NEW 供驗屍"
else
  log "⚠️ 沒有可回滾的舊版(首次安裝就壞?)"
fi
exit 1
