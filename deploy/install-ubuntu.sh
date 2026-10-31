#!/usr/bin/env bash
set -euo pipefail

# Pocket 龍蝦主機 — Ubuntu 一鍵安裝(對應 install-local-bridge.sh 的 Linux 版)。
# 把一台閒置 Ubuntu 機器裝成可被 Pocket App QR 配對的 agent 主機:
#   bridge(FastAPI/uvicorn) + OpenClaw gateway(Node) [+ Hermes 選配]
#   + cloudflared quick tunnel(公網保底) + systemd user units(開機自啟)
# 結尾開 http://127.0.0.1:8081/pair/qr?boot=<code> 掃碼配對(boot code 為
# 安裝時產生的一次性閘門,詳見 bridge.py _pair_check_boot);頁面會建議安裝
# Tailscale(公網之外自建私網,外出連線最穩)。
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
# ── CC / Codex(2026-10-31 補)─────────────────────────────────────────────
# 在這之前,這支安裝器**完全沒有裝 Claude Code 或 Codex**(grep 是 0 次)。
# 於是一台照它裝出來的龍蝦,`_host_capabilities()` 回 cc:false / cx:false
# —— app 上那兩個頁籤是關的。而「把你的 coding agent 裝進口袋」正是 Pocket
# 整個產品的賣點。這是龍蝦這條線最大的功能缺口,不是 Hermes vs OpenClaw。
#
# **刻意不碰憑證。** 兩者都只裝執行檔;登入由使用者自己在 Pocket 的內建
# SSH 終端機裡跑 `claude` / `codex` 完成。我們一行 token 都不經手 ——
# 這同時也是定價文件那條原則「不把 agent credentials 放到 Pocket 或 Relay」
# 在龍蝦上唯一站得住的做法。
INSTALL_CLAUDE="${POCKET_INSTALL_CLAUDE:-1}"
INSTALL_CODEX="${POCKET_INSTALL_CODEX:-1}"
# Codex 走 npm(官方套件 @openai/codex,bin=codex);版本釘死,理由同 openclaw。
CODEX_VERSION="${CODEX_VERSION:-0.166.0}"
HERMES_REPO_URL="${HERMES_REPO_URL:-https://github.com/NousResearch/hermes-agent.git}"
HERMES_INSTALL_DIR="${HERMES_INSTALL_DIR:-$HOME/apps/hermes-agent}"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.7.1-2}"
OPENCLAW_NODE_VERSION="${OPENCLAW_NODE_VERSION:-24.18.0}"
OPENCLAW_INSTALL_DIR="${OPENCLAW_INSTALL_DIR:-$HOME/apps/openclaw-clean}"
TUNNEL_URL_FILE="${POCKET_TUNNEL_URL_FILE:-$HOME/.pocket/tunnel-url}"
INSTALL_TUNNEL="${POCKET_INSTALL_TUNNEL:-1}"
# cloudflared 版本釘死(同 node/openclaw 的釘法;latest 會讓安裝不可重現)
CLOUDFLARED_VERSION="${CLOUDFLARED_VERSION:-2026.7.3}"
# 配對頁 boot code(/pair/qr 的閘門;tunnel 下 loopback 不可信,見 bridge.py)
PAIR_BOOT_CODE_FILE="${PAIR_BOOT_CODE_FILE:-$HOME/.pocket/pair-boot-code}"
# bridge 綁定位址:預設只綁 loopback —— tunnel 是唯一預期入口。
# 若要讓 LAN 裝置直連(不經 tunnel/Tailscale),明確覆蓋 POCKET_BRIDGE_BIND=0.0.0.0。
BRIDGE_BIND="${POCKET_BRIDGE_BIND:-127.0.0.1}"

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
grep -q '^PAIR_BOOT_CODE_FILE=' "$ENV_FILE" || printf 'PAIR_BOOT_CODE_FILE=%s\n' "$PAIR_BOOT_CODE_FILE" >> "$ENV_FILE"
# ── Codex:龍蝦上關家目錄隔離,改用 managed daemon(2026-10-31)──────────
#
# **為什麼**:機主的目標是「cc / cx 要能持久」。Codex 的持久性(回合跨得過
# bridge 重啟)來自 managed daemon —— 它獨立於 bridge 行程。但 bridge 有一條
# 鐵則:**隔離開啟 ⇒ 一定自己 spawn stdio、不碰 daemon**(見 bridge.py 的
# `_codex_isolated` 區塊)。而 `codex_home.isolation_enabled()` 在**非 macOS
# 預設開隔離**,理由寫著「龍蝦那台無頭 Ubuntu 根本沒有 daemon」。
#
# 那個前提被本安裝器推翻了 —— 我們**裝了** pocket-codex-daemon。於是隔離預設
# 把 daemon 永遠排除掉,Codex 退回 stdio、隨 bridge 死,持久性拿不到。
# (2026-10-31 第一台實機就是這樣:codex session 一律 `codex binary
#  unavailable`,追到底是隔離規則強制 stdio。)
#
# 關隔離的代價在龍蝦上是**零**:隔離防的是「桌面版與 Pocket 搶同一個
# ~/.codex 的 writer lock」,而龍蝦是單使用者、沒有桌面版 —— 沒有東西可搶。
# 換來的正是機主要的持久性。
grep -q '^POCKET_CODEX_ISOLATED=' "$ENV_FILE" || printf 'POCKET_CODEX_ISOLATED=%s\n' "${POCKET_CODEX_ISOLATED:-0}" >> "$ENV_FILE"
# ── 選定 provider(2026-10-31 補:0/0 空人格的根因)─────────────────────────
#
# `DEFAULT_PROVIDER` 一直被算出來(龍蝦=openclaw)卻**從沒寫進 bridge.env**,
# 所以 bridge 的 `_dashboard_active_provider()` 讀不到 POCKET_ACTIVE_PROVIDER →
# 回 "auto"。後果:`/app/v1/personas`(設定頁人格管理)不走 OpenClaw 分支,掉回
# Hermes 內建清單 —— 龍蝦上那是空的 → 人格管理一個都沒有。寫上它,人格分頁才會
# 顯示 OpenClaw 的主 session(對外名「Pocket」)。
grep -q '^POCKET_ACTIVE_PROVIDER=' "$ENV_FILE" || printf 'POCKET_ACTIVE_PROVIDER=%s\n' "$DEFAULT_PROVIDER" >> "$ENV_FILE"
# ── 桌面控制台(2026-10-31 補)───────────────────────────────────────────
#
# 在這之前安裝器**從來沒設過** POCKET_CONSOLE_ENABLED(預設 0),所以龍蝦上
# `/console` 一律 404。但那台機器根本沒有 GUI —— 它就該用瀏覽器控制台。
#
# `/console` 是純 FastAPI 路由 + static/console/ 的靜態檔,**零平台相依**,
# 而且那些檔案本來就會被 rsync 進 payload。也就是說「Ubuntu 桌面端」其實
# 早就在了,只差沒打開。不需要為 Linux 另外寫一個 GUI app。
#
# QR 登入一併開:雲端龍蝦**沒有「本機」可言**,每一次存取都是遠端,
# 而遠端走 `_pair_local_only` 一律裸 403。沒有 QR 登入 = 控制台開了也進不去。
#   → 遠端開頁 → 顯示 QR → 已配對的手機批准 → 進去(見 CONSOLE_QR_LOGIN_SPEC)
CONSOLE_ENABLED="${POCKET_CONSOLE_ENABLED:-1}"
CONSOLE_QR_LOGIN="${POCKET_CONSOLE_QR_LOGIN:-1}"
# **檔案面預設一個都不開。** 遠端控制台的能力降級規則:沒設這個變數就等於
# 看不到任何檔案。龍蝦上使用者自己的工作目錄要不要開放,由他自己決定 ——
# 我們不替他把家目錄掛上去。設法:POCKET_CONSOLE_REMOTE_FILE_ROOTS=/path:/path2
CONSOLE_FILE_ROOTS="${POCKET_CONSOLE_REMOTE_FILE_ROOTS:-}"
grep -q '^POCKET_CONSOLE_ENABLED=' "$ENV_FILE" || printf 'POCKET_CONSOLE_ENABLED=%s\n' "$CONSOLE_ENABLED" >> "$ENV_FILE"
grep -q '^POCKET_CONSOLE_QR_LOGIN=' "$ENV_FILE" || printf 'POCKET_CONSOLE_QR_LOGIN=%s\n' "$CONSOLE_QR_LOGIN" >> "$ENV_FILE"
grep -q '^POCKET_CONSOLE_REMOTE_FILE_ROOTS=' "$ENV_FILE" || printf 'POCKET_CONSOLE_REMOTE_FILE_ROOTS=%s\n' "$CONSOLE_FILE_ROOTS" >> "$ENV_FILE"
# ── App 內 agent 登入(2026-10-31:龍蝦沒有「你的 Mac」可去桌面控制台登入)──
# 預設**開**:雲端龍蝦上 CC/Codex/Gemini 的登入只能在手機 App 裡按鈕完成
# (CC setup-token / Codex device-auth / Gemini 貼 key)。旗標在 bridge.py 預設
# 關(404),mac 版維持關(走桌面控制台);龍蝦這台一定要開,否則三個引擎都登不了。
grep -q '^POCKET_AGENT_LOGIN_IN_APP=' "$ENV_FILE" || printf 'POCKET_AGENT_LOGIN_IN_APP=%s\n' "${POCKET_AGENT_LOGIN_IN_APP:-1}" >> "$ENV_FILE"
chmod 600 "$ENV_FILE"

# ── 配對 boot code(既有就沿用;bridge 啟動也會自產,這裡先產好以便印連結) ──
if [ ! -s "$PAIR_BOOT_CODE_FILE" ]; then
  log_step "產生配對 boot code"
  mkdir -p "$(dirname "$PAIR_BOOT_CODE_FILE")"
  (umask 077; python3 -c 'import secrets;print(secrets.token_urlsafe(16))' > "$PAIR_BOOT_CODE_FILE")
fi
chmod 600 "$PAIR_BOOT_CODE_FILE"
PAIR_BOOT_CODE="$(head -n1 "$PAIR_BOOT_CODE_FILE" | tr -d '[:space:]')"
[ -n "$PAIR_BOOT_CODE" ] || die "boot code 產生失敗($PAIR_BOOT_CODE_FILE)"

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

# Codex 也需要 Node —— 原本 install_node 只在 OpenClaw 區塊裡被呼叫,
# 關掉 OpenClaw 但要 Codex 時會拿不到 node。
if [ "$INSTALL_OPENCLAW" = "1" ] || [ "$INSTALL_CODEX" = "1" ]; then
  install_node
fi

if [ "$INSTALL_OPENCLAW" = "1" ]; then
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
  # ── gateway 預設模型(2026-10-31:開箱就有一個免費線上模型可用)────────────
  #
  # OpenClaw 的 gateway 設定(models.providers / agents.defaults)在
  # $OPENCLAW_INSTALL_DIR/home/openclaw.json。原本安裝器完全不寫它 → 新機
  # gateway 跑得起來(--allow-unconfigured)但沒有任何模型,機主得自己手接。
  # 這裡預設接上 OpenRouter 的免費模型(:free 不需信用卡),讓「預設 Pocket
  # 人格」一裝好就能回話。api 明確寫 openai-completions(補掉 gateway 日誌那句
  # `no "api" specified` 警告;實機已證模型回覆正常,這只是清掉雜訊)。
  #
  # **API key 絕不寫死在這支腳本(它在公開 repo)。** 由安裝時的環境變數
  # POCKET_OPENROUTER_KEY 帶進來;沒給就留空字串 —— gateway 照跑,免費模型要
  # 等機主把 key 填進去(app 設定頁或重跑安裝器帶 key)才會真的呼叫。
  OPENCLAW_GATEWAY_CONFIG="$OPENCLAW_INSTALL_DIR/home/openclaw.json"
  OPENROUTER_FREE_MODEL="${POCKET_OPENROUTER_MODEL:-nvidia/nemotron-3.5-super-vl:free}"
  if [ ! -s "$OPENCLAW_GATEWAY_CONFIG" ]; then
    mkdir -p "$(dirname "$OPENCLAW_GATEWAY_CONFIG")"
    OPENROUTER_KEY="${POCKET_OPENROUTER_KEY:-}" OPENROUTER_MODEL="$OPENROUTER_FREE_MODEL" \
      python3 - "$OPENCLAW_GATEWAY_CONFIG" <<'PY'
import json, os, sys
path = sys.argv[1]
model = os.environ["OPENROUTER_MODEL"]
cfg = {
    "models": {"providers": {"openrouter": {
        "baseUrl": "https://openrouter.ai/api/v1",
        "apiKey": os.environ.get("OPENROUTER_KEY", ""),
        "api": "openai-completions",
        "models": [{"id": model, "name": model}],
    }}},
    "agents": {"defaults": {"model": "openrouter/" + model}},
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
os.chmod(path, 0o600)
PY
    if [ -n "${POCKET_OPENROUTER_KEY:-}" ]; then
      log_step "  gateway 預設模型:openrouter/$OPENROUTER_FREE_MODEL(已帶 key)"
    else
      printf '⚠️  gateway 預設模型已設 openrouter/%s,但**沒帶 API key** —— 免費模型要填 key 才會回話。\n' "$OPENROUTER_FREE_MODEL" >&2
      printf '    取得免費 key:https://openrouter.ai/keys(:free 模型不需信用卡)。填法:app 設定頁,或帶 POCKET_OPENROUTER_KEY=... 重跑安裝器。\n' >&2
    fi
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

# ── Claude Code / Codex(使用者自備帳號,我們只裝執行檔)────────────────────
#
# 失敗一律不致命:少了它們這台機器仍然可用(OpenClaw、終端機、檔案都還在),
# 把整支安裝器弄掛才是真的壞。但要**講清楚少了什麼**,不要安靜地少一個東西。
#
# Claude Code 走**官方簽章 apt repo**,不走管道式安裝腳本:
#   · 套件與 repo 都有 GPG 簽章,apt 會自動驗 —— 伺服器上這點比方便重要
#   · 和本腳本既有的「sudo 只用於 apt 缺件補裝」模式一致
#   · 代價:apt 版**不會自動更新**(原生安裝器版會),要靠系統升級流程
# 官方文件與金鑰指紋:https://code.claude.com/docs/en/setup
CLAUDE_APT_KEY_URL="${CLAUDE_APT_KEY_URL:-https://downloads.claude.ai/keys/claude-code.asc}"
CLAUDE_APT_REPO="${CLAUDE_APT_REPO:-https://downloads.claude.ai/claude-code/apt/stable}"
CLAUDE_APT_FPR="31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"

install_claude_code() {
  command -v claude >/dev/null 2>&1 && return 0
  command -v sudo >/dev/null 2>&1 || { echo "  (沒有 sudo,略過 Claude Code)" >&2; return 1; }
  local key=/etc/apt/keyrings/claude-code.asc
  sudo install -d -m 0755 /etc/apt/keyrings || return 1
  sudo curl -fsSL "$CLAUDE_APT_KEY_URL" -o "$key" || return 1
  # **先驗指紋再信任。** 下載到的東西沒驗就加進 apt 信任鏈,等於把套件來源
  # 的決定權交給當下的網路。
  if ! gpg --show-keys "$key" 2>/dev/null | tr -d ' ' | grep -q "$CLAUDE_APT_FPR"; then
    echo "  ✗ 金鑰指紋不符預期,中止安裝 Claude Code" >&2
    sudo rm -f "$key"; return 1
  fi
  echo "deb [signed-by=$key] $CLAUDE_APT_REPO stable main" \
    | sudo tee /etc/apt/sources.list.d/claude-code.list >/dev/null || return 1
  sudo apt-get update -qq || return 1
  sudo apt-get install -y -qq claude-code || return 1
}

if [ "$INSTALL_CLAUDE" = "1" ] && ! command -v claude >/dev/null 2>&1; then
  log_step "安裝 Claude Code(官方簽章 apt repo)"
  if install_claude_code; then
    log_step "  claude $(claude --version 2>/dev/null | head -1)"
  else
    printf '⚠️  Claude Code 沒裝成 —— 這台龍蝦的 CC 頁籤會是關的,其餘功能不受影響。\n' >&2
    printf '    安裝方式見 https://code.claude.com/docs/en/setup\n' >&2
  fi
fi

if [ "$INSTALL_CODEX" = "1" ] && ! command -v codex >/dev/null 2>&1; then
  log_step "安裝 Codex $CODEX_VERSION"
  # npm 裝進使用者自己的 prefix(不是 sudo npm -g)。bridge 的
  # _resolve_codex_bin() 會依序找 CODEX_BIN → ~/.codex/packages/… →
  # ~/.local/bin/codex → PATH,所以放進 ~/.local 就找得到。
  if PATH="$NODE_ROOT/bin:$PATH" npm install -g --silent \
       --prefix "$HOME/.local" "@openai/codex@$CODEX_VERSION"; then
    log_step "  codex $(PATH="$NODE_ROOT/bin:$PATH" "$HOME/.local/bin/codex" --version 2>/dev/null | head -1)"
  else
    printf '⚠️  Codex 沒裝成 —— 這台龍蝦的 Codex 頁籤會是關的,其餘功能不受影響。\n' >&2
  fi
fi

# ── node 要進登入 PATH(2026-10-31 實機抓到,修正版)─────────────────────
#
# **Linux 上的 codex 和 macOS 不一樣。** macOS 裝到的是原生二進位
# (~/.codex/packages/standalone/…);Linux 用 npm 裝到的是 JS wrapper
# (`#!/usr/bin/env node` → codex.js)—— **執行時需要 node 在 PATH 上**。
#
# 服務面沒事(bridge 與 codex-daemon 的 unit 都帶了 $NODE_ROOT/bin),
# 壞的是**使用者面**:他要在 Pocket 的內建終端機裡跑 `codex` 登入自己的帳號,
# 而那是登入 shell。第一台實機就是這樣炸的:
#     env: 'node': No such file or directory
# 而 bridge 的 `cx` capability 只檢查檔案在不在 → 照樣回 true
#   → **capability 在說謊**:頁籤亮著,一用就壞。
#
# ⚠️ **這段刻意放在安裝區塊外面。** 第一版我寫在
# `! command -v codex` 的判斷式**裡面** —— 於是在「codex 已經裝好、只是
# PATH 沒設」的機器上重跑安裝器,整段被跳過,修正永遠套不上去。
# 實機重跑時就是這樣:日誌裡連「安裝 Codex」都沒出現,問題原封不動。
# **修的是既有狀態,就不能掛在「第一次安裝」的條件下。**
if [ -x "$NODE_ROOT/bin/node" ] && ! grep -q 'pocket-lobster-node-path' "$HOME/.profile" 2>/dev/null; then
  printf '\n# pocket-lobster-node-path:codex 是 JS wrapper,執行時要 node 在 PATH 上\nexport PATH="%s/bin:$HOME/.local/bin:$PATH"\n' \
    "$NODE_ROOT" >> "$HOME/.profile"
  log_step "已把 node 加進 ~/.profile 的 PATH"
fi

# Claude Code 官方系統需求寫明 **4 GB+ RAM**。Starter 級的雲端龍蝦正好卡在
# 那條線上,而 Hermes 全套靜置實測就吃 1.86 GB —— 兩個一起開會很擠。
# 只警告不中止:夠不夠要看他實際怎麼用,我們不替他決定。
_mem_kb=$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 0)
if [ "${_mem_kb:-0}" -gt 0 ] && [ "$_mem_kb" -lt 4000000 ]; then
  printf '⚠️  這台機器只有約 %s GB RAM;Claude Code 官方需求是 4 GB+。\n' \
    "$(awk -v k="$_mem_kb" 'BEGIN{printf "%.1f", k/1048576}')" >&2
  printf '    會跑,但大一點的 session 容易被 OOM 殺掉。\n' >&2
fi

# ── cloudflared(公網保底 quick tunnel) ──────────────────────────────────
CLOUDFLARED_BIN="$HOME/.local/bin/cloudflared"
if [ "$INSTALL_TUNNEL" = "1" ] && [ ! -x "$CLOUDFLARED_BIN" ]; then
  if command -v cloudflared >/dev/null; then
    CLOUDFLARED_BIN="$(command -v cloudflared)"
  else
    log_step "下載 cloudflared $CLOUDFLARED_VERSION($CF_ARCH)"
    mkdir -p "$HOME/.local/bin"
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/download/$CLOUDFLARED_VERSION/cloudflared-linux-$CF_ARCH" -o "$CLOUDFLARED_BIN"
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
# 預設只綁 loopback:對外唯一入口是 cloudflared tunnel(或自建 Tailscale)。
# 要開 LAN 直連請以 POCKET_BRIDGE_BIND=0.0.0.0 重跑安裝器(明確選擇,不是預設)。
ExecStart=$BRIDGE_VENV/bin/python -m uvicorn bridge:app --host $BRIDGE_BIND --port $BRIDGE_PORT --timeout-graceful-shutdown 3
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

# ── Codex managed daemon(2026-10-31)──────────────────────────────────────
#
# **裝了 codex 執行檔,不等於拿到持久化。**
#
# bridge 連 Codex 有兩條路(見 bridge.py 的 CODEX_APP_SERVER_SOCKET 註解):
#   · **managed daemon**(主要):unix socket 上跑 WebSocket frames,
#     「the single writer shared by Desktop and Pocket」。那顆 daemon 在 Mac 上
#     是**桌面版 Codex 帶起來的**,獨立於 bridge —— 所以 bridge 重啟時
#     **進行中的回合活得下來**。
#   · `codex app-server --stdio`(退路):bridge 自己 spawn 的子行程,
#     **隨 bridge 一起死**。
#
# 龍蝦沒有桌面版 → 不補這個 unit 就只剩退路 → 每次 bridge 重啟都打斷正在跑的
# Codex 回合。而龍蝦正是最常重啟的環境(更新、OOM、換設定)。
# 機主 2026-10-31 的目標是「cc / cx 要能持久」,這一格就是缺口之一。
#
# ⚠️ **這段指令是從 macOS 上執行中的 daemon 行程反推的,不是來自官方文件。**
#    (`codex app-server --remote-control --listen unix:// --managed-daemon`)
#    刻意不帶 `--analytics-default-enabled` —— 那是桌面版自己的選擇,
#    我們不替使用者在他的機器上打開遙測。
#    **沒有在 Linux 上驗過**:起不來的話 bridge 會自動退回 stdio(三態判定
#    是「連得上嗎」而不是「socket 檔在不在」),所以最壞情況是回到現狀,
#    不會比不裝這個 unit 更糟。
if [ "$INSTALL_CODEX" = "1" ]; then
cat > "$UNIT_DIR/pocket-codex-daemon.service" <<UNIT
[Unit]
Description=Codex managed daemon (Pocket lobster) —— 讓 Codex 回合跨得過 bridge 重啟
After=network-online.target

[Service]
Environment=PATH=%h/.local/bin:$NODE_ROOT/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=%h/.local/bin/codex app-server --remote-control --listen unix:// --managed-daemon
Restart=always
RestartSec=5
TimeoutStartSec=120

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

# ── systemctl --user 需要 XDG_RUNTIME_DIR(2026-10-31 實機抓到)─────────
#
# 首次 cloud-init 時這個變數是有的,所以第一次裝沒事。但**重跑**時常常是
# `ssh root@box 'sudo -u pocket -i …'` —— 那條路徑沒有 user session bus,
# 於是每一個 systemctl --user 都失敗:
#     Failed to connect to user scope bus via local transport:
#     $DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined
# 而安裝器照樣往下跑、印出「結果」—— 看起來成功,**實際上服務根本沒重啟,
# 跑的還是舊程式**。這是「離開碼不等於成功」的又一個版本。
#
# linger 開著時 /run/user/<uid> 一定在,補上變數就能用。
if [ -z "${XDG_RUNTIME_DIR:-}" ] && [ -d "/run/user/$(id -u)" ]; then
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  log_step "補上 XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR(重跑時 ssh 環境沒有它)"
fi

systemctl --user daemon-reload \
  || die "systemctl --user 不可用(XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-未設})。
  若是從 ssh 重跑,改用:  sudo machinectl shell pocket@  然後在裡面跑本腳本"
loginctl enable-linger "$USER" 2>/dev/null || log_step "enable-linger 失敗(無 sudo?)— 登出後服務會停,可稍後手動: sudo loginctl enable-linger $USER"

log_step "啟動服務"
# ⚠️ enable **+ restart**,不是 `enable --now`(2026-10-31 實機抓到)。
# `enable --now` 對**已經在跑**的服務是 no-op —— 於是重跑安裝器時,服務雖然
# rsync 了新 bridge.py,卻**永遠不會被重新載入**,跑的還是舊程式。
# 每次重跑都要手動 `systemctl --user restart` 才生效,是這個坑。
# enable(開機自啟)只做一次、restart 保證吃到新碼。
#
# bridge 要先 daemon-reload 後再 restart(unit 檔可能剛改過)。
_svc_up() {   # $1 = unit 名
  systemctl --user enable "$1" >/dev/null 2>&1
  systemctl --user restart "$1"
}
_svc_up "$BRIDGE_LABEL.service"
[ "$INSTALL_OPENCLAW" = "1" ] && _svc_up pocket-openclaw.service
# codex daemon:只在 codex 真的裝起來了才啟用。寫了 unit 卻啟不動,只會在
# systemctl 裡留一條紅的,讓人以為壞了 —— 而實際上只是沒裝 codex。
if [ "$INSTALL_CODEX" = "1" ] && [ -x "$HOME/.local/bin/codex" ]; then
  _svc_up pocket-codex-daemon.service \
    || log_step "codex daemon 啟不動 —— bridge 會自動退回 stdio(回合不跨重啟,但功能正常)"
fi
[ "$INSTALL_TUNNEL" = "1" ] && _svc_up pocket-tunnel.service

# ── 等 bridge 起來 → 開配對頁 ────────────────────────────────────────────
log_step "等 bridge /health"
for _ in $(seq 1 30); do
  if curl -fsS -m 2 "http://127.0.0.1:$BRIDGE_PORT/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS -m 2 "http://127.0.0.1:$BRIDGE_PORT/health" >/dev/null || die "bridge 未起來;journalctl --user -u $BRIDGE_LABEL -n 50 查原因"

# 配對頁連結一律內嵌 boot code(頁面/qr.json 缺碼會 403;連結本身即憑證,勿外流)
PAIR_URL="http://127.0.0.1:$BRIDGE_PORT/pair/qr?boot=$PAIR_BOOT_CODE"
echo
echo "✅ 龍蝦主機就緒。用 Pocket App 掃碼配對:"
echo "   $PAIR_URL"
# tunnel 版配對頁(等 quick tunnel URL 落檔;雲端主機從手機瀏覽器直接開這條)
if [ "$INSTALL_TUNNEL" = "1" ]; then
  TUNNEL_URL=""
  for _ in $(seq 1 20); do
    TUNNEL_URL="$(head -n1 "$TUNNEL_URL_FILE" 2>/dev/null | tr -d '[:space:]')"
    [ -n "$TUNNEL_URL" ] && break
    sleep 1
  done
  if [ -n "$TUNNEL_URL" ]; then
    echo
    echo "🌐 公網配對頁(含 boot code,連結即憑證,只傳給自己的裝置):"
    echo "   $TUNNEL_URL/pair/qr?boot=$PAIR_BOOT_CODE"
  fi
fi
# 雲端/headless 免掃碼:直接給配對連結(30 分鐘有效),傳到手機點開即配。
PAIR_LINK="$(curl -fsS -m 8 "http://127.0.0.1:$BRIDGE_PORT/pair/qr.json?ttl=1800&boot=$PAIR_BOOT_CODE" 2>/dev/null \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("payload","") if d.get("ok") else "")' 2>/dev/null || true)"
PAIR_LINK_FILE="${POCKET_PAIR_LINK_FILE:-$HOME/pocket-pair-link.txt}"
if [ -n "$PAIR_LINK" ]; then
  echo
  echo "📎 免掃碼(雲端主機):把下面這條配對連結傳到手機(iMessage/mail 皆可),點開即配對(30 分鐘內有效):"
  echo "   $PAIR_LINK"
  # **也要落檔。** 這支常常在沒有人看著 stdout 的情況下跑:
  # cloud-init／Vultr startup script／ssh 一行帶過、或單純捲掉了。
  # 連結只印在畫面上 = 錯過就只能重跑安裝器。
  # 連結本身即憑證(帶 boot code),所以 0600。
  (umask 077; printf '%s\n' "$PAIR_LINK" > "$PAIR_LINK_FILE") 2>/dev/null \
    && echo "   (也寫到 $PAIR_LINK_FILE —— 錯過上面那行就 cat 它)"
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
