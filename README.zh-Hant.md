# Studio · Hermes — OpenAI 相容 bridge + Open WebUI

[English](README.md) · **繁體中文**

一道通往 **Hermes** 多人格 agent 的薄而穩定的前門:它把每個 Hermes 人格包成一個
OpenAI 的「model」,所以任何 OpenAI 相容的客戶端(**Open WebUI**、手機 app、腳本)
都能跟它對話,而且**人格共用的長期記憶原封不動**。與既有的 Telegram gateway
**並行**運作,Telegram 那邊完全不受影響。

```
 手機 / PWA / app ─▶ Open WebUI(成熟的聊天 UI,需登入)
                       └─▶ bridge.py(OpenAI /v1,token 守門)  :8081
                             └─▶ hermes -z --continue owui-<persona>
                                   └─ HERMES_HOME=<profile home>  (= 共用記憶)
 Telegram bots(不變) ─▶ 同一組 Hermes home / state.db  ── 共存,不衝突
```

> ⚠️ **這不是一般的 web service。** 它是一個**會在它所在的機器上執行工具**的 agent
> 閘道 —— 開 tmux session 驅動 Claude Code / Codex、提供終端機、瀏覽檔案、轉達核准
> 決定。**一個 bridge token 等同那台機器的 shell 存取權。**
> 部署前請讀 [`SECURITY.md`](SECURITY.md)。

## 元件

| 元件 | 是什麼 / 在哪 |
|---|---|
| **bridge.py** | FastAPI app。`GET /v1/models`(人格)、`POST /v1/chat/completions`(SSE 串流)。token 守門(`BRIDGE_TOKEN`)。每一回合跑一次 `hermes -z`。 |
| **bridge LaunchAgent** | `deploy/ai.studio.hermes-bridge.plist` → `~/Library/LaunchAgents/`。KeepAlive、RunAtLoad、埠 8081、注入 `BRIDGE_TOKEN`。 |
| **Open WebUI** | Docker(`deploy/run-openwebui.sh`),埠 3000,指向 `host.docker.internal:8081/v1`。需登入(第一個使用者即管理員)。 |
| **人格** | 對應到 Hermes 的 profile home,見 `bridge.py` 的 `PERSONAS`。 |

## 快速開始

```bash
# 1. bridge(安裝或升級 per-user LaunchAgent)
./deploy/install-local-bridge.sh

# 2. 重啟 bridge
launchctl kickstart -k gui/$(id -u)/ai.studio.hermes-bridge

# 3. Open WebUI(需要 Docker/Colima)
colima start
BRIDGE_TOKEN=<你的 token> ./deploy/run-openwebui.sh
```

安裝器結尾會印出一條**免登入的配對連結**(`/pair/qr?boot=…`),在那台 Mac 上開啟、
用手機 Pocket 掃就完成配對 —— 不需要 Apple 登入,也不需要 iCloud。
⚠️ 那條連結等同這台機器的配對權限,別外流。

## 安全

- **bridge**:bearer token 守門(`BRIDGE_TOKEN`,設在 LaunchAgent 的環境變數裡,
  **絕不 commit**)。綁 `0.0.0.0:8081`,但只在區網 / tailnet 上搆得到。
- **不要把 bridge 直接暴露在公網。** 設計上是走你自己的私有網路(Tailscale 之類)
  或你自己掌控的通道。有幾個端點**刻意**拒絕任何帶轉送標頭的請求,原因就在這裡。
- **Open WebUI**:需登入(`WEBUI_AUTH` 預設開)。

漏洞回報請看 [`SECURITY.md`](SECURITY.md) —— **不要開公開 issue**。

## 測試

```bash
pip install -r requirements.txt -r requirements-dev.txt
./scripts/run-tests.sh            # 全套,每個檔案一個行程
./scripts/run-tests.sh console    # 只跑檔名含 "console" 的
```

`run-tests.sh` **刻意逐檔開一個行程**。這個 repo 的測試有兩種血統(正規 unittest
檔、以及在 import 時就執行並 `sys.exit()` 的腳本式驗收檔),第二種在
`unittest discover` 底下會安靜地爛掉。該腳本開頭有完整說明。

結果分三態:**綠 = 跑過了、紅 = 真的壞了、跳過 = 這台機器沒有它需要的東西**
(見 `tests/_requires.py`)。跳過**不會**被算成通過 —— 「看起來全綠、其實沒跑」
比紅色更危險。

## 貢獻

請先讀 [`CONTRIBUTING.md`](CONTRIBUTING.md)。兩件要先知道的:

1. **解釋性註解是繁體中文。** 識別字、API、log 鍵與文件是英文,但註解不是 ——
   而推理都在註解裡,幾乎每一條都記著一次具體事故。所以不會整批翻掉或刪掉。
   看不懂哪一段就在 issue 裡問。
2. **不准 commit 憑證,也不准 commit 私有基礎建設識別**(tailnet 名、個人主機名、
   `/Users/<你>` 路徑)。`tests/test_no_private_infra_leak.py` 用**形狀**判斷,
   所以連**你自己的** tailnet 也會被擋下來。

想找題目的話:`tests/ci-skip.txt` 裡那幾個檔案在 CI 上跑不起來,因為它們依賴的是
某台機器上裝了什麼。把其中一個改成依 `tests/_requires.py` 自己跳過,是很好的
第一個貢獻。

## 授權

**AGPL-3.0**(見 [`LICENSE`](LICENSE))。注意第 13 條在實務上的意思:**你改了這個
bridge 並透過網路提供給別人使用,就必須提供你修改後的原始碼。** 自己私下跑沒有
這個義務。

在上面做商業產品而這條造成困擾的話,開 issue 談,不要默默 fork —— 另外的安排是
有可能的。
