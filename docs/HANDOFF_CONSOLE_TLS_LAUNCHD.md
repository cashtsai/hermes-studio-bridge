# 交辦:`ai.studio.pocket-console-tls` 的 launchd 不自動啟動也不重生

> 事實基準:2026-10-20 實機查證(cashcamp)。
> 任務 A 的 TLS 前門**功能已完成並驗收通過**,只剩這一個韌性缺口。

---

## 症狀

服務本體正常,但 launchd 的 `RunAtLoad` 與 `KeepAlive` **兩個都不生效**:

| 動作 | 預期 | 實際 |
|---|---|---|
| `launchctl bootstrap` | RunAtLoad 立刻啟動 | `runs = 0`,不啟動 |
| `kill <pid>` 後等 20s | KeepAlive 重生 | 不重生,`state = not running` |
| `launchctl kickstart` | 啟動 | ✅ 正常啟動並持續服務 |

**影響**:服務掛掉或機器重開機後**不會自己回來**,要有人手動 kickstart。

---

## 已排除(不要重走)

1. **不在 disabled 清單** —— `launchctl print-disabled gui/$(id -u) | grep pocket-console` 無任何條目。
2. **`launchctl enable` 無效** —— 執行後 bootout + bootstrap,仍 `runs = 0`。
3. **`ThrottleInterval` 不是肇因** —— 移除該 key 後 bootout + bootstrap,仍 `runs = 0`。
   (當初懷疑它是因為:能正常運作的 `ai.studio.hermes-bridge` 與
   `ai.studio.pocket-console-cert` 都沒有這個 key,是唯一的結構差異。已證實無關,
   該 key 已寫回,因為規格有建議。)
4. **plist 本身正確** —— `plutil -lint` OK;`launchctl print` 顯示
   `properties = keepalive | runatload | inferred program`,代表 launchd
   **有讀到**這兩個屬性,只是不照做。
5. **程式本身沒問題** —— kickstart 起來後 TLS 門回 401、憑證鏈正確、
   可長時間服務。不是啟動即崩潰被 throttle 的情況。

---

## 下一步候選

1. **legacy `launchctl load -w`**(最可疑的一條,本次**未測成** —— 指令被安全
   檢查擋下)。現代 `bootstrap` 與 legacy `load` 在 RunAtLoad 的處理上有差異,
   值得直接試:
   ```bash
   launchctl bootout gui/$(id -u)/ai.studio.pocket-console-tls
   launchctl load -w ~/Library/LaunchAgents/ai.studio.pocket-console-tls.plist
   ```
2. **從 GUI 的 Terminal session 重新載入** —— 本次所有 launchctl 操作都在
   CLI/SSH 脈絡下執行。gui domain 的 job 從非 GUI session bootstrap 時,
   launchd 可能註冊了但不接管生命週期。
3. 若以上都不成,**退一步用 watchdog**:這個 repo 已有
   `deploy/bridge-watchdog.sh` + `ai.studio.hermes-bridge-watchdog` 的前例,
   照抄一支定時探測 `:8443` 不通就 kickstart 的即可。

---

## 相關檔案

| 用途 | live 位置 | repo 追蹤副本 |
|---|---|---|
| TLS 轉發器 | `~/.local/bin/pocket-console-tls.py` | `deploy/pocket-console-tls.py` |
| launchd | `~/Library/LaunchAgents/ai.studio.pocket-console-tls.plist` | `deploy/ai.studio.pocket-console-tls.plist` |
| log | `~/Library/Logs/pocket-console-tls.{log,err.log}` | — |

手動拉起:
```bash
launchctl kickstart gui/$(id -u)/ai.studio.pocket-console-tls
```

驗收(這台 MagicDNS 目前沒運作,要用 `--resolve` 強制指向 tailnet IP):
```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  --resolve cashcamp.tail905550.ts.net:8443:100.67.0.12 \
  https://cashcamp.tail905550.ts.net:8443/console    # 401 = 成功
```

---

## 附帶觀察(不是待辦,但會影響你驗收)

cashcamp 的 **MagicDNS 沒有運作**:`cashcamp.tail905550.ts.net` 解析到公網 IP
`103.84.155.153` 而非 tailnet 的 `100.67.0.12`,`scutil --dns` 查不到 ts.net
resolver,且 `tailscale` CLI 本身啟動失敗(`Tailscale.CLIError error 1`)。

tailnet 資料面是通的(介面有 100.67.0.12,直連 8443 正常),只有 DNS 這一層壞。
Pixelbook 端若 MagicDNS 正常就不受影響,但從 cashcamp 本機驗收時必須加
`--resolve`,否則會連到公網 IP 而得到 `000`。
