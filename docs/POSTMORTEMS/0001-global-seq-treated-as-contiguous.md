# Postmortem 0001: Console 把全域 event_log 序號當成 per-session 連號驗,進 session 永遠卡在「正在補接」

Status: Resolved — 2026-11-10 熱修上線,2026-11-17 補上 base-fails scenario 證明。

## Executive summary

Console 進任何一條 session 都會立刻卡死在紅字「連線中斷,正在補接」,一張卡都不出來。
原因是事件信封上的 `seq` 是**全域** `event_log` 的 id(所有 session 共用一條序號),
而 Console 的 client 把它當成**這條 lane 自己的連號**來驗:`seq !== latestSeq + 1` 就重同步。
別的 session 只要有任何活動就會在中間佔號,所以本 lane 看到的序號**天生有洞** ——
看到洞 → 重同步 → 重同步後再看到洞 → 無限迴圈。
它逃過了所有防線,因為**沒有任何一道防線同時看得見「序號由誰發」和「序號被誰驗」**:
bridge 側的測試證明全域序號嚴格遞增(正確),Console 側沒有任何測試在跑那段
`subscribeCards`,而兩邊的契約只活在人的腦袋裡。
教訓是:**一個跨 lane 產生的事實,不可以在單一 lane 裡被當成契約消費;
而「哪一半的假設是對的」只有同時驅動兩半的測試才看得出來。**

## Impact

- **誰**:所有使用 Console(浏覽器端)的使用者 —— 實務上就是機主自己與配對過的瀏覽器。
  Pocket app 不受影響(app 端的 `TerminalCardStore` 一直是「只要往前就收」的語意,
  也就是說**正確答案當時就已經存在於同一個產品裡**)。
- **症狀**:點進 session 後畫面只有一行紅字「連線中斷,正在補接」,永遠不消失;
  歷史卡片一張都不載入,新訊息也不出現。Console 看起來整個壞掉。
- **多久**:Console 大改版(`12cc03a`,2026-11-10)上線後即刻發作,
  同日 `1cc59e5` 熱修。以小時計,但**只要有第二條 session 在動就 100% 發作**,
  所以在機主的實機上等於「完全不能用」。
- **怎麼發現的**:機主自己打開 Console 踩到,直接回報「進 session 卡死」。
  沒有任何自動訊號。

## Timeline

| 時間 | 事件 |
|---|---|
| `2026-11-10` `12cc03a` | Console 大改版七件套上線,新的 `subscribeCards` 加入「不連號就 resync」+「ping 尖端不合就 resync」兩條驗證。 |
| `2026-11-10` | 機主打開 Console,進 session 立刻卡在「連線中斷,正在補接」。單一 session 的開發環境裡不一定重現 —— 要有第二條 lane 在動才看得到洞。 |
| `2026-11-10 22:04` `1cc59e5` | 熱修:`seq` 只要往前就收;`ping` 僅推進重連游標,絕不觸發 resync;Service Worker 升到 v9 強制換快取。 |
| `2026-11-17 09:51` `ac64d04` | 補上 `scenarios/global_seq_resync_storm.py`:用產品 API 造出真的有洞的全域序號,再用 `node --test` + `vm` 把**出貨中的** `app.js` 那段 `subscribeCards` 切出來跑。`scripts/prove-scenario.sh global_seq_resync_storm 1cc59e5^` 已 PROVEN。 |

## Why every safety net missed it

- **單元測試(`tests/`)** — 攔不到,而且是結構性攔不到。這條線上的 python 測試
  (`tests/test_v2_events_durable_resume.py`、`tests/test_event_taxonomy_b1.py`)
  驗的是**發送端**:全域 `seq` 嚴格遞增、`durable_seq` 只在里程碑影子出現、
  INSERT 只有一個 chokepoint。**這些斷言全部是對的** —— bug 在消費端,
  而消費端是 `static/console/app.js`,不在任何 python 測試的視野裡。
- **Scenario(`scenarios/`)** — 當時還不存在。`scenarios/` 這個家族是
  2026-11-17(`ac64d04`)才建立的;這隻 bug 是促成它的三個案例之一。
- **掃描式守門** — 結構上碰不到。我們的掃描式守門全部是「登記表完整性」型的
  (事件型別有沒有登記、provider 的格子有沒有表態、有沒有洩漏私有識別),
  它們檢查**符號是否被登記**,不檢查**語意是否被正確消費**。
  「把全域序號當 per-lane 連號」是一個語意錯誤,沒有任何符號缺席。
- **型別與契約** — 這是最根本的那一道,而它整道不存在。信封上的欄位就叫 `seq`,
  一個光禿禿的整數;型別上完全看不出它的序號空間是**全域**還是**per-session**。
  `docs/APP_BRIDGE_CONTRACT.md` 當時也沒有寫明這件事。
  同一個名字在兩個產生者/消費者之間承載了兩種語意,型別沒有任何力量阻止。
- **部署後煙霧測試(`scripts/post-deploy-smoke.py`)** — 攔不到。那支刻意設計成
  **唯讀、不花錢、驗不變式**,只打 HTTP/JSON 面。它會確認 `/app/v2/.../cards`
  回得出卡片與遞增的 `durable_seq`(都正常),但它不跑瀏覽器、不執行 `app.js`,
  所以「client 收到正確資料之後自己判定要重同步」這件事完全在它的盲區。
- **人工驗收** — 攔到了,但只在 production、而且是**唯一**攔到的那道。
  開發機上常常只有一條 session 在動,序號剛好連號,bug 不重現 ——
  這隻 bug 的發作條件是「併發的第二條 lane」,那是機主機器的日常,不是開發環境的日常。

## Root cause

**同一個欄位名承載了兩個不同的序號空間,而型別與契約都沒有把它們分開。**

`seq` 是全域 `event_log` 的 primary key,它唯一保證的是**全域嚴格遞增**;
它在 per-session lane 上出現時,只有「順序」這一個意義,「連續」從來不成立。
Client 卻需要一個「我這條 lane 有沒有漏事件」的判準,而手邊唯一的整數就是它,
於是把「遞增」誤讀成「連號」—— 這不是手滑,是**缺少一個能表達『這條 lane 的斷層』
的型別**所以只能拿最近的整數湊。

第二層放大器:`ping`(keepalive)也帶著全域尖端。於是即使 client 不收任何真事件,
光靠 keepalive 就能把自己推進 resync,讓迴圈自我維持 —— 一個**資訊性**訊號
被拿去做**致命性**判斷。

(這與 deepseek-harness postmortem 0004 是同一族:那邊是 launcher 的
`landlock-run: ` 善意提示行被當成失敗證據,這邊是 keepalive 的尖端被當成斷層證據。
**資訊性與致命性診斷共用一個通道,消費端一定會把兩者接錯。**)

## Guardrails added

1. **症狀級 scenario,且「前提本身要是真的」** —
   `scenarios/global_seq_resync_storm.py`(`ac64d04`)。
   它不手刻序號:先用產品自己的 API 建兩條 CC session、交錯送訊息,
   從 `GET /app/v2/sessions/{id}/cards` 讀回真的 `durable_seq`,
   然後**先斷言那串數字真的有洞** —— 沒洞就直接報錯,因為前提消失的話綠色是假的。
   revert `1cc59e5` 這支就紅。
2. **驗的是出貨中的那一份 JS** — 同一個 scenario 的 JS 半邊用 `node --test` + `vm`
   把 `static/console/app.js` 裡的 `subscribeCards` 原始碼切出來跑
   (沿用 `tests/console_delivery_sort.test.cjs` 的既有作法)。
   所以在舊 commit 上它驗到的是**舊的那份邏輯**,而不是一份對不上的複製品。
3. **base-fails 證明制** — `scripts/prove-scenario.sh global_seq_resync_storm 1cc59e5^`
   (`ac64d04`)。把 base commit 檢出到拋棄式 worktree,在那裡跑 scenario
   (**必須紅**),再在 head 上跑(**必須綠**),兩者都成立才印 `PROVEN`。
   這把「這條 guardrail 真的會為這個機制而紅嗎」從人的自信變成一道可重跑的指令。
4. **語意寫進程式碼旁邊** — `static/console/app.js` 那段現在帶著五行 zh-Hant 註解,
   寫明「`seq` 是全域序號、洞是正常的、真正的斷層由 410/resync 協定負責、
   ping 只推進游標」。下一個讀到這段的人不需要重新推導一次。

### 還沒做的

- **`seq` 的序號空間仍然只靠註解與文件區分,型別上沒有分家。**
  正解是讓 per-lane 的斷層偵測有自己的欄位(例如 lane 內單調的 `lane_seq`,
  或由伺服器明確送出 `gap` 信號),而不是讓 client 從全域序號推論。
  這是協定改版,牽動 app / Console 兩個消費端,不在熱修的範圍內。

## Lessons

> 「全域產生的序號,在單一 lane 上只保證順序,永遠不保證連續。
> 任何『不連號就重來』的 client 邏輯,都要先回答『這個序號是誰發的』。」

> 「Keepalive 是資訊性訊號。讓資訊性訊號能夠觸發致命性動作,
> 迴圈就會自我維持 —— 不需要任何真實錯誤發生。」

> 「同一個產品裡已經有一個正確答案(app 端的 `TerminalCardStore`),
> 新寫的第二個 client 卻重新發明了一個錯的。**第二個消費端是抄語意的時機,
> 不是重新推導的時機。**」

> 「一個只在『有併發的第二條 lane』時才發作的 bug,
> 在開發機上是 0% 重現、在機主機器上是 100% 發作。
> 『我本機跑過沒事』對這一類 bug 完全沒有證據力。」
