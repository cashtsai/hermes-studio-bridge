#!/usr/bin/env bash
set -euo pipefail

# Pocket 龍蝦主機 — Ubuntu 一鍵安裝(對應 install-local-bridge.sh 的 Linux 版)。
# 把一台閒置 Ubuntu 機器裝成可被 Pocket App QR 配對的 agent 主機:
#   bridge(FastAPI/uvicorn) + OpenClaw gateway(Node) [+ Hermes 選配]
#   + cloudflared quick tunnel(公網保底) + systemd user units(開機自啟)
# 結尾開 http://127.0.0.1:8081/pair/qr 掃碼配對;頁面會建議安裝 Tailscale
# (公網之外自建私網,外出連線最穩)。
#
# 與 mac 版共用同一套 env 覆蓋契約(POCKET_BRIDGE_LABEL/INSTALL_ROOT/
# POCKET_PROVIDER/OPENCLAW_*/HERMES_*),差異只在平台黏合層:
#   LaunchAgent → systemd user unit;~/Library → XDG 路徑;plutil → systemd-analyze。
# 本腳本 per-user、不碰 root 常駐;sudo 只用於 apt 缺件補裝(可拒絕,改手動裝)。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

log_step() { printf '==> %s\n' "$*" >&2; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "此腳本只支援 Linux(macOS 請用 install-local-bridge.sh)"

# ── 佈局(env 可覆蓋;預設走 XDG) ──────────────────────────────────────────
BRIDGE_LABEL="${POCKET_BRIDGE_LABEL:-pocket-bridge}"
INSTALL_ROOT="${POCKET_BRIDGE_INSTALL_ROOT:-$HOME/.local/share/pocket-lobster/bridge/current}"
BRIDGE_VENV="${POCKET_BRIDGE_VENV:-$INSTALL_ROOT/venv}"
BRIDGE_PORT="${POCKET_BRIDGE_PORT:-8081}"
ENV_DIR="${POCKET_ENV_DIR:-$HOME/.config/pocket-lobster}"
ENV_FILE="$ENV_DIR/bridge.env"
UNIT_DIR="$HOME/.config/systemd/user"
HERMES_HOME_ROOT="${HERMES_HOME_ROOT:-$HOME/apps/hermes-agent/home}"
OPENCLAW_CONFIG_FILE="${OPENCLAW_CONFIG_FILE:-$HOME/.pocket/openclaw.json}"
PROVIDER_CHOICE="${POCKET_PROVIDER:-${POCKET_INSTALL_PROVIDER:-auto}}"
DEFAULT_PROVIDER="${POCKET_DEFAULT_PROVIDER:-openclaw}"   # 龍蝦預設 openclaw(mac 版預設 hermes)
INSTALL_HERMES="${POCKET_INSTALL_HERMES:-0}"              # Hermes 需另行配置,Ubuntu 預設關
INSTALL_OPENCLAW="${POCKET_INSTALL_OPENCLAW:-1}"
HERMES_REPO_URL="${HERMES_REPO_URL:-https://github.com/NousResearch/hermes-agent.git}"
HERMES_INSTALL_DIR="${HERMES_INSTALL_DIR:-$HOME/apps/hermes-agent}"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.7.1-2}"
OPENCLAW_NODE_VERSION="${OPENCLAW_NODE_VERSION:-24.18.0}"
OPENCLAW_INSTALL_DIR="${OPENCLAW_INSTALL_DIR:-$HOME/apps/openclaw-clean}"
TUNNEL_URL_FILE="${POCKET_TUNNEL_URL_FILE:-$HOME/.pocket/tunnel-url}"
INSTALL_TUNNEL="${POCKET_INSTALL_TUNNEL:-1}"

case "$(uname -m)" in
  x86_64)  NODE_ARCH=linux-x64;  CF_ARCH=amd64 ;;
  aarch64) NODE_ARCH=linux-arm64; CF_ARCH=arm64 ;;
  *) die "不支援的架構: $(uname -m)" ;;
esac

# ── 系統前置(apt) ─────────────────────────────────────────────────────────
need_pkgs=()
command -v python3 >/dev/null || need_pkgs+=(python3)
python3 -c 'import venv' 2>/dev/null || need_pkgs+=(python3-venv)
command -v tmux  >/dev/null || need_pkgs+=(tmux)
command -v git   >/dev/null || need_pkgs+=(git)
command -v curl  >/dev/null || need_pkgs+=(curl)
command -v xz    >/dev/null || need_pkgs+=(xz-utils)
if [ "${#need_pkgs[@]}" -gt 0 ]; then
  log_step "缺系統套件: ${need_pkgs[*]}"
  if command -v sudo >/dev/null; then
    sudo apt-get update -qq && sudo apt-get install -y -qq "${need_pkgs[@]}" \
      || die "apt 安裝失敗;請手動: sudo apt-get install ${need_pkgs[*]}"
  else
    die "請先安裝: apt-get install ${need_pkgs[*]}"
  fi
fi
command -v systemctl >/dev/null || die "需要 systemd(systemctl 不存在)"

# ── BRIDGE_TOKEN(裝置配對的 master token;既有就沿用) ─────────────────────
mkdir -p "$ENV_DIR" "$(dirname "$TUNNEL_URL_FILE")"
if [ -f "$ENV_FILE" ] && grep -q '^BRIDGE_TOKEN=' "$ENV_FILE"; then
  log_step "沿用既有 BRIDGE_TOKEN($ENV_FILE)"
else
  log_step "產生新 BRIDGE_TOKEN"
  printf 'BRIDGE_TOKEN=%s\n' "$(python3 -c 'import secrets;print(secrets.token_urlsafe(36))')" >> "$ENV_FILE"
fi
grep -q '^POCKET_BRIDGE_PORT=' "$ENV_FILE" || printf 'POCKET_BRIDGE_PORT=%s\n' "$BRIDGE_PORT" >> "$ENV_FILE"
grep -q '^POCKET_TUNNEL_URL_FILE=' "$ENV_FILE" || printf 'POCKET_TUNNEL_URL_FILE=%s\n' "$TUNNEL_URL_FILE" >> "$ENV_FILE"
chmod 600 "$ENV_FILE"

# ── bridge 本體 + venv ────────────────────────────────────────────────────
log_step "安裝 bridge 到 $INSTALL_ROOT"
mkdir -p "$INSTALL_ROOT"
rsync -a --delete \
  --exclude 'venv' --exclude '.git' --exclude '__pycache__' \
  --exclude '*.log' --exclude '*.log.*' --exclude 'tests' \
  "$SOURCE_ROOT/" "$INSTALL_ROOT/"
if [ ! -x "$BRIDGE_VENV/bin/python" ]; then
  python3 -m venv "$BRIDGE_VENV"
fi
"$BRIDGE_VENV/bin/python" -m pip install -q --upgrade pip
"$BRIDGE_VENV/bin/python" -m pip install -q -r "$INSTALL_ROOT/requirements.txt"

# ── OpenClaw provider ─────────────────────────────────────────────────────
NODE_ROOT="$HOME/apps/node-v$OPENCLAW_NODE_VERSION-$NODE_ARCH"
install_node() {
  [ -x "$NODE_ROOT/bin/node" ] && return 0
  log_step "下載 Node $OPENCLAW_NODE_VERSION($NODE_ARCH)"
  mkdir -p "$HOME/apps"
  local archive="$HOME/apps/node-v$OPENCLAW_NODE_VERSION-$NODE_ARCH.tar.xz"
  curl -fsSL "https://nodejs.org/dist/v$OPENCLAW_NODE_VERSION/node-v$OPENCLAW_NODE_VERSION-$NODE_ARCH.tar.xz" -o "$archive"
  tar -xJf "$archive" -C "$HOME/apps"
  rm -f "$archive"
}

if [ "$INSTALL_OPENCLAW" = "1" ]; then
  install_node
  if [ ! -x "$OPENCLAW_INSTALL_DIR/npm/node_modules/.bin/openclaw" ]; then
    log_step "安裝 OpenClaw $OPENCLAW_VERSION"
    mkdir -p "$OPENCLAW_INSTALL_DIR/npm" "$OPENCLAW_INSTALL_DIR/home/state"
    PATH="$NODE_ROOT/bin:$PATH" npm install --silent --prefix "$OPENCLAW_INSTALL_DIR/npm" "openclaw@$OPENCLAW_VERSION"
  fi
  PATH="$NODE_ROOT/bin:$PATH" "$OPENCLAW_INSTALL_DIR/npm/node_modules/.bin/openclaw" --version >/dev/null
  # gateway token:每台隨機(mac 版的固定預設是 dev 靶機遺留,公開安裝不可沿用)
  if [ -z "${OPENCLAW_GATEWAY_TOKEN:-}" ]; then
    if [ -s "$OPENCLAW_CONFIG_FILE" ]; then
      OPENCLAW_GATEWAY_TOKEN="$(python3 -c "import json;print(json.load(open('$OPENCLAW_CONFIG_FILE')).get('token',''))")"
    fi
    [ -n "${OPENCLAW_GATEWAY_TOKEN:-}" ] || OPENCLAW_GATEWAY_TOKEN="$(python3 -c 'import secrets;print("oc-"+secrets.token_urlsafe(24))')"
  fi
  mkdir -p "$(dirname "$OPENCLAW_CONFIG_FILE")"
  if [ ! -s "$OPENCLAW_CONFIG_FILE" ]; then
    printf '{"base_url":"ws://127.0.0.1:19801","token":"%s"}\n' "$OPENCLAW_GATEWAY_TOKEN" > "$OPENCLAW_CONFIG_FILE"
    chmod 600 "$OPENCLAW_CONFIG_FILE"
  fi
fi

# ── Hermes provider(選配) ────────────────────────────────────────────────
if [ "$INSTALL_HERMES" = "1" ] && [ ! -x "$HERMES_INSTALL_DIR/runtime/venv/bin/hermes" ]; then
  log_step "安裝 Hermes(選配)"
  [ -d "$HERMES_INSTALL_DIR/.git" ] || git clone -q "$HERMES_REPO_URL" "$HERMES_INSTALL_DIR"
  python3 -m venv "$HERMES_INSTALL_DIR/runtime/venv"
  "$HERMES_INSTALL_DIR/runtime/venv/bin/pip" install -q -e "$HERMES_INSTALL_DIR"
  mkdir -p "$HERMES_HOME_ROOT"
fi

# ── cloudflared(公網保底 quick tunnel) ──────────────────────────────────
CLOUDFLARED_BIN="$HOME/.local/bin/cloudflared"
if [ "$INSTALL_TUNNEL" = "1" ] && [ ! -x "$CLOUDFLARED_BIN" ]; then
  if command -v cloudflared >/dev/null; then
    CLOUDFLARED_BIN="$(command -v cloudflared)"
  else
    log_step "下載 cloudflared($CF_ARCH)"
    mkdir -p "$HOME/.local/bin"
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$CF_ARCH" -o "$CLOUDFLARED_BIN"
    chmod +x "$CLOUDFLARED_BIN"
  fi
fi

# ── systemd user units ────────────────────────────────────────────────────
log_step "寫 systemd user units"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/$BRIDGE_LABEL.service" <<UNIT
[Unit]
Description=Pocket bridge (lobster host)
After=network-online.target

[Service]
WorkingDirectory=$INSTALL_ROOT
EnvironmentFile=$ENV_FILE
Environment=PATH=$NODE_ROOT/bin:%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$BRIDGE_VENV/bin/python -m uvicorn bridge:app --host 0.0.0.0 --port $BRIDGE_PORT --timeout-graceful-shutdown 3
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
UNIT

if [ "$INSTALL_OPENCLAW" = "1" ]; then
cat > "$UNIT_DIR/pocket-openclaw.service" <<UNIT
[Unit]
Description=OpenClaw gateway (Pocket lobster)
After=network-online.target

[Service]
Environment=PATH=$NODE_ROOT/bin:/usr/local/bin:/usr/bin:/bin
Environment=OPENCLAW_HOME=$OPENCLAW_INSTALL_DIR/home
Environment=OPENCLAW_STATE_DIR=$OPENCLAW_INSTALL_DIR/home/state
Environment=OPENCLAW_CONFIG_PATH=$OPENCLAW_INSTALL_DIR/home/openclaw.json
Environment=OPENCLAW_GATEWAY_TOKEN=$OPENCLAW_GATEWAY_TOKEN
ExecStart=$OPENCLAW_INSTALL_DIR/npm/node_modules/.bin/openclaw gateway run --port 19801 --bind loopback --auth token --allow-unconfigured
Restart=always
RestartSec=5
# openclaw 冷啟(插件載入)可達 30s-2min,別讓 systemd 誤判失敗連環重啟
TimeoutStartSec=180

[Install]
WantedBy=default.target
UNIT
fi

if [ "$INSTALL_TUNNEL" = "1" ]; then
# quick tunnel URL 每次啟動都會變 → wrapper 從 stderr 撈 trycloudflare URL
# 寫進 $TUNNEL_URL_FILE,bridge 的 /pair/qr 讀它當公網保底候選。
cat > "$ENV_DIR/tunnel-run.sh" <<WRAP
#!/usr/bin/env bash
set -uo pipefail
: > "$TUNNEL_URL_FILE"
exec "$CLOUDFLARED_BIN" tunnel --no-autoupdate --url "http://127.0.0.1:$BRIDGE_PORT" 2>&1 | \
  while IFS= read -r line; do
    printf '%s\n' "\$line"
    url=\$(printf '%s' "\$line" | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)
    if [ -n "\$url" ]; then printf '%s\n' "\$url" > "$TUNNEL_URL_FILE"; fi
  done
WRAP
chmod +x "$ENV_DIR/tunnel-run.sh"
cat > "$UNIT_DIR/pocket-tunnel.service" <<UNIT
[Unit]
Description=Cloudflare quick tunnel (Pocket lobster public fallback)
After=$BRIDGE_LABEL.service

[Service]
ExecStart=$ENV_DIR/tunnel-run.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT
fi

systemctl --user daemon-reload
loginctl enable-linger "$USER" 2>/dev/null || log_step "enable-linger 失敗(無 sudo?)— 登出後服務會停,可稍後手動: sudo loginctl enable-linger $USER"

log_step "啟動服務"
systemctl --user enable --now "$BRIDGE_LABEL.service"
[ "$INSTALL_OPENCLAW" = "1" ] && systemctl --user enable --now pocket-openclaw.service
[ "$INSTALL_TUNNEL" = "1" ] && systemctl --user enable --now pocket-tunnel.service

# ── 等 bridge 起來 → 開配對頁 ────────────────────────────────────────────
log_step "等 bridge /health"
for _ in $(seq 1 30); do
  if curl -fsS -m 2 "http://127.0.0.1:$BRIDGE_PORT/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS -m 2 "http://127.0.0.1:$BRIDGE_PORT/health" >/dev/null || die "bridge 未起來;journalctl --user -u $BRIDGE_LABEL -n 50 查原因"

PAIR_URL="http://127.0.0.1:$BRIDGE_PORT/pair/qr"
echo
echo "✅ 龍蝦主機就緒。用 Pocket App 掃碼配對:"
echo "   $PAIR_URL"
# 雲端/headless 免掃碼:直接給配對連結(30 分鐘有效),傳到手機點開即配。
PAIR_LINK="$(curl -fsS -m 8 "http://127.0.0.1:$BRIDGE_PORT/pair/qr.json?ttl=1800" 2>/dev/null \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("payload","") if d.get("ok") else "")' 2>/dev/null || true)"
if [ -n "$PAIR_LINK" ]; then
  echo
  echo "📎 免掃碼(雲端主機):把下面這條配對連結傳到手機(iMessage/mail 皆可),點開即配對(30 分鐘內有效):"
  echo "   $PAIR_LINK"
fi
if ! command -v tailscale >/dev/null; then
  echo
  echo "💡 建議:除了公網通道,自己用 Tailscale 建私網(外出連線最穩、免固定 IP):"
  echo "   curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up"
fi
if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xdg-open >/dev/null; then
  xdg-open "$PAIR_URL" >/dev/null 2>&1 || true
else
  echo "(無桌面環境:可從別台機器 ssh -L $BRIDGE_PORT:127.0.0.1:$BRIDGE_PORT 後開上面網址)"
fi
