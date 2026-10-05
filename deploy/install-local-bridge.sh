#!/usr/bin/env bash
set -euo pipefail

# Install or upgrade the local Pocket bridge for the current macOS user.
# This script is intentionally per-user: tokens, LaunchAgents, Hermes homes, and
# OpenClaw config stay under the user's home directory and never reuse production
# Cloudflare tunnel or Telegram gateway state.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

log_step() {
  printf '==> %s\n' "$*" >&2
}

BRIDGE_LABEL="${POCKET_BRIDGE_LABEL:-com.pocketconnect.bridge}"
INSTALL_ROOT="${POCKET_BRIDGE_INSTALL_ROOT:-$HOME/Library/Application Support/PocketConnect/bridge/current}"
BRIDGE_VENV="${POCKET_BRIDGE_VENV:-$INSTALL_ROOT/venv}"
LAUNCH_AGENT="$HOME/Library/LaunchAgents/$BRIDGE_LABEL.plist"
HERMES_HOME_ROOT="${HERMES_HOME_ROOT:-$HOME/apps/hermes-agent/home}"
OPENCLAW_CONFIG_FILE="${OPENCLAW_CONFIG_FILE:-$HOME/.pocket/openclaw.json}"
PROVIDER_CHOICE="${POCKET_PROVIDER:-${POCKET_INSTALL_PROVIDER:-auto}}"
if [ -z "$PROVIDER_CHOICE" ] && [ "${POCKET_INSTALL_PROVIDERS:-0}" = "1" ]; then
  PROVIDER_CHOICE="auto"
fi
DEFAULT_PROVIDER="${POCKET_DEFAULT_PROVIDER:-hermes}"
INSTALL_HERMES="${POCKET_INSTALL_HERMES:-1}"
INSTALL_OPENCLAW="${POCKET_INSTALL_OPENCLAW:-1}"
HERMES_REPO_URL="${HERMES_REPO_URL:-https://github.com/NousResearch/hermes-agent.git}"
HERMES_INSTALL_DIR="${HERMES_INSTALL_DIR:-$HOME/apps/hermes-agent}"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.7.1-2}"
OPENCLAW_NODE_VERSION="${OPENCLAW_NODE_VERSION:-24.18.0}"
OPENCLAW_INSTALL_DIR="${OPENCLAW_INSTALL_DIR:-$HOME/apps/openclaw-clean}"
OPENCLAW_GATEWAY_TOKEN="${OPENCLAW_GATEWAY_TOKEN:-openclaw-dev-pocket-7f3a}"
OPENCLAW_LABEL="${OPENCLAW_LABEL:-com.pocketconnect.openclaw}"
OPENCLAW_LAUNCH_AGENT="$HOME/Library/LaunchAgents/$OPENCLAW_LABEL.plist"

find_existing_hermes_bin() {
  for candidate in \
    "$HOME/apps/hermes-agent/runtime/venv/bin/hermes" \
    "$HOME/apps/hermes-agent/venv/bin/hermes" \
    "$HOME/.local/bin/hermes"; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

detect_hermes_bin() {
  if find_existing_hermes_bin; then
    return 0
  fi
  printf '%s\n' "$HOME/apps/hermes-agent/venv/bin/hermes"
}

install_hermes_provider() {
  if [ "$INSTALL_HERMES" != "1" ]; then
    log_step "Hermes automatic install is disabled"
    return 0
  fi

  local existing_hermes
  existing_hermes="$(find_existing_hermes_bin || true)"
  if [ -n "$existing_hermes" ]; then
    HERMES_BIN="${HERMES_BIN:-$existing_hermes}"
    mkdir -p "$HERMES_HOME_ROOT" \
      "$HERMES_HOME_ROOT/profiles/fliper" \
      "$HERMES_HOME_ROOT/profiles/xcash" \
      "$HERMES_HOME_ROOT/profiles/shuijing" \
      "$HERMES_HOME_ROOT/uploads"
    log_step "Using existing Hermes"
    echo "Hermes already installed: $existing_hermes"
    return 0
  fi

  mkdir -p "$(dirname "$HERMES_INSTALL_DIR")"
  if [ ! -d "$HERMES_INSTALL_DIR/.git" ]; then
    log_step "Downloading Hermes"
    git clone "$HERMES_REPO_URL" "$HERMES_INSTALL_DIR"
  fi

  log_step "Preparing Hermes home"
  mkdir -p "$HERMES_HOME_ROOT" \
    "$HERMES_HOME_ROOT/profiles/fliper" \
    "$HERMES_HOME_ROOT/profiles/xcash" \
    "$HERMES_HOME_ROOT/profiles/shuijing" \
    "$HERMES_HOME_ROOT/uploads"

  if [ ! -x "$HERMES_INSTALL_DIR/venv/bin/hermes" ] && [ -x "$HERMES_INSTALL_DIR/setup-hermes.sh" ]; then
    log_step "Installing Hermes runtime"
    (
      cd "$HERMES_INSTALL_DIR"
      printf 'n\nn\n' | HERMES_HOME="$HERMES_HOME_ROOT" ./setup-hermes.sh
    )
  fi
}

install_node_for_openclaw() {
  local node_root="$HOME/apps/node-v$OPENCLAW_NODE_VERSION-darwin-arm64"
  if [ -x "$node_root/bin/node" ]; then
    log_step "Using existing Node runtime for OpenClaw"
    printf '%s\n' "$node_root"
    return 0
  fi

  log_step "Downloading Node runtime for OpenClaw"
  mkdir -p "$HOME/apps"
  local archive="$HOME/apps/node-v$OPENCLAW_NODE_VERSION-darwin-arm64.tar.xz"
  curl -fL "https://nodejs.org/dist/v$OPENCLAW_NODE_VERSION/node-v$OPENCLAW_NODE_VERSION-darwin-arm64.tar.xz" \
    -o "$archive"
  tar -xJf "$archive" -C "$HOME/apps"
  rm -f "$archive"
  printf '%s\n' "$node_root"
}

install_openclaw_provider() {
  if [ "$INSTALL_OPENCLAW" != "1" ]; then
    log_step "OpenClaw automatic install is disabled"
    return 0
  fi
  if [ -s "$OPENCLAW_CONFIG_FILE" ]; then
    log_step "Using existing OpenClaw configuration"
    echo "OpenClaw already configured: $OPENCLAW_CONFIG_FILE"
    return 0
  fi

  local node_root
  node_root="$(install_node_for_openclaw)"
  log_step "Installing OpenClaw"
  mkdir -p "$OPENCLAW_INSTALL_DIR/npm" "$OPENCLAW_INSTALL_DIR/home/state" "$(dirname "$OPENCLAW_CONFIG_FILE")"

  PATH="$node_root/bin:$PATH" npm install --prefix "$OPENCLAW_INSTALL_DIR/npm" "openclaw@$OPENCLAW_VERSION"
  PATH="$node_root/bin:$PATH" "$OPENCLAW_INSTALL_DIR/npm/node_modules/.bin/openclaw" --version

  if [ ! -f "$OPENCLAW_CONFIG_FILE" ]; then
    printf '{"base_url":"ws://127.0.0.1:19801","token":"%s"}\n' "$OPENCLAW_GATEWAY_TOKEN" \
      > "$OPENCLAW_CONFIG_FILE"
    chmod 600 "$OPENCLAW_CONFIG_FILE"
  fi

  cat > "$OPENCLAW_LAUNCH_AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$OPENCLAW_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$OPENCLAW_INSTALL_DIR/npm/node_modules/.bin/openclaw</string>
        <string>gateway</string>
        <string>run</string>
        <string>--port</string>
        <string>19801</string>
        <string>--bind</string>
        <string>loopback</string>
        <string>--auth</string>
        <string>token</string>
        <string>--allow-unconfigured</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$node_root/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>OPENCLAW_HOME</key>
        <string>$OPENCLAW_INSTALL_DIR/home</string>
        <key>OPENCLAW_STATE_DIR</key>
        <string>$OPENCLAW_INSTALL_DIR/home/state</string>
        <key>OPENCLAW_CONFIG_PATH</key>
        <string>$OPENCLAW_INSTALL_DIR/home/openclaw.json</string>
        <key>OPENCLAW_GATEWAY_TOKEN</key>
        <string>$OPENCLAW_GATEWAY_TOKEN</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$OPENCLAW_INSTALL_DIR/openclaw.out.log</string>
    <key>StandardErrorPath</key>
    <string>$OPENCLAW_INSTALL_DIR/openclaw.err.log</string>
</dict>
</plist>
PLIST

  chmod 600 "$OPENCLAW_LAUNCH_AGENT"
  plutil -lint "$OPENCLAW_LAUNCH_AGENT" >/dev/null
  log_step "Starting OpenClaw gateway"
  launchctl bootout "gui/$(id -u)/$OPENCLAW_LABEL" 2>/dev/null || true
  if launchctl bootstrap "gui/$(id -u)" "$OPENCLAW_LAUNCH_AGENT" 2>/dev/null; then
    launchctl kickstart -k "gui/$(id -u)/$OPENCLAW_LABEL" 2>/dev/null || true
  else
    echo "OpenClaw LaunchAgent was written but could not be bootstrapped from this context."
    echo "Run: launchctl bootstrap gui/$(id -u) \"$OPENCLAW_LAUNCH_AGENT\""
  fi
}

resolve_requested_provider() {
  case "$PROVIDER_CHOICE" in
    auto)
      if find_existing_hermes_bin >/dev/null 2>&1; then
        printf '%s\n' "hermes"
      elif [ -s "$OPENCLAW_CONFIG_FILE" ]; then
        printf '%s\n' "openclaw"
      else
        printf '%s\n' "$DEFAULT_PROVIDER"
      fi
      ;;
    hermes|openclaw|none|"")
      printf '%s\n' "${PROVIDER_CHOICE:-none}"
      ;;
    *)
      echo "Unsupported POCKET_PROVIDER=$PROVIDER_CHOICE (use auto, hermes, openclaw, or none)." >&2
      exit 2
      ;;
  esac
}

install_requested_provider() {
  REQUESTED_PROVIDER="$(resolve_requested_provider)"
  log_step "Selected provider: $REQUESTED_PROVIDER"
  case "$REQUESTED_PROVIDER" in
    hermes)
      install_hermes_provider
      ;;
    openclaw)
      install_openclaw_provider
      ;;
    none|"")
      log_step "Skipping AI provider automatic install"
      ;;
    *)
      echo "Unsupported resolved provider=$REQUESTED_PROVIDER (use auto, hermes, openclaw, or none)." >&2
      exit 2
      ;;
  esac
}

# bridge.py 需要 Python ≥3.10(127 處 PEP 604 型別語法)。macOS CLT 的
# /usr/bin/python3 是 3.9 —— 用它建 venv,import 當下就 SyntaxError,使用者
# 只看到「bridge 起不來」(2026-10-05 上線前盤查,乾淨 Mac 百分之百踩中)。
# 候選序與桌面 app 的 BridgeEnvironment 一致;POCKET_BRIDGE_PYTHON 可覆寫。
resolve_python() {
  for c in "${POCKET_BRIDGE_PYTHON:-}"            /opt/homebrew/bin/python3 /usr/local/bin/python3            "$HOME/.local/bin/python3" /usr/bin/python3; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      printf '%s' "$c"; return 0
    fi
  done
  return 1
}

install_bridge_runtime() {
  log_step "Preparing Pocket bridge runtime"
  BRIDGE_PY="$(resolve_python)" || {
    echo "✗ 找不到 Python 3.10 以上版本(系統內建的 3.9 跑不動 bridge)。" >&2
    echo "  安裝其一後重跑:  brew install python   或  https://www.python.org/downloads/macos/" >&2
    exit 1
  }
  if [ ! -x "$BRIDGE_VENV/bin/python" ]; then
    "$BRIDGE_PY" -m venv "$BRIDGE_VENV"
  else
    # 既有 venv 可能是舊 3.9 建的(踩雷後重跑的使用者)—— 驗一次,不合格就重建。
    if ! "$BRIDGE_VENV/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      echo "▸ 既有 venv 版本過舊,重建($BRIDGE_PY)"
      rm -rf "$BRIDGE_VENV"
      "$BRIDGE_PY" -m venv "$BRIDGE_VENV"
    fi
  fi
  "$BRIDGE_VENV/bin/python" -m pip install --upgrade pip >/dev/null
  "$BRIDGE_VENV/bin/python" -m pip install \
    fastapi uvicorn httpx PyJWT cryptography websockets python-multipart eval_type_backport >/dev/null
}

install_requested_provider

HERMES_BIN="${HERMES_BIN:-$(detect_hermes_bin)}"
BRIDGE_PYTHON="${BRIDGE_PYTHON:-$BRIDGE_VENV/bin/python}"
BRIDGE_TOKEN="${BRIDGE_TOKEN:-}"

read_existing_token() {
  if [ ! -f "$LAUNCH_AGENT" ]; then
    return 1
  fi
  /usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:BRIDGE_TOKEN" "$LAUNCH_AGENT" 2>/dev/null \
    | awk 'length($0) > 0 && $0 !~ /REPLACE_WITH_BRIDGE_TOKEN|CHANGE-ME/ { print; exit }'
}

if [ -z "$BRIDGE_TOKEN" ]; then
  BRIDGE_TOKEN="$(read_existing_token || true)"
fi
if [ -z "$BRIDGE_TOKEN" ]; then
  BRIDGE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
fi

mkdir -p "$INSTALL_ROOT" "$(dirname "$LAUNCH_AGENT")" "$HERMES_HOME_ROOT" "$(dirname "$OPENCLAW_CONFIG_FILE")"
log_step "Copying Pocket bridge"
rsync -a --delete \
  --exclude ".git" \
  --exclude "__pycache__" \
  --exclude "*.pyc" \
  --exclude "venv" \
  --exclude "bridge.out.log*" \
  --exclude "bridge.err.log*" \
  "$SOURCE_ROOT/" "$INSTALL_ROOT/"

install_bridge_runtime

log_step "Writing LaunchAgent"
cat > "$LAUNCH_AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$BRIDGE_LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$BRIDGE_PYTHON</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>bridge:app</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>8081</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$INSTALL_ROOT</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$(dirname "$HERMES_BIN"):$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>BRIDGE_TOKEN</key>
        <string>$BRIDGE_TOKEN</string>
        <key>HERMES_BIN</key>
        <string>$HERMES_BIN</string>
        <key>HERMES_HOME_ROOT</key>
        <string>$HERMES_HOME_ROOT</string>
        <key>OPENCLAW_CONFIG_FILE</key>
        <string>$OPENCLAW_CONFIG_FILE</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$INSTALL_ROOT/bridge.out.log</string>
    <key>StandardErrorPath</key>
    <string>$INSTALL_ROOT/bridge.err.log</string>
</dict>
</plist>
PLIST

chmod 600 "$LAUNCH_AGENT"
plutil -lint "$LAUNCH_AGENT" >/dev/null

launchctl bootout "gui/$(id -u)/$BRIDGE_LABEL" 2>/dev/null || true
log_step "Starting Pocket bridge"
if launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENT" 2>/dev/null; then
  launchctl kickstart -k "gui/$(id -u)/$BRIDGE_LABEL" 2>/dev/null || true
else
  echo "LaunchAgent was written but could not be bootstrapped from this context."
  echo "Run: launchctl bootstrap gui/$(id -u) \"$LAUNCH_AGENT\""
fi

log_step "Pocket bridge installed"
echo "Bridge installed: $INSTALL_ROOT"
echo "LaunchAgent: $LAUNCH_AGENT"
echo "Provider: $REQUESTED_PROVIDER"
echo "Health: http://127.0.0.1:8081/health"
