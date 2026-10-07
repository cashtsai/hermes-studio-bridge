# CC 逐字串流 Spike(GAP-S3 / parity spec P1-S3)— 2026-08-15

> Branch:`spike/cc-token-stream`。旗標 `CC_TOKEN_STREAM` **預設 OFF**,
> off 時零行為差異(有測試佐證)。本檔是 spike 結論:現況考古、選項比較、
> 本輪修了什麼、實測結果、風險、到可出貨還缺什麼。

## 0. TL;DR

**地基不是「部分」,是「全套但開不了」。** `feat/cc-token-stream`(360a86a,
merge a0ab5c7,2026-08-11)早就把 pane-diff 草稿卡整條路蓋完:狀態機
`CCPaneStream`、250ms 子迴圈、草稿卡 `final:false` rev++、jsonl 正典卡
**接管草稿卡 id** 原位換文、孤兒草稿寬限定稿、36 條單元測試。
flag 一直開不了的**真因是 TUI 版面漂移**:地基寫在 Claude Code v2.0.x
(輸入框是 `╭─╮` 框),現行 v2.1.207 把輸入區換成
`──分隔線 / ❯ 提示行 / ──分隔線 / 狀態行`——舊錨(最後一個 `╭`)不是咬在
頂部歡迎框、就是歡迎框捲走後整個認不出版面 → **一個字都流不出來**。

本 spike:對 v2.1.207 實錄坐實真因 → 修錨點(向下相容兩代版面)→
**真機實測逐字真的流出來了**(見 §4)→ 補實錄 fixture 測試(36 tests 全綠,
全套 suite 與 main 同水位)→ 附診斷工具 `scripts/cc-stream-probe.py`。

## 1. 現況考古:既有 `_cc_token_stream_enabled` 這條路走到哪

| 元件 | 位置(bridge.py) | 狀態 |
|---|---|---|
| 旗標 `CC_TOKEN_STREAM`(每呼叫讀 env,預設 OFF) | `_cc_token_stream_enabled` | ✅ 完成 |
| pane → 對話區純文字(剝輸入區/spinner/chrome) | `_cc_stream_content` | ⛔ **v2.1 版面認不出 → 本 spike 修** |
| 附加型 diff + 捲動尾端錨定重對齊 | `_cc_stream_diff_append` | ✅ 完成 |
| 塊別狀態機(⏺ prose / 工具塊 / ⎿ 結果 / 使用者回顯) | `CCPaneStream` | ✅ 完成(本 spike 補空草稿 lstrip) |
| 草稿卡 upsert(同卡 rev++、`final:false`、origin `pane.stream`) | `_cc_stream_upsert_draft` | ✅ 完成 |
| 正典卡接管草稿 id(jsonl 行到 → 同 id rev++ `final:true` 原位換文) | `_cc_stream_reconcile`(掛在 `_cc_digest_lines`) | ✅ 完成 |
| 孤兒草稿寬限定稿(4s) | `_cc_stream_finalize_expired` | ✅ 完成 |
| busy+有訂閱者才跑的 250ms 子迴圈(取代外圈 1s sleep) | `_cc_stream_subticks`(掛在 `_cc_card_follower`) | ✅ 完成 |
| turn begin/end 掛勾 | follower 的 busy 翻轉處 | ✅ 完成 |

結論:**接著蓋 = 只修版面錨點這一塊**,其餘全部沿用。沒有重蓋任何東西。

## 2. 選項比較(重新驗過,結論與地基當初一致)

**A. tmux pane-diff(選定,既有地基)** — busy+有訂閱者時 250ms
`capture-pane -p`,前後兩張做「附加文字」diff,萃取新增助手 prose 進
`final:false` 草稿卡;jsonl 正典行到達時接管草稿卡 id。
優點:互動 session 完全不動、失敗模式安全(對不上就跳過,正典卡永遠會到)、
無人看/idle 零成本。缺點:對 TUI 版面有假設(本次真因)、拿到的是「TUI
重繪節奏」而非真 per-token(實測 2-3s 一段,見 §4)。

**B. `--output-format stream-json`(否決)** — 只在 `-p`(print/非互動)
模式有效,會整個取代互動式 TUI:tmux send-keys 輸入、審批選單、Esc 中斷、
shift+tab 模式切換全報廢。ccsess 的常駐互動 session 不可用。

**C. hooks(否決)** — Claude Code hooks 沒有 token/delta 級事件
(UserPromptSubmit/Stop 等 turn 級而已),v2.1 仍然如此。

**D. `tmux pipe-pane`(評估過,不換)** — 拿到的是含全部 ANSI 控制序列的
raw 輸出流,TUI 大量用游標移動/重繪,重建「附加文字」需要完整終端模擬器
(pyte 之類),複雜度與風險遠高於 capture diff,且一樣是 TUI 節奏。
capture -p 讓 tmux 幫我們做掉終端模擬,是正確的槓桿位置。

## 3. 本 spike 改了什麼(全部在 `CC_TOKEN_STREAM` 旗標路徑內)

1. **`_cc_stream_content` 錨點支援兩代版面**:
   - ≤v2.0:最後一個 `╭`(原邏輯,fixture 測試保留)。
   - v2.1+:最後一條 `❯` 提示行(輸入區永遠在 transcript 之下,使用者
     回顯的 `❯` 在上方,取最後一個就是輸入區),上緣緊貼的 `──` 分隔線
     一併切掉。兩種都在取「較下面」那個(2.1 歡迎框仍是 `╭─╮`,不能讓
     它偷走錨)。
2. **chrome 剝除補 2.1 實錄變體**:`tmux detected · scroll…` 提示行、
   `⏸ manual mode…`;spinner 新變體(`· Coalescing… (4s · ↓ 79 tokens)`、
   `✻ Coalescing… (running stop hook · …)`、`✻ Brewed for 21s`、
   `· Gitifying…`)原字元組已涵蓋,以實錄 fixture 補測試釘住。
3. **空草稿 lstrip**(`CCPaneStream._grow`):2.1 實測「⏺」先落、字下一
   tick 才到,同行增長會帶著 `⏺ ` 後的空格開頭;≥4 空白會被 markdown 當
   code block。
4. **`scripts/cc-stream-probe.py`**:唯讀診斷工具(只跑 `tmux capture-pane`),
   對任一 tmux CC session 實測萃取器,印逐字到達時間線 + JSON 統計。
   日後 Claude Code 改版懷疑版面又漂移時,先跑這支。
5. **測試**:`tests/test_cc_pane_stream.py` 26 → 36 條——v2.1 實錄版面
   全套(錨點/歡迎框不搶錨/歡迎框捲走/spinner 變體不攪 diff/`❯` 回顯
   不進草稿/`✻ Brewed` 完稿行零擾動/空草稿 lstrip)+ `_cc_stream_subticks`
   全迴圈整合測試(monkeypatch tmux 擷取:草稿 rev++ → 正典接管 final:true)
   + **OFF 零影響**(預設旗標下正典卡原 id 原樣入庫、無訂閱者一張 pane
   都不抓)。

## 4. 實測(v2.1.207 真 session,唯讀,未動 production)

隔離 tmux session(未註冊 ccsess,production bridge 不可見)跑真 `claude`:

- **離線重播**(0.3s×130 張實錄 pane,貓習性 250 字):`layout_none=0`,
  6 段漸進 delta,最終草稿 442ch 與畫面全文一致,零垃圾。
  (未修版:同一份實錄 **0 delta** —— 錨咬在歡迎框,content 幾乎空。)
- **線上探針**(海豚習性 200 字,250ms tick):送出後 **+5.4s 首段字流出**
  (模型思考延遲),之後 5 段 delta(+39/+77/+90/+61/+78 ch)間隔約 2-3s,
  final 草稿 345ch 完整。對照現況(jsonl 整段完成才落 + 1Hz tail =
  **整段 pop-in 且再晚 1-1.5s**),體感差距明確。
- 注意:**節奏是「漸進段落」不是真 per-token** —— Claude Code TUI 自身
  以段落級重繪,pane-diff 拿不到比 TUI 重繪更細的粒度。這已消滅
  「整段 pop-in」,但別對外承諾「打字機逐 token」。

## 5. 對 digest/SSE 契約的接法(不變,重新核對過)

- 草稿卡走 `card.upsert` 同卡 `rev++`、`final:false`;正典到 → 同 id
  `rev++`、`final:true` 原位換文 —— 契約 §1 原生支援,app 已會渲染
  (persona/CX 同路)。
- **節流**:子迴圈 250ms/tick,每 tick 最多一次 upsert → 每卡 ≤4 事件/s,
  天然滿足「不要每 token 一個 SSE 事件、合批 ~100ms」的契約要求;
  SSE 已是事件驅動喚醒(c829bb5),不會再被 0.5s 輪詢量化。

## 6. 風險清單

| 風險 | 評估 / 緩解 |
|---|---|
| **TUI 版面再漂移**(頭號,已發生過一次) | 失敗模式安全:認不出 → 跳過 tick,正典卡永遠會到(退回現況 pop-in,不出垃圾)。緩解:probe 工具一鍵體檢;實錄 fixture 把 2.1 版面釘進測試;升級 claude 後跑一次 probe 應納入 SOP。 |
| **CPU** | 只在「busy ∧ 有訂閱者」時 4 次/s `tmux capture-pane`(subprocess 數十 ms 級);idle/無人看零成本;訂閱者中途走光立即停。多 session 同時忙+同時被看才會疊加,量級仍小。 |
| **雙寫競態(草稿 vs 正典)** | bridge 全程單 asyncio 事件圈,無真併發;接管靠「同 id rev++」原位換文,app 端無縫。孤兒草稿 4s 寬限自行定稿,不會永遠掛 `final:false`。理論殘口:正典行恰在草稿 upsert 同 tick 到達 → 下一 rev 覆蓋,終態仍正典。 |
| **與 1Hz tail 共存** | tail 照跑不動(正典來源);stream 只是提前劇透。OFF 時零接觸(測試釘住)。 |
| **busy 偵測抖動** | 2.1 spinner 不再帶 `esc to interrupt`,`(running stop hook · …)`/`· Gitifying…` 不匹配 `_CC_BUSY_RE` → pane 判 busy 在這些片刻會誤 idle,turn begin/end 可能抖動(影響 stream 起停與現有 status,**非本 spike 引入**)。hook 州新鮮時以 hook 為準可壓掉大半。出貨前建議順手放寬該 regex(動到現有 status 語意,獨立小 PR)。 |
| **雙寬字撕裂** | 實測一例:`渾濁` капture 到一半成 `渾�`,已進草稿;正典接管即修復。可加「piece 內 U+FFFD 丟棄該 tick」再壓低,非阻斷。 |
| **系統通知混入** | `⏺ Session model … could not be restored` 這類系統行與 prose 同記號,會短暫進草稿;正典接管即消失。可加黑名單 regex,非阻斷。 |
| **草稿上限** | 12k ch 飽和後停止增長等正典;超長回覆尾段退化為現況(可接受)。 |

## 7. 為什麼必須與 GAP-S2 綁定出貨

GAP-S2 = app 端 `CardStreamView.swift:768` 對**每次** `card.upsert` 都把
整卡 markdown 全文重解析(O(n²) 累積)。現況 CC 一張助手卡一生只有
1-2 個 rev,爛得無感;**CC_TOKEN_STREAM 一開,同一張卡變成每秒最多 4 個
rev、一輪回覆數十 rev**,每個 rev 都全量重解析,正好踩中 build 103 卡頓案
同款病灶(當時是 persona 流卡把主執行緒打死)。也就是說 bridge 這半邊
單獨上線 = 把 app 已知的 O(n²) 病灶從「無感」推進「必發作」。
綁定出貨:app 端增量渲染(block 級 diff,`feat/stream-incremental` 那條線)
必須先/同批到位,CC_TOKEN_STREAM 才能開旗標。

## 8. 到可出貨還缺什麼(工作量估計)

| 項目 | 量級 |
|---|---|
| app 端 GAP-S2 增量渲染落地驗證(對 4 rev/s 草稿卡實測不掉幀) | M(app 線) |
| 真機端到端:對 ccsess 正式 session 開旗標(測試 bridge 實例或維護窗)驗 Pocket 真機體感 | S-M |
| `_CC_BUSY_RE` 放寬(2.1 spinner 變體),連動現有 status 語意回歸測試 | S(獨立 PR) |
| U+FFFD / 系統通知行過濾(草稿化妝,非阻斷) | S |
| 多忙碌 session 同時訂閱的 CPU 壓測(4/s × N) | S |
| 上線開關 SOP:plist 加 `CC_TOKEN_STREAM=1` + 重啟(善彰執行);claude 升版後跑 probe 體檢 | 文件即本檔 |

## 9. 檔案索引

- `bridge.py`:`_cc_stream_content` 錨點修正、chrome regex、`_grow` lstrip
  (其餘地基未動)。
- `scripts/cc-stream-probe.py`:唯讀實測探針。
- `tests/test_cc_pane_stream.py`:36 條(v2.1 實錄 fixture + subtick 整合
  + OFF 零影響)。
