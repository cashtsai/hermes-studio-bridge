# 自架 Pocket bridge:Ubuntu / Debian / WSL2 一鍵安裝

給自己有一台 Linux 機器(或 Windows + WSL2)的開發者:把它裝成可被 Pocket App
連上的自架 bridge 主機。免費、不經手任何憑證、不依賴 Pocket 的雲端。

## 一行安裝

```sh
curl -fsSL https://raw.githubusercontent.com/cashtsai/hermes-studio-bridge/main/deploy/selfhost-install.sh | sh
```

(正式短網址 `https://pocketone.app/selfhost.sh` 接上後亦可用;兩者是同一支腳本。)

腳本做的事:檢查前置 → 缺的 apt 套件先列清單再安裝 → clone bridge 並釘到
最新 `lobster-v*` release tag → 建 venv、裝依賴 → 生成 `BRIDGE_TOKEN`
(`~/.pocket/bridge-token`,0600)→ 寫 per-user systemd unit(非 root)→
啟動並印出配對資訊。

## 前置需求

- Ubuntu 22.04+ 或 Debian 12+(其他發行版:`--force` 可硬上,未驗證)
- Python 3.10+(上述發行版內建即符合;缺的話腳本會用 apt 補)
- 一般使用者 + sudo(只在補 apt 套件時用;root 也能跑但不建議)
- 埠 8081 未被占用(換埠:`POCKET_BRIDGE_PORT=8082` 重跑)
- macOS 不走這支 —— 請用 Pocket Connect / Pocket macOS

## 配對(三步)

安裝完成時會印出:

1. **bridge 位址**:`http://<這台機器的局網IP>:8081`
2. **token**:`cat ~/.pocket/bridge-token`
3. 手機 Pocket → **設定 → 進階連線 → 手動位址**,貼上位址與 token

也可以掃碼/點連結配對:安裝器結尾會印 `http://127.0.0.1:8081/pair/qr?boot=<碼>`
(此頁只服務本機與 Tailscale 來源)與一條 30 分鐘有效的免掃碼配對連結,
傳到手機點開即配。boot 碼在 `~/.pocket/pair-boot-code`。

## Windows(WSL2)

1. PowerShell:`wsl --install -d Ubuntu`(已有 Ubuntu 的略過),進 WSL 跑上面那行 curl。
2. 建議在 WSL 開 systemd:`/etc/wsl.conf` 加 `[boot]` / `systemd=true`,
   `wsl --shutdown` 後重進。沒開也能用 —— 安裝器會退回 nohup + `~/.profile`
   自啟(開著任一 WSL 視窗即存活)。
3. 手機連的是 **Windows 的局網 IP**,要把埠打進 WSL(擇一):
   - **mirrored networking**(Win11 22H2+,建議):`%UserProfile%\.wslconfig`
     加 `[wsl2]` / `networkingMode=mirrored`,`wsl --shutdown` 重進。
   - **netsh portproxy**:系統管理員 PowerShell
     `netsh interface portproxy add v4tov4 listenport=8081 listenaddress=0.0.0.0 connectport=8081 connectaddress=<WSL_IP>`
     (WSL IP 重開機會變,變了要重跑這條)。
   - 兩種都要放行防火牆:
     `netsh advfirewall firewall add rule name="Pocket Bridge 8081" dir=in action=allow protocol=TCP localport=8081`

已知限制:Windows 睡眠時 WSL 一起睡,bridge 連不上屬正常;喚醒即恢復。

## coding agents(自己裝、自己登入)

自架版刻意不代裝 Claude Code / Codex / Gemini —— 憑證是你的,登入在你自己的
機器上完成,bridge 一行 token 都不經手。裝好並登入後 bridge 會自動偵測,
App 對應頁籤就會亮:

- Claude Code:<https://code.claude.com/docs/en/setup>,裝完跑 `claude` 登入
- Codex:`npm install -g @openai/codex`,裝完跑 `codex` 登入
- Gemini(Antigravity):`curl -fsSL https://antigravity.google/cli/install.sh | bash`,裝完跑 `agy` 登入

## 升級 = 重跑同一行

重跑安裝那行 curl 即升級到最新 `lobster-v*` tag。已裝過的機器會交棒
`lobster-update.sh`:SQLite 快照、systemd unit 快照、新版行為探活、失敗自動
回滾 —— 與雲端代管主機同一條更新路徑。token 與配對碼不會重生,App 不用重配。
裝好後也有每日自動更新 timer(`pocket-bridge-update.timer`);不想要就
`systemctl --user disable --now pocket-bridge-update.timer`。

## 公網存取

自架 = 局網直連。出了家門要連,建議 Tailscale(手機與主機都裝):

```sh
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
```

之後想要免 Tailscale 的固定公網位址,可在 App 內訂閱 Pocket Relay(自架加購,
不是必需品)。本安裝器不裝 relay/frpc、也不開 cloudflared tunnel。

## 旗標與覆蓋

- `--no-start`:只裝到 systemd unit 檔生成為止,不碰 systemctl(容器/CI 用)
- `--force`:非 Ubuntu/Debian 仍強行安裝
- 環境變數:`POCKET_BRIDGE_PORT`(預設 8081)、`POCKET_BRIDGE_BIND`(預設
  0.0.0.0)、`POCKET_REPO` / `POCKET_REF`(裝 fork 或特定版位)

## 解除安裝

```sh
systemctl --user disable --now pocket-bridge.service pocket-bridge-update.timer 2>/dev/null
rm -f ~/.config/systemd/user/pocket-bridge.service \
      ~/.config/systemd/user/pocket-bridge-update.{service,timer}
systemctl --user daemon-reload
rm -rf ~/.local/share/pocket-lobster ~/.config/pocket-lobster ~/pocket-bridge \
       ~/.local/bin/lobster-update.sh ~/.local/bin/pocket-bridge-run.sh
rm -f ~/.pocket/bridge-token ~/.pocket/pair-boot-code   # 其他 ~/.pocket 檔案與 bridge 無關則保留
# WSL nohup 退路有用到的話:手動刪 ~/.profile 裡 pocket-bridge-autostart 那段
```
