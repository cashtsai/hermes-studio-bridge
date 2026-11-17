# Postmortem 0002: Claude Code 的評分問卷佔走輸入框,從 Pocket 送訊息一律 409 CC_INPUT_NOT_ACCEPTED

Status: Resolved — 2026-11-15 自動關閉上線(`4c87829`),2026-11-16 補上偵測登記表那一格(`ee2a31c`),2026-11-17 補上 base-fails scenario。

## Executive summary

Claude Code 的 TUI 偶爾會在回合結束後彈出一張可選的評分頁
(`How is Claude doing this session? … 0: Dismiss`)。它**佔走輸入框的位置**,
於是 bridge 掃不到 composer,`POST /app/v2/sessions/{id}/input` 回
409 `CC_INPUT_NOT_ACCEPTED`,手機上只有一行紅字「不在可輸入狀態」。
使用者完全不知道那台電腦上有個框在等人按一下鍵 —— session 看起來就是死了。
**兩天內發作兩次**(11-13、11-15)。
它逃過所有防線的原因是:bridge 的 CC 狀態偵測引擎是**資料驅動的 manifest**
(`cc_detect.py`),而 manifest 是一份**開放集合** ——
第三方 TUI 新增一個畫面態時,沒有任何東西會因為「這個態沒被登記」而紅;
未登記的畫面只會安靜地掉到 `idle`,然後 Pocket 顯示「待命」並把後續輸入
送進被 modal 蓋住的 pane。
教訓是:**消費第三方 UI 的偵測規則表,它的『完整性』不可能被靜態證明,
所以必須把『未登記 ⇒ 大聲』做成預設,而不是『未登記 ⇒ idle』。**

## Impact

- **誰**:所有用 Pocket 驅動 tmux 型 Claude Code session 的使用者。
  實務上就是機主的 `shan-ops` 這條線。
- **症狀**:在手機上送任何訊息都立刻失敗,紅字「不在可輸入狀態」;
  session 列表上看起來是「待命」(因為偵測掉到 idle),
  所以使用者的結論是「這條線死了」而不是「有個對話框在等我」。
  **唯一的解法是走到那台電腦前面按一下鍵**,而產品沒有任何地方透露這件事。
- **多久**:每次發作持續到機主實體走到機器前為止(以小時計)。
  兩天內兩次 —— 這張問卷的出現頻率不是罕見事件。
- **怎麼發現的**:機主在手機上送不出訊息,回報「又卡成看起來死了」。
  第一次(11-13)是人工診斷出來的,第二次(11-15)才拍板自動關閉。

## Timeline

| 時間 | 事件 |
|---|---|
| (上游) | Claude Code 在回合結束後偶發彈出可選評分頁。這是**外部輸入的變化**,我們沒有收到任何通知,也不可能有。 |
| `2026-11-13` | 第一次發作。機主在手機上送訊息一律 409。人工診斷:問卷佔走 composer。同一天的對話裡**原封不動聊過** `How is Claude doing this session?` 這句英文 —— 這件事後來成為誤觸防護的設計依據。 |
| `2026-11-13` | 實機試過 `0` + `Enter` 關問卷 —— **關不掉**。單鍵 `0`(不帶 Enter)才有效。 |
| `2026-11-15` | 第二次發作。機主拍板:不需要給 Anthropic 評分,偵測到就自動關掉。 |
| `2026-11-15 07:54` `4c87829` | 修復:貼字前偵測到問卷就送單鍵 `0`,關完重拍畫面走原流程;關不掉時 reason 誠實回報 `survey_blocking` 並附人工處置提示,不再混進籠統的 `composer_stuck`。5 支測試。 |
| `2026-11-16 21:28` `ee2a31c` | 補遺:同一張問卷在 `cc_detect.py` 的 manifest 裡也加一條 `session_feedback_survey` 規則(`state: blocked`, `priority: 855`)。在這之前,**狀態偵測仍然會把問卷態判成 idle** —— 修掉了「送不進去」,但「顯示待命」還在。 |
| `2026-11-17 09:51` `ac64d04` | 補上 `scenarios/cc_input_not_accepted_when_survey_modal.py`。`scripts/prove-scenario.sh cc_input_not_accepted_when_survey_modal 4c87829^` 已 PROVEN。 |

## Why every safety net missed it

- **單元測試(`tests/`)** — 攔不到,因為它們全部是**已登記畫面態**的測試。
  `tests/test_cc_detect.py` 當時有十幾個 pane fixture(spinner、permission prompt、
  model picker、context full…),每一個都綠;
  `tests/test_cc_input_delivery.py` 驗「有 composer 時貼字會進去」,也綠。
  **沒有任何測試能問出「還有哪些畫面我沒登記」** —— 那個問題的答案住在上游的 TUI 裡,
  不在我們的 repo 裡。這是 manifest 制度**內建的**盲區,不是漏寫了一支測試。
- **Scenario(`scenarios/`)** — 當時還不存在(2026-11-17 `ac64d04` 才建立)。
  這隻是促成它的三個案例之一;也是三個裡面唯一「使用者症狀」與「技術根因」
  距離最遠的一隻(症狀:送不出訊息;根因:一張可選問卷的鍵盤處理)。
- **掃描式守門** — 整道不適用,而「為什麼不適用」就是本案的核心:
  我們的掃描式守門全部建立在**封閉集合**上 ——
  `tests/test_event_taxonomy_b1.py` 能逼每個事件型別表態,是因為事件型別
  **由我們自己發射**,掃源碼就能窮舉;
  `tests/test_v2_session_ops.py` 能逼每個 (op, provider) 格子表態,是因為兩個軸
  **都是我們定義的**。
  CC 的畫面態是**第三方定義的開放集合**,沒有任何地方可以掃出全集,
  所以沒有任何守門可以斷言「manifest 完整」。
- **型別與契約** — 攔不到,而且當時還主動幫忙隱藏了症狀。
  `_cc_not_accepted_reason()` 的回傳型別是一個字串,預設落點是
  `"composer_stuck"`(= 「CC 沒有收下這則訊息,請重送」)。
  「不知道為什麼」與「已知是 composer 卡住」**共用同一個值**,
  於是一個未知的新畫面態會穿上一個已知原因的外衣,把診斷帶往錯的方向。
  同理,`cc_detect` 沒命中任何規則時的落點是 `idle`(= 「待命」)——
  **預設值把未知偽裝成已知,是本案真正的放大器。**
- **部署後煙霧測試(`scripts/post-deploy-smoke.py`)** — 攔不到。那支驗的是不變式
  (用量不得超過容量、idle 的 session 不該掛著錯誤),而這隻 bug 的狀態
  **完全符合所有不變式**:session 是 idle、沒有錯誤、用量正常。
  一張問卷蓋住輸入框在任何不變式底下都是合法狀態。
- **人工驗收** — 攔到了,兩次,而且是唯一的那道。但代價是機主兩次在手機上
  以為 session 死了,第二次才升級成「這要自動化」。

## Root cause

**我們把一個第三方定義的開放集合,當成封閉集合在消費,而且未登記的落點是「正常」。**

`cc_detect.py` 的 manifest 引擎本身是好設計(規則寫成資料、可熱更新、
壞檔退回內建、偵測永不 crash)。問題在**沒命中任何規則時要怎麼辦**:
當時的答案是 `idle`。對一個輪詢路徑來說「壞了要靜默降級」是對的,
但「沒有任何規則命中」與「這個 pane 真的在待命」是**兩件不同的事實**,
它們共用一個值,所以第一件事永遠不會被看見。

同一個形狀在下游重複一次:`_cc_not_accepted_reason()` 的
`"composer_stuck"` 也是一個「未知落在已知身上」的預設值。
一個病根,兩層放大。

(這與 deepseek-harness postmortem 0004 的教訓二逐字對應:
「資訊性與致命性診斷共用命名空間時,排除必須精確而窄,**未知的 fatal 行要 fail-closed**」。
我們的版本是:**未知的畫面態不可以 fail 成 `idle`。**)

## Guardrails added

1. **問卷態進了偵測登記表** — `cc_detect.py` 的 `session_feedback_survey` 規則
   (`ee2a31c`),`state: blocked`、`priority: 855`(高於 bash permission prompt
   的 850,低於 model switch)。
   `tests/test_cc_detect.py::test_blocked_session_feedback_survey` 釘住它;
   移掉規則這支就紅。
2. **送出路徑會自己關掉問卷** — `bridge.py` 的 `_cc_survey_overlay()` +
   貼字前的 dismiss(`4c87829`,`bridge.py:19236`、`:19523-19529`)。
   送單鍵 `0`、不帶 Enter(11-13 實機證據:`0`+Enter 關不掉),
   關完重拍畫面走原流程,並發 `cc_survey_dismissed` 事件帶 `cleared=` 結果。
3. **誤觸防護被測試釘死,不是靠註解** —
   `tests/test_cc_survey_overlay.py::test_survey_text_quoted_in_conversation_is_not_overlay`
   (fixture 名 `QUOTED_IN_CHAT`)與 `test_survey_scrolled_into_history_not_detected`。
   偵測是**雙條件**:問卷文字在畫面尾段 **AND** composer 真的不見了。
   單看文字必誤觸(11-13 的對話裡就有那句英文),而誤觸的代價是把 `0`
   打進使用者的草稿 —— 這兩支測試就是在防止後人「簡化」掉第二個條件。
4. **關不掉時誠實回報,不混進籠統原因** — `survey_blocking` 這個 reason
   (`4c87829`,`bridge.py:19258-19259`、`_CC_NOT_ACCEPTED_HINT`)
   帶人工處置提示「請在那台機器按一下 0 關掉後重送」。
   `tests/test_cc_survey_overlay.py::test_reason_reports_survey_when_blocked` 釘住。
   這條直接修掉「未知穿上 `composer_stuck` 外衣」的下游那一層。
5. **症狀級 scenario + base-fails 證明** —
   `scenarios/cc_input_not_accepted_when_survey_modal.py`(`ac64d04`)。
   它造的現場是「上面一段對話、下面是問卷、**沒有輸入框**」,
   只打 `POST /app/v2/sessions/{id}/input`,然後**驗行為不驗回傳值**:
   斷言那則訊息真的打進 pane、而且 `0` 真的被送出。
   `scripts/prove-scenario.sh cc_input_not_accepted_when_survey_modal 4c87829^` 已 PROVEN。

### 還沒做的

- **`cc_detect` 的「未命中」仍然落在 `idle`。** 正解是讓未命中成為一個
  **顯式的第三態**(例如 `unknown_screen`),由呼叫端決定要不要當 idle ——
  這樣下一張 CC 新彈窗會讓 Pocket 顯示「畫面我不認得」而不是「待命」,
  使用者至少知道要去看一眼。這會動到狀態詞彙與 app 的消費面,是獨立一刀。
  Status 仍標 `Resolved` 是因為本案的三個具體缺口(送不進去、顯示待命、
  原因籠統)都已有閘門;這一條是**同族的下一隻**,不是本案的殘留。

## Lessons

> 「偵測規則表在消費第三方 UI 時,它的完整性**不可能**被靜態證明。
> 唯一能做的是讓『未登記』變成一個響亮的狀態,而不是一個安靜的預設值。」

> 「預設值不可以把未知偽裝成已知。`idle`、`composer_stuck` 這種
> 『聽起來像已知原因』的落點,會把診斷主動帶往錯的方向 ——
> 比直接回 `unknown` 糟糕得多。」

> 「同一句文字在 UI 裡出現,可能是**介面**也可能是**內容**。
> 任何靠畫面文字做判定的規則,都必須有第二個獨立證據
> (本案:composer 真的不見了)。單一子字串不是協定。」

> 「兩天內第二次發作才升級成自動化。**『人工處置一次』與『同一件事人工處置兩次』
> 是不同等級的訊號** —— 第二次就該假設它會有第三次。」
