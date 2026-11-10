#!/bin/sh
# Pocket bridge — 自架用戶的 Ubuntu/Debian 一鍵安裝器(2026-11-05,看板 #15)。
#
#   curl -fsSL https://raw.githubusercontent.com/cashtsai/hermes-studio-bridge/main/deploy/selfhost-install.sh | sh
#
# 目標使用者:自己有一台 Ubuntu 22.04+/Debian 12+(或 Windows 上的 WSL2)的
# 開發者,想把它裝成可被 Pocket App 連上的自架 bridge 主機。
#
# 與雲端龍蝦(deploy/install.sh → install-ubuntu.sh)同一套紀律:
#   · 釘最新 lobster-v* release tag(main 是開發線,安裝只認 tag)
#   · releases/<tag>/ + current symlink 佈局 → 升級=原子切換、回滾一步之遙
#   · 升級走同一支 deploy/lobster-update.sh(SQLite 快照/unit 快照/行為探活/
#     自動回滾),不另造第二套更新邏輯
# 自架差異(刻意剝掉的):
#   · 不 enroll Pocket ID、不裝 relay/frpc —— 自架=局網直連或自己開 Tailscale;
#     要免 Tailscale 的固定公網位址,日後可訂閱 Pocket Relay
#   · 不裝 OpenClaw/Claude Code/Codex/agy —— coding agent 由機主自己裝、
#     自己登入(本腳本一行憑證都不經手;結尾會印提示)
#   · 不開 cloudflared quick tunnel —— 預設 bind 0.0.0.0 讓局網直連
#
# 冪等:重跑 = 升級到最新 tag(已裝+新 tag 時交棒 lobster-update.sh);
# BRIDGE_TOKEN 與配對 boot code 只生成一次,重跑不重生。
#
# 旗標:
#   --no-start   只裝到 systemd unit 檔生成為止,不碰 systemctl(容器/CI 用)
#   --force      非 Ubuntu/Debian 仍強行安裝
#
# 刻意寫成 POSIX sh(不是 bash):`curl | sh` 在 Ubuntu 上是 dash,
# bash-only 語法會在半路炸掉。shellcheck 以 sh 方言全過。
#
# 內部契約(lobster-update.sh 呼叫用):POCKET_BRIDGE_INSTALL_ROOT 與
# POCKET_BRIDGE_RUN_ROOT **同時**存在時進「payload 模式」—— 只鋪程式碼+venv+
# unit 檔,不啟動服務(啟動/探活/回滾由更新器負責,同 install-ubuntu.sh 的
# env 契約)。
set -eu

say()  { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '!!  %s\n' "$*" >&2; }
die()  { printf 'x  %s\n' "$*" >&2; exit 1; }

# ── 平台防呆 ───────────────────────────────────────────────────────────────
case "$(uname -s)" in
  Darwin) die "macOS 請用 Pocket Connect / Pocket macOS(App 內建 bridge),不走這支安裝器" ;;
  Linux) : ;;
  *) die "不支援的平台:$(uname -s)(支援 Ubuntu 22.04+/Debian 12+,Windows 請在 WSL2 裡跑)" ;;
esac

# ── 佈局(與 lobster-update.sh 的預設一致,env 可覆蓋)─────────────────────
REPO="${POCKET_REPO:-https://github.com/cashtsai/hermes-studio-bridge.git}"
SRC="${POCKET_SRC:-$HOME/pocket-bridge}"
BASE="${POCKET_LOBSTER_BASE:-$HOME/.local/share/pocket-lobster/bridge}"
CURRENT="$BASE/current"
RELEASES="$BASE/releases"
UNIT_NAME="${POCKET_BRIDGE_LABEL:-pocket-bridge}"
UNIT_DIR="$HOME/.config/systemd/user"
ENV_DIR="${POCKET_ENV_DIR:-$HOME/.config/pocket-lobster}"
ENV_FILE="$ENV_DIR/bridge.env"
TOKEN_FILE="${POCKET_BRIDGE_TOKEN_FILE:-$HOME/.pocket/bridge-token}"
BOOT_CODE_FILE="${PAIR_BOOT_CODE_FILE:-$HOME/.pocket/pair-boot-code}"
BRIDGE_PORT="${POCKET_BRIDGE_PORT:-8081}"
STATE_DIR="$HOME/.local/state/pocket-lobster/bridge"
RUN_HELPER="$HOME/.local/bin/pocket-bridge-run.sh"

# bind 位址:自架預設 0.0.0.0(局網直連是主要配對路徑;龍蝦版綁 loopback 是
# 因為它有 tunnel 當唯一入口,自架沒有)。曾寫進 bridge.env 的值重跑時沿用。
_env_bind=""
[ -f "$ENV_FILE" ] && _env_bind="$(sed -n 's/^POCKET_BRIDGE_BIND=//p' "$ENV_FILE" | head -n1)" || true
BRIDGE_BIND="${POCKET_BRIDGE_BIND:-${_env_bind:-0.0.0.0}}"

# ── WSL 偵測 ───────────────────────────────────────────────────────────────
IS_WSL=0
if [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qi microsoft /proc/version 2>/dev/null; then
  IS_WSL=1
fi

# ── 共用:鋪 payload(rsync + venv + 依賴)────────────────────────────────
install_payload() {  # $1=來源 checkout  $2=安裝目錄  $3=venv 目錄
  mkdir -p "$2"
  rsync -a --delete \
    --exclude venv --exclude .git --exclude __pycache__ \
    --exclude '*.log' --exclude '*.log.*' --exclude tests \
    "$1/" "$2/"
  [ -x "$3/bin/python" ] || python3 -m venv "$3"
  "$3/bin/python" -m pip install -q --upgrade pip
  "$3/bin/python" -m pip install -q -r "$2/requirements.txt"
}

# ── 共用:寫 bridge 的 systemd user unit ──────────────────────────────────
write_bridge_unit() {  # $1=RUN_ROOT  $2=RUN_VENV
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/$UNIT_NAME.service" <<UNIT
[Unit]
Description=Pocket bridge (self-hosted)
After=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
WorkingDirectory=$1
EnvironmentFile=$ENV_FILE
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=POCKET_BRIDGE_CRASH_STATE_DIR=%h/.local/state/pocket-lobster/bridge
Environment=POCKET_BRIDGE_CRASH_LOG=%h/.local/state/pocket-lobster/bridge/crash-loop.log
ExecStart=/bin/bash $1/deploy/bridge-supervisor.sh $2/bin/python -m uvicorn bridge:app --host $BRIDGE_BIND --port $BRIDGE_PORT --timeout-graceful-shutdown 3
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
UNIT
}

# ── payload 模式(lobster-update.sh 進來的門)──────────────────────────────
if [ -n "${POCKET_BRIDGE_INSTALL_ROOT:-}" ] && [ -n "${POCKET_BRIDGE_RUN_ROOT:-}" ]; then
  [ -f "$0" ] || die "payload 模式需要以檔案路徑執行(不是 stdin)"
  _script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
  _source_root="$(cd -- "$_script_dir/.." && pwd)"
  say "payload 模式:$_source_root → $POCKET_BRIDGE_INSTALL_ROOT"
  install_payload "$_source_root" "$POCKET_BRIDGE_INSTALL_ROOT" \
    "${POCKET_BRIDGE_VENV:-$POCKET_BRIDGE_INSTALL_ROOT/venv}"
  write_bridge_unit "$POCKET_BRIDGE_RUN_ROOT" \
    "${POCKET_BRIDGE_RUN_VENV:-$POCKET_BRIDGE_RUN_ROOT/venv}"
  systemctl --user daemon-reload 2>/dev/null || true
  exit 0
fi

# ── 旗標 ───────────────────────────────────────────────────────────────────
NO_START=0
FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --no-start) NO_START=1 ;;
    --force)    FORCE=1 ;;
    -h|--help)
      sed -n '2,36p' "$0" 2>/dev/null || true
      printf 'usage: selfhost-install.sh [--no-start] [--force]\n'; exit 0 ;;
    *) die "不認得的旗標:$1(支援 --no-start / --force)" ;;
  esac
  shift
done

# ── 前置檢查 1:發行版 ─────────────────────────────────────────────────────
DISTRO_ID=""
DISTRO_VER=""
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  DISTRO_ID="${ID:-}"
  DISTRO_VER="${VERSION_ID:-0}"
fi
distro_supported=0
case "$DISTRO_ID" in
  ubuntu|debian) distro_supported=1 ;;
esac
case "${ID_LIKE:-}" in
  *ubuntu*|*debian*) distro_supported=1 ;;
esac
if [ "$distro_supported" = 1 ]; then
  if command -v dpkg >/dev/null 2>&1; then
    if [ "$DISTRO_ID" = ubuntu ] && ! dpkg --compare-versions "$DISTRO_VER" ge 22.04; then
      [ "$FORCE" = 1 ] || die "Ubuntu $DISTRO_VER 太舊(需要 22.04+)。確定要硬上就加 --force"
      warn "Ubuntu $DISTRO_VER < 22.04,--force 強行續跑"
    fi
    if [ "$DISTRO_ID" = debian ] && ! dpkg --compare-versions "$DISTRO_VER" ge 12; then
      [ "$FORCE" = 1 ] || die "Debian $DISTRO_VER 太舊(需要 12+)。確定要硬上就加 --force"
      warn "Debian $DISTRO_VER < 12,--force 強行續跑"
    fi
  fi
else
  [ "$FORCE" = 1 ] || die "非 Ubuntu/Debian(ID=${DISTRO_ID:-unknown})—— 沒驗過。確定要硬上就加 --force"
  warn "非 Ubuntu/Debian(ID=${DISTRO_ID:-unknown}),--force 強行續跑;apt 相關步驟可能失敗"
fi
[ "$IS_WSL" = 1 ] && note "偵測到 WSL2(${WSL_DISTRO_NAME:-unknown})—— 結尾會印 Windows 側的打通步驟"

# ── 前置檢查 2:root / sudo ────────────────────────────────────────────────
SUDO=""
if [ "$(id -u)" = 0 ]; then
  warn "正以 root 執行。可以跑,但建議用一般使用者(服務會以執行者身分跑 per-user systemd)"
elif command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
fi

# ── 前置檢查 3:系統套件(先列清單,再一次裝)──────────────────────────────
MISSING=""
command -v git   >/dev/null 2>&1 || MISSING="$MISSING git"
command -v curl  >/dev/null 2>&1 || MISSING="$MISSING curl"
command -v rsync >/dev/null 2>&1 || MISSING="$MISSING rsync"
if command -v python3 >/dev/null 2>&1; then
  python3 -c 'import ensurepip' 2>/dev/null || MISSING="$MISSING python3-venv"
else
  MISSING="$MISSING python3 python3-venv"
fi
[ -f /etc/ssl/certs/ca-certificates.crt ] || MISSING="$MISSING ca-certificates"
# WSL 無 systemd 的退路要用 pgrep(見結尾);一起補,免得退路半路缺工具。
if [ "$IS_WSL" = 1 ] && ! command -v pgrep >/dev/null 2>&1; then
  MISSING="$MISSING procps"
fi

if [ -n "$MISSING" ]; then
  say "缺系統套件,將安裝:$MISSING"
  command -v apt-get >/dev/null 2>&1 \
    || die "找不到 apt-get,請先手動安裝:$MISSING"
  if [ "$(id -u)" != 0 ] && [ -z "$SUDO" ]; then
    die "缺套件且沒有 sudo。請先以 root 執行:apt-get install$MISSING"
  fi
  $SUDO apt-get update -qq || die "apt-get update 失敗"
  # shellcheck disable=SC2086
  $SUDO apt-get install -y -qq $MISSING \
    || die "apt 安裝失敗;請手動:${SUDO:+sudo }apt-get install$MISSING"
fi

# ── 前置檢查 4:Python 3.10+ ──────────────────────────────────────────────
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "需要 Python 3.10+(目前:$(python3 -V 2>&1))。Ubuntu 22.04+/Debian 12+ 內建版本即符合"

# ── 前置檢查 5:systemd 可不可用(WSL 常見沒開)───────────────────────────
SYSTEMD_OK=0
if command -v systemctl >/dev/null 2>&1; then
  _sd_state="$(systemctl is-system-running 2>/dev/null || true)"
  case "$_sd_state" in
    running|degraded|starting|maintenance) SYSTEMD_OK=1 ;;
  esac
fi
if [ "$NO_START" = 0 ] && [ "$SYSTEMD_OK" = 0 ]; then
  if [ "$IS_WSL" = 1 ]; then
    warn "WSL 裡 systemd 沒在跑 —— 改用 nohup 退路(開 shell 自動帶起 bridge)。"
    note "建議開 systemd:在 WSL 裡 sudo 編輯 /etc/wsl.conf 加入"
    note "  [boot]"
    note "  systemd=true"
    note "然後在 Windows PowerShell 跑 wsl --shutdown 再重進,重跑本安裝器即可轉正。"
  else
    die "需要 systemd(systemctl 不可用,狀態:${_sd_state:-無})。容器/CI 請加 --no-start"
  fi
fi

# ── 前置檢查 6:埠 8081(僅首次安裝需要空著;重跑時占用者就是舊 bridge)────
INSTALLED_TAG=""
[ -f "$CURRENT/.lobster-release" ] && INSTALLED_TAG="$(cat "$CURRENT/.lobster-release")"
if [ -z "$INSTALLED_TAG" ] && [ "$NO_START" = 0 ]; then
  python3 - "$BRIDGE_PORT" <<'PY' || die "埠 $BRIDGE_PORT 已被占用。換埠:POCKET_BRIDGE_PORT=8082 重跑;或先停掉占用的服務"
import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("0.0.0.0", int(sys.argv[1])))
except OSError:
    raise SystemExit(1)
finally:
    s.close()
PY
fi

# ── 取碼:釘最新 lobster-v* tag(同 deploy/install.sh 的釘法)──────────────
say "取得 Pocket bridge 原始碼($SRC)"
if [ ! -d "$SRC/.git" ]; then
  mkdir -p "$SRC"
  git -C "$SRC" init -q
  git -C "$SRC" remote add origin "$REPO"
fi
git -C "$SRC" remote set-url origin "$REPO"
git -C "$SRC" fetch -q --tags origin || die "連不上 $REPO"
REF="${POCKET_REF:-}"
if [ -z "$REF" ]; then
  REF="$(git -C "$SRC" tag -l 'lobster-v*' | sort -V | tail -1)"
  [ -n "$REF" ] || { note "(找不到 lobster-v* tag,退回 main)"; REF="origin/main"; }
fi
git -C "$SRC" checkout -q --force "$REF" 2>/dev/null \
  || git -C "$SRC" checkout -q --force "origin/${REF#origin/}" \
  || die "checkout $REF 失敗"
case "$REF" in
  lobster-v*) RELEASE_NAME="$REF" ;;
  *)          RELEASE_NAME="untagged-$(git -C "$SRC" rev-parse --short HEAD)" ;;
esac
say "版位:$RELEASE_NAME"
note "$(git -C "$SRC" log -1 --format='%h %s' | cut -c1-70)"

# ── BRIDGE_TOKEN(只生成一次;~/.pocket/bridge-token 是正本)────────────────
mkdir -p "$(dirname "$TOKEN_FILE")" "$ENV_DIR"
if [ -s "$TOKEN_FILE" ]; then
  say "沿用既有 BRIDGE_TOKEN($TOKEN_FILE)"
else
  # 舊安裝只把 token 放 bridge.env 的話,搬進 token 檔;否則新生成。
  _tok=""
  [ -f "$ENV_FILE" ] && _tok="$(sed -n 's/^BRIDGE_TOKEN=//p' "$ENV_FILE" | head -n1)" || true
  [ -n "$_tok" ] || _tok="$(python3 -c 'import secrets;print(secrets.token_urlsafe(36))')"
  say "產生 BRIDGE_TOKEN → $TOKEN_FILE"
  (umask 077; printf '%s\n' "$_tok" > "$TOKEN_FILE")
fi
chmod 600 "$TOKEN_FILE"
BRIDGE_TOKEN="$(head -n1 "$TOKEN_FILE" | tr -d '[:space:]')"
[ -n "$BRIDGE_TOKEN" ] || die "BRIDGE_TOKEN 是空的($TOKEN_FILE)"

# bridge.env:systemd EnvironmentFile + lobster-update.sh 探活都讀它。
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"
if ! grep -qxF "BRIDGE_TOKEN=$BRIDGE_TOKEN" "$ENV_FILE" 2>/dev/null; then
  grep -v '^BRIDGE_TOKEN=' "$ENV_FILE" > "$ENV_FILE.tmp" 2>/dev/null || true
  mv "$ENV_FILE.tmp" "$ENV_FILE"
  printf 'BRIDGE_TOKEN=%s\n' "$BRIDGE_TOKEN" >> "$ENV_FILE"
fi
env_default() { grep -q "^$1=" "$ENV_FILE" 2>/dev/null || printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"; }
env_default POCKET_BRIDGE_PORT "$BRIDGE_PORT"
env_default POCKET_BRIDGE_BIND "$BRIDGE_BIND"
env_default PAIR_BOOT_CODE_FILE "$BOOT_CODE_FILE"
# 更新器靠這行知道「這台是自架機」:每日自動更新要走本腳本的 payload 模式,
# 不能走完全體 install-ubuntu.sh(那會把 OpenClaw/frpc/tunnel 整套裝回來)。
env_default POCKET_BRIDGE_INSTALLER deploy/selfhost-install.sh
chmod 600 "$ENV_FILE"

# ── 配對 boot code(/pair/qr 的閘門;只生成一次)───────────────────────────
if [ ! -s "$BOOT_CODE_FILE" ]; then
  mkdir -p "$(dirname "$BOOT_CODE_FILE")"
  (umask 077; python3 -c 'import secrets;print(secrets.token_urlsafe(16))' > "$BOOT_CODE_FILE")
fi
chmod 600 "$BOOT_CODE_FILE"
BOOT_CODE="$(head -n1 "$BOOT_CODE_FILE" | tr -d '[:space:]')"

# ── 升級路徑:已裝 + 新 tag → 交棒 lobster-update.sh(快照/探活/回滾)──────
UPDATER="$SRC/deploy/lobster-update.sh"
DELEGATED=0
if [ -n "$INSTALLED_TAG" ] && [ "$RELEASE_NAME" != "$INSTALLED_TAG" ] \
   && [ "$NO_START" = 0 ] && [ "$SYSTEMD_OK" = 1 ] && [ -z "${POCKET_REF:-}" ] \
   && [ -f "$UPDATER" ] && grep -q POCKET_BRIDGE_INSTALLER "$UPDATER" \
   && [ -f "$SRC/deploy/selfhost-install.sh" ]; then
  case "$RELEASE_NAME" in lobster-v*)
    say "既有安裝($INSTALLED_TAG)→ 交棒 lobster-update.sh 升級到 $RELEASE_NAME"
    POCKET_BRIDGE_INSTALLER=deploy/selfhost-install.sh bash "$UPDATER" \
      || die "升級失敗(更新器已自動回滾到 $INSTALLED_TAG;log 在 $BASE/)"
    DELEGATED=1
  ;; esac
fi

# ── 直接安裝路徑(首裝、同版重跑、--no-start、或老 tag 的更新器不支援自架)──
if [ "$DELEGATED" = 0 ]; then
  if [ -e "$CURRENT" ] && [ ! -L "$CURRENT" ]; then
    die "$CURRENT 已存在且不是 symlink(雲端龍蝦舊佈局?)。自架安裝器不處理這種遷移,請先跑 deploy/lobster-update.sh"
  fi
  INSTALL_ROOT="$RELEASES/$RELEASE_NAME"
  say "安裝 bridge → $INSTALL_ROOT"
  install_payload "$SRC" "$INSTALL_ROOT" "$INSTALL_ROOT/venv"
  printf '%s\n' "$RELEASE_NAME" > "$INSTALL_ROOT/.lobster-release"
  mkdir -p "$RELEASES" "$STATE_DIR"
  ln -sfn "$INSTALL_ROOT" "$CURRENT"

  write_bridge_unit "$CURRENT" "$CURRENT/venv"

  # 每日自動更新(timer)—— 只在更新器支援自架模式時裝;老 tag 的更新器
  # 會把完全體龍蝦裝回來,寧可不自動更新(重跑本安裝器即可升級)。
  TIMER_OK=0
  if [ "$SYSTEMD_OK" = 1 ] && [ -f "$UPDATER" ] && grep -q POCKET_BRIDGE_INSTALLER "$UPDATER"; then
    mkdir -p "$HOME/.local/bin"
    install -m 0755 "$UPDATER" "$HOME/.local/bin/lobster-update.sh"
    cat > "$UNIT_DIR/pocket-bridge-update.service" <<'UNIT'
[Unit]
Description=Pocket bridge auto-update (oneshot)
[Service]
Type=oneshot
ExecStart=%h/.local/bin/lobster-update.sh
UNIT
    cat > "$UNIT_DIR/pocket-bridge-update.timer" <<'UNIT'
[Unit]
Description=Pocket bridge auto-update (daily)
[Timer]
OnBootSec=15min
OnUnitActiveSec=24h
RandomizedDelaySec=3600
[Install]
WantedBy=timers.target
UNIT
    TIMER_OK=1
  elif [ "$SYSTEMD_OK" = 1 ]; then
    note "此版位的 lobster-update.sh 尚不支援自架模式 —— 略過每日自動更新(升級=重跑本安裝器)"
  fi
fi

# ── --no-start:到 unit 檔為止 ─────────────────────────────────────────────
if [ "$NO_START" = 1 ]; then
  say "--no-start:略過 enable/start。之後手動啟動:"
  note "systemctl --user daemon-reload"
  note "systemctl --user enable --now $UNIT_NAME.service"
  [ "${TIMER_OK:-0}" = 1 ] && note "systemctl --user enable --now pocket-bridge-update.timer"
fi

# ── 啟動 ───────────────────────────────────────────────────────────────────
STARTED=0
if [ "$NO_START" = 0 ] && [ "$DELEGATED" = 0 ]; then
  if [ "$SYSTEMD_OK" = 1 ]; then
    # 重跑時常見從 ssh 進來沒有 user bus(雲端版實踩),補 XDG_RUNTIME_DIR。
    if [ -z "${XDG_RUNTIME_DIR:-}" ] && [ -d "/run/user/$(id -u)" ]; then
      XDG_RUNTIME_DIR="/run/user/$(id -u)"
      export XDG_RUNTIME_DIR
    fi
    say "啟動服務($UNIT_NAME.service)"
    systemctl --user daemon-reload \
      || die "systemctl --user 不可用(XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-未設});從 ssh 來請先 loginctl enable-linger $USER 再重跑"
    systemctl --user enable "$UNIT_NAME.service" >/dev/null 2>&1 || true
    # enable --now 對已在跑的服務是 no-op → 重跑吃不到新碼;一律 restart。
    systemctl --user restart "$UNIT_NAME.service"
    if [ "${TIMER_OK:-0}" = 1 ]; then
      systemctl --user enable --now pocket-bridge-update.timer >/dev/null 2>&1 \
        || note "update timer 啟用失敗(稍後手動:systemctl --user enable --now pocket-bridge-update.timer)"
    fi
    loginctl enable-linger "$USER" 2>/dev/null \
      || warn "enable-linger 失敗 —— 登出後服務會停;稍後手動:sudo loginctl enable-linger $USER"
  else
    # WSL 無 systemd 退路:nohup + ~/.profile 自啟。
    say "啟動 bridge(WSL nohup 退路)"
    mkdir -p "$HOME/.local/bin" "$STATE_DIR"
    cat > "$RUN_HELPER" <<HELPER
#!/bin/sh
# Pocket bridge 啟動器(WSL 無 systemd 退路)— selfhost-install.sh 產生
set -eu
set -a
. "$ENV_FILE"
set +a
export POCKET_BRIDGE_CRASH_STATE_DIR="$STATE_DIR"
export POCKET_BRIDGE_CRASH_LOG="$STATE_DIR/crash-loop.log"
cd "$CURRENT"
exec /bin/bash "$CURRENT/deploy/bridge-supervisor.sh" \\
  "$CURRENT/venv/bin/python" -m uvicorn bridge:app \\
  --host $BRIDGE_BIND --port $BRIDGE_PORT --timeout-graceful-shutdown 3
HELPER
    chmod +x "$RUN_HELPER"
    if ! grep -q 'pocket-bridge-autostart' "$HOME/.profile" 2>/dev/null; then
      cat >> "$HOME/.profile" <<PROFILE

# pocket-bridge-autostart:WSL 沒開 systemd 時,開 shell 自動帶起 bridge
if ! pgrep -f 'uvicorn bridge:app' >/dev/null 2>&1; then
  nohup "$RUN_HELPER" >> "$STATE_DIR/bridge.log" 2>&1 &
fi
PROFILE
      note "已加自啟段落到 ~/.profile(WSL 視窗全關後,下次開 shell 會自動帶起)"
    fi
    # 重跑=重啟:先收掉舊的(supervisor 也要收,不然它會把舊版再拉起來)。
    pgrep -f 'bridge-supervisor.sh' >/dev/null 2>&1 && pkill -f 'bridge-supervisor.sh' || true
    pgrep -f 'uvicorn bridge:app'   >/dev/null 2>&1 && pkill -f 'uvicorn bridge:app' || true
    sleep 1
    nohup "$RUN_HELPER" >> "$STATE_DIR/bridge.log" 2>&1 &
  fi

  say "等 bridge /health"
  _i=0
  while [ "$_i" -lt 30 ]; do
    if curl -fsS -m 2 "http://127.0.0.1:$BRIDGE_PORT/health" >/dev/null 2>&1; then
      STARTED=1; break
    fi
    sleep 1; _i=$((_i + 1))
  done
  if [ "$STARTED" = 0 ]; then
    if [ "$SYSTEMD_OK" = 1 ]; then
      die "bridge 沒起來。查:journalctl --user -u $UNIT_NAME -n 50"
    else
      die "bridge 沒起來。查:tail -50 $STATE_DIR/bridge.log"
    fi
  fi
elif [ "$DELEGATED" = 1 ]; then
  STARTED=1   # 更新器已重啟並行為探活過
fi

# ── 配對指引 ───────────────────────────────────────────────────────────────
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')" || LAN_IP=""
if [ -z "$LAN_IP" ]; then
  LAN_IP="$(python3 - <<'PY' 2>/dev/null
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("1.1.1.1", 80))
    print(s.getsockname()[0])
except OSError:
    pass
finally:
    s.close()
PY
)" || LAN_IP=""
fi

say "自架 bridge 安裝完成($RELEASE_NAME)。配對 Pocket App:"
printf '   1. bridge 位址: http://%s:%s\n' "${LAN_IP:-<這台機器的局網IP>}" "$BRIDGE_PORT"
printf '   2. token      : %s\n' "$BRIDGE_TOKEN"
printf '   3. 手機 Pocket → 設定 → 進階連線 → 手動位址,貼上位址與 token\n'

if [ "$STARTED" = 1 ]; then
  _qr_code="$(curl -s -m 4 -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:$BRIDGE_PORT/pair/qr?boot=$BOOT_CODE" 2>/dev/null)" || _qr_code=""
  if [ "$_qr_code" = 200 ]; then
    echo
    echo "   掃碼配對(QR 頁只服務本機/Tailscale;局網請走上面的手動位址):"
    echo "   http://127.0.0.1:$BRIDGE_PORT/pair/qr?boot=$BOOT_CODE"
    echo "   (無桌面環境:ssh -L $BRIDGE_PORT:127.0.0.1:$BRIDGE_PORT 這台機器,再用筆電瀏覽器開)"
    # 免掃碼配對連結(30 分鐘有效;連結即憑證,只傳給自己的裝置)
    _pair_link="$(curl -fsS -m 8 "http://127.0.0.1:$BRIDGE_PORT/pair/qr.json?ttl=1800&boot=$BOOT_CODE" 2>/dev/null \
      | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("payload","") if d.get("ok") else "")' 2>/dev/null)" || _pair_link=""
    if [ -n "$_pair_link" ]; then
      echo
      echo "   免掃碼:把這條配對連結傳到手機點開(30 分鐘內有效,勿外流):"
      echo "   $_pair_link"
    fi
  fi
fi

# ── WSL:Windows 側打通步驟 ────────────────────────────────────────────────
if [ "$IS_WSL" = 1 ]; then
  say "Windows(WSL2)還差一步:手機連的是 Windows 的局網 IP,要把埠打進 WSL(擇一)"
  echo "   A. mirrored networking(Win11 22H2+,建議):"
  echo "      在 Windows 的 %UserProfile%\\.wslconfig 加入:"
  echo "        [wsl2]"
  echo "        networkingMode=mirrored"
  echo "      然後 PowerShell 跑 wsl --shutdown 重進 —— WSL 直接共用 Windows 的 IP,上面第 1 行照用。"
  echo "   B. netsh portproxy(傳統):系統管理員 PowerShell:"
  echo "      netsh interface portproxy add v4tov4 listenport=$BRIDGE_PORT listenaddress=0.0.0.0 connectport=$BRIDGE_PORT connectaddress=${LAN_IP:-<WSL_IP>}"
  echo "      (WSL IP 重開機會變;變了就重跑這條,或改用 A)"
  echo "   防火牆放行(兩種都要,系統管理員 PowerShell):"
  echo "      netsh advfirewall firewall add rule name=\"Pocket Bridge $BRIDGE_PORT\" dir=in action=allow protocol=TCP localport=$BRIDGE_PORT"
  echo "   手機端第 1 行的位址改填 Windows 主機的局網 IP(ipconfig 查)。"
fi

# ── coding agents:自架版不代裝,自己的機器自己登入 ─────────────────────────
say "coding agents(自架版不代裝;裝好登入後 bridge 會自動偵測,App 對應頁籤就會亮)"
if command -v claude >/dev/null 2>&1; then
  note "Claude Code:已偵測到($(command -v claude))"
else
  note "Claude Code:未偵測到 → 安裝見 https://code.claude.com/docs/en/setup,裝完跑 claude 登入"
fi
if command -v codex >/dev/null 2>&1 || [ -x "$HOME/.local/bin/codex" ]; then
  note "Codex:已偵測到"
else
  note "Codex:未偵測到 → npm install -g @openai/codex,裝完跑 codex 登入"
fi
if command -v agy >/dev/null 2>&1 || [ -x "$HOME/.local/bin/agy" ]; then
  note "Gemini(Antigravity):已偵測到"
else
  note "Gemini:未偵測到 → curl -fsSL https://antigravity.google/cli/install.sh | bash,裝完跑 agy 登入"
fi

# ── 公網存取備註 ───────────────────────────────────────────────────────────
say "公網存取(自架版不裝 relay/frpc)"
note "局網外要連:建議 Tailscale(curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up)"
note "之後想要免 Tailscale 的固定公網位址,可在 App 內訂閱 Pocket Relay。"
[ "$NO_START" = 1 ] && { echo; note "(--no-start 模式:服務未啟動,上面的配對要等啟動後才有效)"; }
exit 0
