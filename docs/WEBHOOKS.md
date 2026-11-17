# Webhook automations —— 外部事件自動開 agent

讓 GitHub PR / CI / 告警等外部事件,透過一個帶簽章的 HTTP 入站,自動用指定
provider 在指定目錄開一條 agent session 並送上 prompt。設計照 t3code 的
webhook automations 搬。

- 契約(schema / HTTP 碼 / 欄位)見 `APP_BRIDGE_CONTRACT.md` §18。
- 實作:`webhooks.py`(SQLite 落庫 + 純邏輯:簽章、filters、去重、coalesce、
  速率、硬化、prompt 模板)與 `bridge.py`(HTTP 路由 + 真的開 session)。
- 測試:`tests/test_webhooks.py`(純邏輯 + Store + HTTP 接線兩層)。

## 架構一句話

```
送方(GitHub/CI) --POST /webhook/{id}(帶 x-hub-signature-256)--> bridge
  → webhooks.evaluate():簽章→去重→速率→filters→coalesce 判定(全落庫)
  → 若「觸發」:bridge 用設定的 provider+cwd 開 session,送硬化後的 prompt
```

入站**不帶 bearer**(送方不可能有我們的 token),唯一身分證明是每條 hook
獨立 secret 的 HMAC-SHA256 簽章。管理面(建立/查詢/改/刪)走既有 bearer。

## 快速上手

### 1) 建立一條 hook(需 bearer)

```bash
curl -sS -X POST "$BRIDGE/app/v1/webhooks" \
  -H "Authorization: Bearer $BRIDGE_TOKEN" -H 'content-type: application/json' \
  -d '{
    "name": "github-pr",
    "provider": "codex",
    "cwd": "/Users/you/apps/your-repo",
    "prompt_template": "審一下這個 PR:{{pull_request.title}}\n{{pull_request.html_url}}",
    "filters": [
      {"op": "equals", "path": "action", "value": "opened"},
      {"op": "is-one-of", "path": "pull_request.base.ref", "value": ["main"]}
    ],
    "dedup_source": "x-github-delivery",
    "rate_per_minute": 30
  }'
```

回應會帶**只出現這一次**的 `secret` 與 `inbound_url`。把這兩個資訊設到送方。

### 2) 設到 GitHub

Repo → Settings → Webhooks → Add webhook:

- **Payload URL**:回應裡的 `inbound_url`
- **Content type**:`application/json`
- **Secret**:回應裡的 `secret`
- **Events**:選你要的(如 Pull requests)

GitHub 會用這個 secret 以 `x-hub-signature-256: sha256=<hmac>` 簽每次投遞,
bridge 逐一驗證。

### 3) 查投遞結果

```bash
curl -sS "$BRIDGE/app/v1/webhooks/<id>/deliveries" \
  -H "Authorization: Bearer $BRIDGE_TOKEN"
```

每筆有 `triggered` 與 `reason`:沒開 agent 的話,reason 會說明是簽錯、被去重、
filter 沒過、被歸併還是超速。

## Prompt 模板與硬化(安全關鍵)

模板用 `{{ json.path }}` 代入 payload 欄位。**每個代入的值都是不可信輸入**,
進 prompt 前一律:

1. 包 `<webhook-data>…</webhook-data>`(模板文字本身可信,不包);
2. 在 `$`、`@`、行首 `/` 後插零寬字元 —— `$skill`、`@file`、`/command`、
   `//command` 這些會被 agent runtime 特別解讀的前綴因此失效;
3. 打斷欄位值裡偽造的 `</webhook-data>` 收尾標籤(擋 tag breakout)。

所以就算有人能發出通過簽章的 payload,也沒辦法用 PR 標題之類的欄位把 agent
導去跑別的技能 / 讀別的檔 / 下控制指令。

> 設計取捨:硬化只「插零寬字元」不刪字 —— 值仍可讀、語意資訊不丟,但危險
> 前綴的「可被解讀性」沒了。

## Filters

- `path`:dot 記法;`commits[].author.name` 的 `[]` 會展開陣列,**任一**元素
  命中即算該條過。
- `op`:
  - `equals` —— 路徑的值等於 `value`。
  - `is-one-of` —— `value` 是陣列,路徑的值屬於它。
  - `exists` —— 路徑解得出值即過;`"value": false` 反向斷言「不存在」。
- hook 的多條 filter 之間是 **AND**;空陣列 = 無條件觸發。

## 去重

- 同一筆投遞(由 `dedup_source` 指定的 header,如 `x-github-delivery`;沒設
  則用 payload 的 SHA-256)在 **7 天**內只處理一次,重投回 `duplicate`。
- 超速(429)的投遞**不佔去重格**,送方重投時只要額度回來就能真的被處理。
- 去重紀錄落庫,bridge 重啟後仍有效。

## Coalesce(合併視窗)

高頻事件(例如一次 push 連帶多個 CI 狀態)不想每則都開一條 agent 時,設:

```jsonc
"coalesce": { "key": "{{repository.full_name}}", "window_seconds": 60 }
```

同 `key` 的連續投遞併進一個視窗(回 `202 coalesced`),視窗 `window_seconds`
(10~3600 秒)**從第一筆**算起到期後,用併攏後最新的 payload **只開一條**
session。視窗落庫,bridge 重啟後由背景 reaper(預設每 5s 巡一次,可用
`POCKET_WEBHOOK_REAP_SECS` 調)接著燒。

> 視窗刻意**不因後續投遞延後 fire 時刻** —— 否則持續的事件流會把視窗無限
> 往後推,永遠不觸發。

## 速率上限

每 hook 每分鐘最多 `rate_per_minute` 筆(預設 60),超過回 `429`。這是保護
session 開啟不被灌爆。速率狀態在記憶體(重啟歸零,語意上可接受)。

## Provider 與 cwd

- `provider`:`codex`(走 `/codexsessions`,一步建 thread 並起 turn)或
  `cc2`(需 `CC_SDK_PROVIDER=1`;先建 session 再走統一 input)。
- `cwd`:session 的工作目錄。觸發時若目錄不存在,provider 的建立會失敗,
  delivery 記 `trigger_error`(HTTP 502)。
- 觸發出來的 session id 形如 `codex:<thread>` / `cc2:<name>`,會寫回 delivery。

## 設定與環境變數

| 變數 | 預設 | 作用 |
|------|------|------|
| `POCKET_WEBHOOKS_DB` | `~/.local/share/pocket-agent/webhooks.db` | hook/投遞/去重/視窗落庫處 |
| `POCKET_WEBHOOK_REAP_SECS` | `5` | coalesce reaper 巡視間隔 |
| `POCKET_PUBLIC_BASE_URL` | (空) | 有設則 `inbound_url` 回絕對 URL,否則回相對路徑 |

## 管理 API 一覽

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/app/v1/webhooks` | 建立,回一次性 secret |
| `GET` | `/app/v1/webhooks` | 列全部(secret redacted) |
| `GET` | `/app/v1/webhooks/{id}` | 單條(secret redacted) |
| `PATCH` | `/app/v1/webhooks/{id}` | 更新;`{"rotate_secret": true}` 換 secret |
| `DELETE` | `/app/v1/webhooks/{id}` | 刪除(連同去重/視窗) |
| `GET` | `/app/v1/webhooks/{id}/deliveries?limit=100` | 投遞日誌(保留 7 天) |
| `POST` | `/webhook/{id}` | **入站**,無 bearer,靠簽章 |

## 跑測試

```bash
~/apps/hermes-agent/venvs/hermes-0.22.2/bin/python -m pytest tests/test_webhooks.py -q
```
