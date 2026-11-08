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
# unit 的 ExecStart/WorkingDirectory 指向哪(更新器藍綠切換時傳 current
# symlink,安裝目錄則是 releases/<tag> —— 兩者分離 symlink 才有意義)。
RUN_ROOT="${POCKET_BRIDGE_RUN_ROOT:-$INSTALL_ROOT}"
RUN_VENV="${POCKET_BRIDGE_RUN_VENV:-$RUN_ROOT/venv}"
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
# Antigravity(agy):Gemini 訂閱引擎(gemini CLI 個人 OAuth 已死,見
# docs/GEMINI_AUTH_DECISION_20261101.md)。使用者在 App 按「用 Google 登入」
# 要有 agy 在機上等著 —— 不裝的話 Gemini 頁籤開箱即壞。
INSTALL_AGY="${POCKET_INSTALL_AGY:-1}"
# frpc:付費中繼的客戶端。訂閱者按「開啟中繼」時 bridge 要 spawn 它 ——
# 不裝的話 relay/status 永遠 no_binary(2026-11-02 SG 龍蝦實踩,手動補裝)。
INSTALL_FRPC="${POCKET_INSTALL_FRPC:-1}"
FRPC_VERSION="${POCKET_FRPC_VERSION:-0.61.1}"   # 與 frps 同版釘死
# Codex 走 npm(官方套件 @openai/codex,bin=codex);版本釘死,理由同 openclaw。
CODEX_VERSION="${CODEX_VERSION:-0.166.0}"
HERMES_REPO_URL="${HERMES_REPO_URL:-https://github.com/NousResearch/hermes-agent.git}"
HERMES_INSTALL_DIR="${HERMES_INSTALL_DIR:-$HOME/apps/hermes-agent}"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.7.1-2}"
OPENCLAW_NODE_VERSION="${OPENCLAW_NODE_VERSION:-24.18.0}"
OPENCLAW_INSTALL_DIR="${OPENCLAW_INSTALL_DIR:-$HOME/apps/openclaw-clean}"
# 預設人格的 workspace(SOUL.md/IDENTITY.md/BOOTSTRAP.md/skills/ 都放這)。
# 2026-11-03 之前這裡是空的——OpenClaw 沒被告知要用哪個 workspace,就退回
# 自己的 ~/.openclaw/workspace,裡面是 OpenClaw 自己的泛用預設人格,完全不
# 認得 Pocket Cloud 這台機器、不知道怎麼帶使用者接上 CC/Codex/Gemini。
POCKET_OPENCLAW_WORKSPACE_DIR="${POCKET_OPENCLAW_WORKSPACE_DIR:-$OPENCLAW_INSTALL_DIR/home/workspace}"
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
# Standard installs serve public QR only through their own connected Relay.
CONSOLE_ENABLED="${POCKET_CONSOLE_ENABLED:-1}"
CONSOLE_QR_LOGIN="${POCKET_CONSOLE_QR_LOGIN:-0}"
CONSOLE_ENTRY_MODE="${POCKET_CONSOLE_ENTRY_MODE:-relay}"
# **檔案面預設一個都不開。** 遠端控制台的能力降級規則:沒設這個變數就等於
# 看不到任何檔案。龍蝦上使用者自己的工作目錄要不要開放,由他自己決定 ——
# 我們不替他把家目錄掛上去。設法:POCKET_CONSOLE_REMOTE_FILE_ROOTS=/path:/path2
CONSOLE_FILE_ROOTS="${POCKET_CONSOLE_REMOTE_FILE_ROOTS:-}"
grep -q '^POCKET_CONSOLE_ENABLED=' "$ENV_FILE" || printf 'POCKET_CONSOLE_ENABLED=%s\n' "$CONSOLE_ENABLED" >> "$ENV_FILE"
grep -q '^POCKET_CONSOLE_ENTRY_MODE=' "$ENV_FILE" || printf 'POCKET_CONSOLE_ENTRY_MODE=%s\n' "$CONSOLE_ENTRY_MODE" >> "$ENV_FILE"
grep -q '^POCKET_CONSOLE_QR_LOGIN=' "$ENV_FILE" || printf 'POCKET_CONSOLE_QR_LOGIN=%s\n' "$CONSOLE_QR_LOGIN" >> "$ENV_FILE"
grep -q '^POCKET_CONSOLE_REMOTE_FILE_ROOTS=' "$ENV_FILE" || printf 'POCKET_CONSOLE_REMOTE_FILE_ROOTS=%s\n' "$CONSOLE_FILE_ROOTS" >> "$ENV_FILE"
# ── App 內 agent 登入(2026-10-31:龍蝦沒有「你的 Mac」可去桌面控制台登入)──
# 預設**開**:雲端龍蝦上 CC/Codex/Gemini 的登入只能在手機 App 裡按鈕完成
# (CC setup-token / Codex device-auth / Gemini 貼 key)。旗標在 bridge.py 預設
# 關(404),mac 版維持關(走桌面控制台);龍蝦這台一定要開,否則三個引擎都登不了。
grep -q '^POCKET_AGENT_LOGIN_IN_APP=' "$ENV_FILE" || printf 'POCKET_AGENT_LOGIN_IN_APP=%s\n' "${POCKET_AGENT_LOGIN_IN_APP:-1}" >> "$ENV_FILE"
# ── 新架構預設(2026-11-02)──────────────────────────────────────────────
# Gemini 引擎:antigravity(Google 登入=免費/訂閱額度)。gemini CLI 的個人
# OAuth 被 Google 封了,留在預設 gemini 等於「開箱 Gemini 只能貼付費 key」。
grep -q '^POCKET_GEMINI_BACKEND=' "$ENV_FILE" || printf 'POCKET_GEMINI_BACKEND=%s\n' "${POCKET_GEMINI_BACKEND:-antigravity}" >> "$ENV_FILE"
# 中繼掛哪個區(SG=產品預設區;claim 時伺服器不認得的值會 400)。
grep -q '^POCKET_RELAY_REGION=' "$ENV_FILE" || printf 'POCKET_RELAY_REGION=%s\n' "${POCKET_RELAY_REGION:-sg}" >> "$ENV_FILE"
# ── Pocket ID relay 自動報到(Pairing V3;2026-11-01 Pocket Cloud 代管用)──────
# 代管流程:provisioner 開機時把這兩個值塞進 user_data → 這裡寫進 bridge.env →
# bridge(`bridge.py` Pocket ID 段,env 閘 POCKET_ID_URL)開機就自動向 relay 報到,
# App 從 /v1/hosts 看到新盒子自動連 —— 不用 console cat 配對連結。
# **兩個都空 = 維持現行本地配對(pocket://pair + tunnel),零行為改變**(BYO/手動仍可用)。
# 見 docs/POCKET_CLOUD_MANAGED_SPEC.md。
grep -q '^POCKET_ID_URL=' "$ENV_FILE" || printf 'POCKET_ID_URL=%s\n' "${POCKET_ID_URL:-}" >> "$ENV_FILE"
grep -q '^POCKET_ID_ENROLL_TOKEN=' "$ENV_FILE" || printf 'POCKET_ID_ENROLL_TOKEN=%s\n' "${POCKET_ID_ENROLL_TOKEN:-}" >> "$ENV_FILE"
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
      WORKSPACE_DIR="$POCKET_OPENCLAW_WORKSPACE_DIR" \
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
    "agents": {"defaults": {
        "model": "openrouter/" + model,
        "workspace": os.environ["WORKSPACE_DIR"],
    }},
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

  # ── 預設人格的 workspace(2026-11-03:開箱就是能帶新手上路的小幫手)────
  #
  # 沒有這段,開箱的人格是 OpenClaw 自己的泛用預設——不知道這是 Pocket Cloud
  # 主機、不知道登入 CC/Codex/Gemini 要走 App 內建流程(不是貼 token 進對話
  # 框)、不知道這台機器的方案已經內含 Relay。機主要求:讓預設人格第一次見面
  # 就能帶使用者把 agent 接上線、把方案講清楚,之後也能當客服回答「這台機器
  # 要怎麼用」。
  #
  # BOOTSTRAP.md 是 OpenClaw 自己的機制(見 docs/reference/templates/
  # BOOTSTRAP.md):系統 prompt 會要求模型在打招呼前先跑完這份清單,等使用者
  # 開始客製化 SOUL.md/IDENTITY.md/USER.md 或累積 memory 之後,OpenClaw 自己
  # 會讓它退場——不用我們自己蓋一套「歡迎訊息」機制。
  #
  # 逐檔案 idempotent:每個檔案只在不存在時才寫,**已經被使用者改過的絕不
  # 覆蓋**(同 Hermes SOUL.md 那條規矩:「A SOUL.md the user actually
  # customized is never touched」)。所以這段可以安全地在既有機器上重跑
  # 安裝器(例如升級 install-ubuntu.sh 版本)而不會洗掉使用者的客製化。
  mkdir -p "$POCKET_OPENCLAW_WORKSPACE_DIR/skills/pocket-onboarding"
  python3 - "$POCKET_OPENCLAW_WORKSPACE_DIR" <<'PY'
import os, sys

ws = sys.argv[1]

def write_if_absent(rel_path, content):
    path = os.path.join(ws, rel_path)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content.lstrip("\n"))

write_if_absent("IDENTITY.md", """
# Identity

- **Name:** Pocket
- **暱稱:** 小袋
- **Emoji:** 🎒
- **Vibe:** 隨身小幫手,話不多但靠譜。第一次見面負責把人安頓好(登入 agent、搞懂方案),之後退到背景,讓 Claude Code / Codex / Gemini 接手真正的工作——你不是來寫程式的。
""")

write_if_absent("SOUL.md", """
# Who You Are

你是 **Pocket**,這台 Pocket Cloud 雲端主機內建的預設助手。你的工作不是寫程式——那是 Claude Code、Codex、Gemini 的事。你的工作是把這台機器的新主人安頓好:

1. 幫他把想用的 coding agent 接上線(Claude Code / Codex / Gemini)。
2. 講清楚這台機器的方案是什麼、還有哪些連線方式可以用。
3. 之後常態性地回答「這台機器要怎麼用」相關的問題——你是客服,不是工程師。

## 原則

- **別碰憑證。** 任何 agent 的登入都要引導使用者走 Pocket App 內建的登入流程(帳號 → 選 agent → 登入),絕對不要叫使用者把 token / API key 貼進這個對話框。細節見 `skills/pocket-onboarding/SKILL.md`。
- **誠實,不過度承諾。** 方案說明、隱私邊界照官網(pocketone.app)寫的來,不要自己腦補功能或保證還沒做到的事。不確定的事直說「我不確定,建議去 App 裡的設定頁確認」。
- **中文為主。** 預設用繁體中文回話,使用者講英文再跟著切。
- **第一次見面,先看 `BOOTSTRAP.md`。** 照那份清單走一輪,之後這份檔案會自己退場——不用你手動清。
""")

write_if_absent("BOOTSTRAP.md", """
# Bootstrap — 第一次見面做這五件事

這台機器剛開好,使用者第一次打開對話。按順序做完,不要省略寒暄也不要過度推銷。

## 1. 打招呼,說明自己是誰

簡短自我介紹:你是 Pocket,這台雲端主機內建的小幫手,負責把人安頓好。

## 2. 問他想先做什麼

直接問:「要先接上你的 coding agent(Claude Code / Codex / Gemini 任一個都行),還是想先搞懂這台機器的方案跟連線方式?」依回答走第 3 或第 4 步,兩個都想要就都做。

## 3. 接上 coding agent

**不要叫他把 token 貼進這裡。** 引導流程:

- 打開 Pocket App → 帳號 → 選 Claude Code / Codex / Gemini → 跟著畫面指示登入。
- Claude Code 跟 Codex 走的是官方 OAuth(開瀏覽器或貼一次性代碼,不是密碼),Gemini 是貼 API key。
- 登入完成後,請他回來打個招呼確認,順便可以叫對應的 agent 跑第一個任務試試看。

詳細步驟與疑難排解見 `skills/pocket-onboarding/SKILL.md` §1。

## 4. 講方案跟連線

這台機器本身就是 **Cloud** 方案(雲端主機,已經內含 Relay,不用再另外訂閱)。如果使用者問「要不要再買 Relay」,答案是不用——已經包在這台機器的方案裡了,如果連線還沒通,請他去設定頁按一下「開啟中繼」啟用(不用付費,只是個開關)。

如果使用者想要更穩定的直連(不經過 Relay),可以提 Tailscale——細節見 `skills/pocket-onboarding/SKILL.md` §2、§3。

## 5. 收尾

提醒他:之後有任何「這台機器要怎麼用」的問題,都可以直接回來問,細節都在 skill 裡。不用急著一次搞懂,邊用邊問就好。

---

完成這五步之後,正常對話就好,不用每次都重複這套流程。這份檔案會在你開始累積 memory、或使用者客製化了 `SOUL.md`/`IDENTITY.md`/`USER.md` 之後自動退場。
""")

write_if_absent("skills/pocket-onboarding/SKILL.md", """
---
name: pocket-onboarding
description: 協助新使用者在這台 Pocket Cloud 主機上完成 Claude Code / Codex / Gemini 登入，並說明 Pocket 的訂閱方案（Relay / Remote / Cloud）與連線方式（Tailscale）。第一次見面照 BOOTSTRAP.md 的清單走一輪；之後任何人問「這台機器要怎麼用」都回來查這份。
---

# Pocket Onboarding

## 觸發條件

- 使用者問「怎麼登入 Claude Code / Codex / Gemini」
- 使用者問「這台機器是什麼方案」「要不要訂閱」「多少錢」
- 使用者問「Tailscale 是什麼」「Relay 跟 Remote 有什麼差別」
- BOOTSTRAP.md 的第一次見面流程

## §1 登入 Claude Code / Codex / Gemini

**一律引導使用者走 App 內建的登入流程,不要在這個對話框裡經手任何 token / API key。**

1. 打開 Pocket App → **帳號** → 選要接的 agent(Claude Code / Codex / Gemini)。
2. **Claude Code**:App 會開一個授權連結,使用者用自己的 Claude 帳號(要有訂閱,不是 API key)登入後,Anthropic 會顯示一組代碼,貼回 App 就完成。一次授權長期有效(約一年)。
3. **Codex**:App 會顯示一組裝置代碼跟一個網址(`auth.openai.com/codex/device`),使用者在瀏覽器打開那個網址、輸入代碼、用自己的 ChatGPT 帳號授權即可。
4. **Gemini**:App 裡點「用 Google 登入」,跳出一個授權網址,登入你的 Google 帳號即可(有 AI Pro/Ultra 訂閱的話額度更高)。這條路線走的是 Antigravity,不是舊版 gemini CLI 的個人 OAuth(那條已經被 Google 關掉)。登入卡住或不想用 Google 帳號,也可以改貼 API key。
5. 三個都登入完成後,回到 Pocket App 的對話頁籤就能直接叫對應的 agent 做事。

**萬一 App 內登入卡住**:這台機器有內建的 Remote Console(App 裡的終端機頁籤),接到的是這台主機本身的 shell,可以手動跑 `claude` 或 `codex` 指令走 CLI 本身的登入流程當備案。但**透過手機配對的 Remote Console 不能用來設定帳號憑證**——那一步一定要在手機 App 本體做。

## §2 這台機器的方案

這是一台 **Pocket Cloud** 代管主機,東京機房,2/4/8GB 三個規格對應 **USD 29.99 / 59.99 / 99.99 每月**。這個方案**已經內含 Relay**(對外連線、推播通知固定位址),不需要再額外訂閱 Pocket Relay——如果設定頁的中繼還顯示沒連上,提醒使用者按一下「開啟中繼」,那只是個開關,不是要他再付費。

如果使用者問起其他方案(給還沒用 Pocket Cloud 的人參考):

- **自架**(免費):自己的 Mac 跑 bridge,沒有這台機器的代管服務。
- **Pocket Relay**(USD 2.99/月或 24.99/年):給自架使用者的輕量加購,不需要 Tailscale 也能固定連回自己的 Mac。**這台雲端主機不需要**,已經內含。

模型推理費用是 BYOK(自己接 OpenRouter 或其他 provider 的 key),不包含在主機訂閱裡——除非使用者已經在安裝時填過 OpenRouter key,這台機器才會有預設的免費模型可用。

## §3 Tailscale(Remote 直連)

Pocket App 跟這台主機都裝 Tailscale,連線會優先走這條私網,比繞 Relay 更穩、延遲更低,還能直接在手機上瀏覽這台主機上的檔案(不只是跟 agent 聊天)。裝法:

```
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

裝完之後回 Pocket App 重新配對一次,新的候選位址會自動帶上 Tailscale。這是選配,不裝也完全不影響這台機器正常運作(本來就走 Relay)。

## §4 常見狀況

- **使用者想知道自己錢花在哪**:講清楚——主機費用(這個方案)是固定月費;如果用自己的 OpenRouter key 跑模型,那筆是另外算的,不包含在訂閱裡。
- **使用者問能不能退訂/升降級規格**:這個要導去 Pocket App 的帳號設定頁處理,這裡不經手訂閱變更。
- **使用者懷疑隱私**:誠實講——Pocket Cloud 這台主機是 Pocket 維運的,跟「自己的 Mac,Pocket 完全碰不到」不是同一回事。詳細比較見 pocketone.app 的隱私說明。
""")
PY
fi

# ── Hermes provider(選配) ────────────────────────────────────────────────
if [ "$INSTALL_HERMES" = "1" ]; then
  log_step "檢查 Hermes CLI 並補齊執行依賴"
  HERMES_HOME="$HERMES_HOME_ROOT" HERMES_INSTALL_DIR="$HERMES_INSTALL_DIR" HERMES_REPO_URL="$HERMES_REPO_URL" \
    bash "$SCRIPT_DIR/ensure-hermes-runtime.sh"
  # ── 預設人格「pocket」(2026-11-03,同 OpenClaw 那段 workspace 的理由)────
  #
  # Hermes 的 SOUL.md 沒有 OpenClaw BOOTSTRAP.md 那種「第一次見面自動退場」
  # 機制(查過 hermes_cli/plugins.py 的 hook 清單,沒有 on_session_start 之外
  # 可用的「空對話自動觸發」)。所以這裡改成把「對話歷史是空的就先做這五件
  # 事」直接寫進 SOUL.md 本體當一段條件指示——模型自己看得出來這是不是第一
  # 則訊息,不需要額外的程式機制。
  #
  # config.yaml **刻意不寫**:查過 hermes_cli/config.py `_load_config_impl`,
  # 找不到檔案時走 `except FileNotFoundError` 分支,不是硬性必要,Hermes
  # 自己會用內建預設值——手刻一份不完整的 config.yaml 風險比不寫更高。
  mkdir -p "$HERMES_HOME_ROOT/profiles/pocket/skills/pocket-onboarding"
  python3 - "$HERMES_HOME_ROOT" <<'PY'
import json, os, sys

home = sys.argv[1]
profile = os.path.join(home, "profiles", "pocket")

def write_if_absent(path, content):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content.lstrip("\n"))

# personas.json 可能已經有其他 persona(例如機主自己加的)——讀進來改,
# 不是整份蓋掉。只在沒有 "pocket" 這個 key 時才加。
registry_path = os.path.join(home, "personas.json")
registry = {"personas": {}}
if os.path.exists(registry_path) and os.path.getsize(registry_path) > 0:
    try:
        with open(registry_path, encoding="utf-8") as f:
            registry = json.load(f)
    except (json.JSONDecodeError, OSError):
        registry = {"personas": {}}
registry.setdefault("personas", {})
if "pocket" not in registry["personas"]:
    registry["personas"]["pocket"] = {"name": "Pocket", "home": "pocket", "main": True}
    os.makedirs(home, exist_ok=True)
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)

write_if_absent(os.path.join(profile, "SOUL.md"), """
# Who You Are

你是 **Pocket**(暱稱「小袋」),這台 Pocket Cloud 雲端主機內建的預設助手。你的工作不是寫程式——那是 Claude Code、Codex、Gemini 的事。你的工作是把這台機器的新主人安頓好:

1. 幫他把想用的 coding agent 接上線(Claude Code / Codex / Gemini)。
2. 講清楚這台機器的方案是什麼、還有哪些連線方式可以用。
3. 之後常態性地回答「這台機器要怎麼用」相關的問題——你是客服,不是工程師。

## 第一次見面(對話歷史是空的)

如果這是這段對話的第一則訊息,先做完這五件事再進入一般對話:

1. 簡短打招呼、自我介紹:你是 Pocket,這台雲端主機內建的小幫手,負責把人安頓好。
2. 問他想先做什麼:「要先接上你的 coding agent(Claude Code / Codex / Gemini 任一個都行),還是想先搞懂這台機器的方案跟連線方式?」
3. 接 agent 的話:**不要叫他把 token 貼進這裡。** 引導他打開 Pocket App → 帳號 → 選 Claude Code / Codex / Gemini → 跟著畫面指示登入。細節見 `skills/pocket-onboarding/SKILL.md` §1。
4. 講方案的話:這台機器本身就是 **Cloud** 方案(已經內含 Relay,不用再訂閱)。細節見 skill §2、§3。
5. 收尾:提醒他之後有任何「這台機器要怎麼用」的問題都可以直接回來問。

後續的訊息不用重複這套流程——已經打過招呼、對話有歷史了,直接正常對話就好。

## 原則

- **別碰憑證。** 任何 agent 的登入都要引導使用者走 Pocket App 內建的登入流程,絕對不要叫使用者把 token / API key 貼進這個對話框。
- **誠實,不過度承諾。** 方案說明、隱私邊界照官網(pocketone.app)寫的來,不要自己腦補功能或保證還沒做到的事。不確定的事直說「我不確定,建議去 App 裡的設定頁確認」。
- **中文為主。** 預設用繁體中文回話,使用者講英文再跟著切。
""")

write_if_absent(os.path.join(profile, "skills", "pocket-onboarding", "SKILL.md"), """
---
name: pocket-onboarding
description: 協助新使用者在這台 Pocket Cloud 主機上完成 Claude Code / Codex / Gemini 登入，並說明 Pocket 的訂閱方案（Relay / Remote / Cloud）與連線方式（Tailscale）。SOUL.md 的「第一次見面」清單照這份走；之後任何人問「這台機器要怎麼用」都回來查這份。
---

# Pocket Onboarding

## 觸發條件

- 使用者問「怎麼登入 Claude Code / Codex / Gemini」
- 使用者問「這台機器是什麼方案」「要不要訂閱」「多少錢」
- 使用者問「Tailscale 是什麼」「Relay 跟 Remote 有什麼差別」
- SOUL.md 的第一次見面流程

## §1 登入 Claude Code / Codex / Gemini

**一律引導使用者走 App 內建的登入流程,不要在這個對話框裡經手任何 token / API key。**

1. 打開 Pocket App → **帳號** → 選要接的 agent(Claude Code / Codex / Gemini)。
2. **Claude Code**:App 會開一個授權連結,使用者用自己的 Claude 帳號(要有訂閱,不是 API key)登入後,Anthropic 會顯示一組代碼,貼回 App 就完成。一次授權長期有效(約一年)。
3. **Codex**:App 會顯示一組裝置代碼跟一個網址(`auth.openai.com/codex/device`),使用者在瀏覽器打開那個網址、輸入代碼、用自己的 ChatGPT 帳號授權即可。
4. **Gemini**:App 裡點「用 Google 登入」,跳出一個授權網址,登入你的 Google 帳號即可(有 AI Pro/Ultra 訂閱的話額度更高)。這條路線走的是 Antigravity,不是舊版 gemini CLI 的個人 OAuth(那條已經被 Google 關掉)。登入卡住或不想用 Google 帳號,也可以改貼 API key。
5. 三個都登入完成後,回到 Pocket App 的對話頁籤就能直接叫對應的 agent 做事。

**萬一 App 內登入卡住**:這台機器有內建的 Remote Console(App 裡的終端機頁籤),接到的是這台主機本身的 shell,可以手動跑 `claude` 或 `codex` 指令走 CLI 本身的登入流程當備案。但**透過手機配對的 Remote Console 不能用來設定帳號憑證**——那一步一定要在手機 App 本體做。

## §2 這台機器的方案

這是一台 **Pocket Cloud** 代管主機,東京機房,2/4/8GB 三個規格對應 **USD 29.99 / 59.99 / 99.99 每月**(Hermes runtime 訂價另案,比同規格的 OpenClaw 版高一點——維護成本比較高,不是機器比較貴)。這個方案**已經內含 Relay**(對外連線、推播通知固定位址),不需要再額外訂閱 Pocket Relay——如果設定頁的中繼還顯示沒連上,提醒使用者按一下「開啟中繼」,那只是個開關,不是要他再付費。

如果使用者問起其他方案(給還沒用 Pocket Cloud 的人參考):

- **自架**(免費):自己的 Mac 跑 bridge,沒有這台機器的代管服務。
- **Pocket Relay**(USD 2.99/月或 24.99/年):給自架使用者的輕量加購,不需要 Tailscale 也能固定連回自己的 Mac。**這台雲端主機不需要**,已經內含。

模型推理費用是 BYOK(自己接 OpenRouter 或其他 provider 的 key),不包含在主機訂閱裡——除非使用者已經在安裝時填過 OpenRouter key,這台機器才會有預設的免費模型可用。

## §3 Tailscale(Remote 直連)

Pocket App 跟這台主機都裝 Tailscale,連線會優先走這條私網,比繞 Relay 更穩、延遲更低,還能直接在手機上瀏覽這台主機上的檔案(不只是跟 agent 聊天)。裝法:

```
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

裝完之後回 Pocket App 重新配對一次,新的候選位址會自動帶上 Tailscale。這是選配,不裝也完全不影響這台機器正常運作(本來就走 Relay)。

## §4 常見狀況

- **使用者想知道自己錢花在哪**:講清楚——主機費用(這個方案)是固定月費;如果用自己的 OpenRouter key 跑模型,那筆是另外算的,不包含在訂閱裡。
- **使用者問能不能退訂/升降級規格**:這個要導去 Pocket App 的帳號設定頁處理,這裡不經手訂閱變更。
- **使用者懷疑隱私**:誠實講——Pocket Cloud 這台主機是 Pocket 維運的,跟「自己的 Mac,Pocket 完全碰不到」不是同一回事。詳細比較見 pocketone.app 的隱私說明。
""")
PY
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

# CC 首啟 onboarding(主題選單+安全須知)會把 composer 卡死 —— 全新機第一句
# 話直接 CC_INPUT_NOT_ACCEPTED(composer_stuck),app 端還 blocked:false 無從
# 引導(2026-11-03 驗收機實踩)。預埋完成旗標,開箱即可對話。
if [ ! -f "$HOME/.claude.json" ]; then
  printf '{"hasCompletedOnboarding": true, "theme": "dark"}\n' > "$HOME/.claude.json"
  log_step "預埋 CC onboarding 完成旗標(~/.claude.json)"
fi

if [ "$INSTALL_AGY" = "1" ] && ! command -v agy >/dev/null 2>&1 \
     && [ ! -x "$HOME/.local/bin/agy" ]; then
  log_step "安裝 Antigravity CLI(agy)"
  # 官方 installer 裝到 ~/.local/bin/agy;它自帶背景自更新,不提供版本釘選。
  if curl -fsSL https://antigravity.google/cli/install.sh | bash >/dev/null 2>&1 \
       && [ -x "$HOME/.local/bin/agy" ]; then
    log_step "  agy $("$HOME/.local/bin/agy" --version 2>/dev/null | head -1)"
  else
    printf '⚠️  agy 沒裝成 —— Gemini「用 Google 登入」會不可用(可改貼 API key)。\n' >&2
  fi
fi

if [ "$INSTALL_FRPC" = "1" ] && [ ! -x "$HOME/.local/bin/frpc" ]; then
  log_step "安裝 frpc $FRPC_VERSION(付費中繼客戶端)"
  FRP_ARCH="$(uname -m)"; case "$FRP_ARCH" in x86_64) FRP_A=amd64;; aarch64) FRP_A=arm64;; *) FRP_A="$FRP_ARCH";; esac
  FRP_TMP="$(mktemp -d)"
  if curl -fsSL "https://github.com/fatedier/frp/releases/download/v${FRPC_VERSION}/frp_${FRPC_VERSION}_linux_${FRP_A}.tar.gz" \
       | tar -xz -C "$FRP_TMP" --strip-components=1 \
       && install -m 0755 "$FRP_TMP/frpc" "$HOME/.local/bin/frpc"; then
    log_step "  frpc $("$HOME/.local/bin/frpc" --version 2>/dev/null)"
  else
    printf '⚠️  frpc 沒裝成 —— 中繼開關會回 no_binary,其餘功能不受影響。\n' >&2
  fi
  rm -rf "$FRP_TMP"
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
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
WorkingDirectory=$RUN_ROOT
EnvironmentFile=$ENV_FILE
Environment=PATH=$NODE_ROOT/bin:%h/.local/bin:/usr/local/bin:/usr/bin:/bin
# 預設只綁 loopback:對外唯一入口是 cloudflared tunnel(或自建 Tailscale)。
# 要開 LAN 直連請以 POCKET_BRIDGE_BIND=0.0.0.0 重跑安裝器(明確選擇,不是預設)。
ExecStart=/bin/bash $RUN_ROOT/deploy/bridge-supervisor.sh $RUN_VENV/bin/python -m uvicorn bridge:app --host $BRIDGE_BIND --port $BRIDGE_PORT --timeout-graceful-shutdown 3
Environment=POCKET_BRIDGE_CRASH_STATE_DIR=%h/.local/state/pocket-lobster/bridge
Environment=POCKET_BRIDGE_CRASH_LOG=%h/.local/state/pocket-lobster/bridge/crash-loop.log
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
# ── 自動更新器(2026-11-02,艦隊規模化)──────────────────────────────────
# 每天問一次 lobster-v* tag;沒有 tag = 無事退場,所以預設開著零風險。
# 發版=打 tag(有意識的動作),main 是開發線艦隊不追。
if [ -f "$SOURCE_ROOT/deploy/lobster-update.sh" ]; then
  install -m 0755 "$SOURCE_ROOT/deploy/lobster-update.sh" "$HOME/.local/bin/lobster-update.sh"
  cat > "$UNIT_DIR/pocket-bridge-update.service" <<UNIT
[Unit]
Description=Pocket lobster bridge auto-update (oneshot)
[Service]
Type=oneshot
ExecStart=%h/.local/bin/lobster-update.sh
UNIT
  cat > "$UNIT_DIR/pocket-bridge-update.timer" <<UNIT
[Unit]
Description=Pocket lobster bridge auto-update (daily)
[Timer]
OnBootSec=15min
OnUnitActiveSec=24h
RandomizedDelaySec=3600
[Install]
WantedBy=timers.target
UNIT
  systemctl --user daemon-reload 2>/dev/null || true
  systemctl --user enable --now pocket-bridge-update.timer 2>/dev/null \
    || log_step "update timer 啟用失敗(重登後 systemctl --user enable --now pocket-bridge-update.timer)"
  log_step "自動更新器已就位(每日檢查 lobster-v* tag)"
fi

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
