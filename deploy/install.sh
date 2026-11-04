#!/usr/bin/env bash
set -euo pipefail

# Pocket 自架主機一鍵安裝引導(2026-11-04)。
#   curl -fsSL https://pocketone.app/install.sh | sh
#
# 這支只做三件事:檢查前置 → 取原始碼(釘最新 lobster-v* tag,跟代管
# 雲端主機同一份驗收過的版位)→ 交棒給 deploy/install-ubuntu.sh(完全體
# 安裝器:bridge+OpenClaw+CC+CX+agy+frpc+systemd+QR 配對)。
# 雲端龍蝦(我們代管)不走這支 —— 它由 cloud-init 帶 enroll token 自動
# 配對;這支服務的是「自己有一台 Ubuntu 的人」,裝完掃 QR 配對,跟 Mac
# 自架完全同一等級的主機。
#
# 設計原則:
#   · 冪等 —— 重跑 = 更新到最新 tag 再重裝(install-ubuntu.sh 本身冪等)
#   · 不碰 root 常駐(per-user systemd);sudo 只在 apt 缺件時用
#   · 不經手任何憑證 —— CC/CX/Gemini 登入在 Pocket app 裡完成

REPO="${POCKET_REPO:-https://github.com/cashtsai/hermes-studio-bridge.git}"
SRC="${POCKET_SRC:-$HOME/pocket-bridge}"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "目前支援 Linux(Ubuntu 22.04+ 實測);macOS 請用 Pocket 桌面版 DMG"
command -v git >/dev/null 2>&1 || {
  say "安裝 git(需要 sudo)"
  sudo apt-get update -qq && sudo apt-get install -y -qq git || die "git 裝不上,請手動 apt install git 後重跑"
}

say "取得 Pocket bridge 原始碼"
if [ ! -d "$SRC/.git" ]; then
  mkdir -p "$SRC"
  git -C "$SRC" init -q
  git -C "$SRC" remote add origin "$REPO"
fi
git -C "$SRC" fetch -q --tags origin || die "連不上 GitHub"

# 釘最新 lobster-v* release tag —— 這是驗收過的出貨版位(代管雲端主機
# 同款);抓不到 tag(fork/鏡像)才退回 main,並明講。
REF="${POCKET_REF:-}"
if [ -z "$REF" ]; then
  REF="$(git -C "$SRC" tag -l 'lobster-v*' | sort -V | tail -1)"
  [ -n "$REF" ] || { echo "  (找不到 lobster-v* tag,退回 main)"; REF="origin/main"; }
fi
say "版位:$REF"
git -C "$SRC" checkout -q --force "$REF" 2>/dev/null \
  || git -C "$SRC" checkout -q --force "origin/${REF#origin/}" \
  || die "checkout $REF 失敗"
echo "    $(git -C "$SRC" log -1 --format='%h %s' | cut -c1-70)"

say "交棒完全體安裝器"
chmod +x "$SRC/deploy/install-ubuntu.sh"
exec "$SRC/deploy/install-ubuntu.sh"
