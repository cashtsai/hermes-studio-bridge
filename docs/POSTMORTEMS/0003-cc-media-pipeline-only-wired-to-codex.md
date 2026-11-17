# Postmortem 0003: 媒體附件管線只接在 codex 一家,CC/cc2 產出的檔案在 Pocket 看不到;修它的時候又種下「先提到、後產生的檔案被永久黑名單」

Status: Resolved — 2026-11-17 補齊管線(`36bb691`)+ 四路複查修 6 隻真 bug(`2fb8c4c`)+ base-fails scenario(`ac64d04`);對齊矩陣守門於本分支補上。

## Executive summary

機主 2026-11-17 回報:「CC 給的影片在 Pocket 看不到,要開 Claude 才拿得到。」
根因雙重:(1) `carddigest._IMAGE_EXTS` 只認 7 種圖片,`.mp4`/`.pdf`/`.pptx`/`.csv`/`.zip`
**根本不出卡**;(2) 真正掛附件的 `attach_captured_media` 靠「內文含絕對路徑」比對,
而 attachment 卡沒有 `body.text` 永遠不命中 —— 且**那支函式全 repo 只被 codex 的
seed 呼叫一次**,tmux CC 這條線從來沒接上。
它逃過所有防線的原因是:缺口的形狀是**「某個 provider 少接一個掛點」**,
而我們當時唯一有的對齊矩陣守門(`tests/test_v2_session_ops.py`)只管
session 操作那一張表 —— 媒體掛點不在任何表上,所以一個格子空著,
沒有任何東西會紅,文件甚至還寫著 ✅。
更值得記的是**修它的過程**:第一刀(`36bb691`)69 測綠、功能看起來好了,
四路獨立複查卻挖出 6 隻真 bug,其中最嚴重的一隻
(「先提到、後產生的檔案被永久黑名單」)**正是這個功能要治的病換個地方發生**。
教訓是:**一個橫跨 N 個 provider 的能力,如果沒有一張逼每個格子表態的表,
它就只會在你剛好測到的那一家上work;而「69 測綠」證明的是你想到的那些事,
不是這個功能的正確性。**

## Impact

- **誰**:所有用 Pocket 看 tmux Claude Code / cc2 session 的使用者。
  codex 那一家是部分可用(seed 冷載有掛,但 cwd 分支缺失 ⇒ 只掃得到 `/tmp`)。
- **症狀**:agent 說「影片算完了,在 `/work/render.mp4`」,
  Pocket 上就只是一行**裸路徑文字**,點不開、下載不了。
  使用者的處置是「去開 Claude Code 桌面版拿檔案」——
  也就是**產品在它最該有用的時刻把人趕回去用別的工具**。
  而 log 會報 `media_cards_attached cards=0`,**沒有任何錯誤**。
- **多久**:從媒體管線存在的第一天起就只接在 codex;
  CC/cc2 那條線是**從未實作**,不是壞掉。以月計。
- **怎麼發現的**:機主自己在手機上拿不到影片,直接回報。
  `cards=0` 這個數字一直在 log 裡,沒有人也沒有任何東西在看它。

## Timeline

| 時間 | 事件 |
|---|---|
| (更早) | 媒體擷取/附件管線為 codex 建立。`attach_captured_media` 只有一個呼叫點(codex seed),靠「卡片內文含絕對路徑」比對。 |
| (更早) | `carddigest._IMAGE_EXTS` 定為 7 種圖片副檔名。非圖片的 agent 產出**根本不會出卡**。 |
| `2026-11-17 09:06` `520deb1` | 八家 provider 能力矩陣體檢(唯讀)。這份盤點是下一刀的依據。 |
| `2026-11-17` | 機主實錘:「CC 給的影片在 Pocket 看不到,要開 Claude。」 |
| `2026-11-17 09:08` `36bb691` | 第一刀:媒體管線補到 CC/cc2,卡片帶 `body.attachments[]`(filename/kind/media_id/download_url),Bearer 直接下載得到位元組。**69 測綠**(含路徑逃逸/白名單/去重/live+seed 兩路)。 |
| `2026-11-17 09:40` `2fb8c4c` | 四路獨立複查(line-by-line / removed-behavior / cross-file / reuse)彼此印證,挖出 **6 隻真 bug**,每一隻都實驗重現過、每個修正都做過變異測試確認會紅。見下「六隻的共同形狀」。 |
| `2026-11-17 09:51` `ac64d04` | 補上 `scenarios/cc_media_attachment_missing.py`。`scripts/prove-scenario.sh cc_media_attachment_missing 36bb691^` 已 PROVEN。 |
| (本分支) | 補上媒體掛點對齊矩陣守門 `tests/test_media_attach_matrix.py` —— 把 `docs/ADDING_A_PROVIDER.md` 裡的散文檢核項變成會紅的閘。 |

### 六隻的共同形狀(`2fb8c4c`)

1. **先提到、後產生的檔案被永久黑名單**(最嚴重)。`ffmpeg -i a.mov out.mp4`
   的工具卡 1 秒內落地、檔案 30 秒後才寫完;原版在 vet **之前**就把路徑記進
   `seen`,於是那支 mp4 **整個 session 再也掛不上**。
   修法:`vet_attachable_path` 改三態(`str` = 收 / `False` = 待會再看 /
   `None` = 永久不合),且「有待重試就不標記這張卡掃過」。
2. **冷載那條把 live 那條的工作吃掉**。兩條共用「掃過了」記憶,`emit=False`
   的呼叫(媒體抽屜輪詢、第二台裝置、guest 頁 5 秒刷新)搶先就只寫 store
   不發事件 → 在線 app 的 rev 永遠不前進。改成一律發 `card.upsert`。
3. **工具卡搶走會渲染的卡**。CC 的形狀是「先宣告、再動手」,純取「最後提到」
   會讓 `tool_result` 拿到而 markdown 拿不到 → 使用者還是看到裸路徑、
   log 卻報成功。改成 attachment > text/markdown > 工具卡(保底)。
4. **跨回合去重是回歸**。同一份報告在第 3 與第 18 回合各被引用時只剩最後一次
   → 往上捲又變裸文字。`covered` 改成 `(turn_id, ref)`。
5. **cwd = 家目錄 ⇒ 整個家目錄可下載**。cc2 沒給 workdir 時 cwd 是 `~`。
   `attach_roots` 現在擋掉家目錄 / `/Users` / `/var` 等過大的根。
6. **codex 沒有 cwd 分支** ⇒ 只掃得到 `/tmp`,**而文件寫著 ✅**。
   補 `_CX_THREAD_CWD` 旁觀快取。

六隻裡有四隻(1、2、3、4)的使用者症狀**與原 bug 完全相同**:看到裸路徑。
也就是說第一刀如果直接出貨,機主會回報「還是看不到」,而我們會以為修好了。

## Why every safety net missed it

- **單元測試(`tests/`)** — 攔不到原 bug,因為**沒有一支測試是以「所有 provider」
  為主體寫的**。既有的媒體測試(`tests/test_media_artifacts.py`、
  `tests/test_cc_image_bytes.py`)驗的是「給定一個合法輸入,擷取/授權做對了嗎」,
  每一支都綠 —— 它們證明元件在**它自己的假設下**正確,
  而缺口是「這個元件在 CC 這條線上根本沒被呼叫」。
  沒有被呼叫的程式碼不會讓任何測試變紅。
  第二刀的 6 隻則說明更尖銳的一點:**第一刀的 69 支新測試也全綠**,
  因為它們是寫新功能的人自己想到的那些情況 ——
  「先提到後產生」「兩條路徑共用記憶」這兩個時序問題,
  要四路複查才被挖出來。
- **Scenario(`scenarios/`)** — 當時還不存在。這隻是促成它的三個案例之一,
  也是最能說明 scenario 價值的一隻:
  `scenarios/cc_media_attachment_missing.py` 的狀態是用**CC 自己的 transcript
  jsonl(產品的外部輸入)+ 工作目錄裡真的有那兩個檔**造出來的,
  然後只打 API 讀卡片 —— 沒有手刻任何一張卡、沒有碰 `canonical.db`。
  所以同一份 scenario 在 base 上跑的是**base 的媒體管線**,
  而不是一份對不上的 fixture。
- **掃描式守門** — 這是本案的核心缺口。我們當時**有**對齊矩陣守門的能力,
  而且用得很好:`tests/test_v2_session_ops.py` 逼 `_V2_SESSION_OPS` 的
  每個 (op, provider) 格子表態(支援、或進 `_V2_OPS_KNOWN_GAPS` 附一句理由),
  `tests/test_provider_caps.py` 再確認能力回報不會漂移。
  **但那張表只收 session 操作。** 媒體掛點(turn-end 的
  `_schedule_media_attach` + `_media_session_cwd` 分支)不在任何表上,
  所以 8 家裡少接 6 家是一個**沒有任何東西在看的格子**。
  「我們有對齊矩陣制度」與「這個能力在矩陣上」是兩件事。
- **型別與契約** — 攔不到,而且反向幫倒忙。
  `docs/APP_BRIDGE_CONTRACT.md` 的 provider 對照表裡 codex 那一格是 ✅,
  **而實際上 codex 連 cwd 分支都沒有**(第 6 隻)。
  散文表格是手維護的,它與程式碼之間沒有任何機械關係,
  所以它會**漂到比真相更樂觀的方向** ——
  一張寫 ✅ 而程式碼交不出來的表,比寫 ⚠️ 糟糕得多。
  同樣地,`docs/ADDING_A_PROVIDER.md` 的新 provider 檢核清單是 markdown checkbox,
  沒勾不會有任何後果。
- **部署後煙霧測試(`scripts/post-deploy-smoke.py`)** — 攔不到。那支驗不變式,
  而「卡片沒有附件」在所有不變式底下都合法 ——
  沒有附件的卡片是絕大多數卡片的正常樣子。
  要抓這隻,煙測必須知道「這條 session 的工作目錄裡有一個 agent 剛產出的檔案」,
  那是狀態依賴的斷言,不是不變式。
- **人工驗收** — 攔到了,而且是唯一的那道。但它攔到的方式是機主**放棄 Pocket、
  改用 Claude Code 桌面版** —— 這種「使用者自己繞過去」的失敗最危險:
  產品沒有壞掉的訊號,只有變得沒用。
  `media_cards_attached cards=0` 這行 log 月復一月地印著,沒有人看。

## Root cause

**一個橫跨 N 個 provider 的能力,沒有一張逼每個格子表態的表;
而唯一宣稱它存在的東西是一份手維護的散文表格。**

真正的機制有兩層:

1. **缺口是「沒有被呼叫」,不是「呼叫錯了」。**
   `attach_captured_media` 的實作沒有 bug;它只有一個呼叫點。
   測試覆蓋率、型別檢查、lint 對「這支函式應該有 8 個呼叫點但只有 1 個」
   這件事**在結構上無話可說** —— 要說得出話,必須先有一個地方宣告
   「這 8 家都該有」,那就是對齊矩陣。
2. **第二刀的 6 隻全部是「兩個真相共用一份狀態」或「資訊性拒絕被當成終局」。**
   第 1 隻:「還沒出現」與「永久不合」共用 `seen` 這一個集合 ——
   與 postmortem 0002 的 `idle` / `composer_stuck`、
   postmortem 0001 的 `ping` 尖端是**同一個形狀**:
   兩種不同的事實被塞進同一個值,於是其中一種永遠不會被看見。
   第 2 隻:冷載與 live 共用「掃過了」記憶。
   第 4 隻:跨回合的 `covered` 用了不含回合的 key。

## Guardrails added

1. **媒體掛點進了對齊矩陣,而且會紅** —
   `tests/test_media_attach_matrix.py`(本分支)。
   它掃 `bridge.py`:`_media_session_cwd()` 的 provider 分支、
   turn-end `_schedule_media_attach` 的呼叫點,
   對 8 家 provider 逐一比對,缺格必須寫進 `_MEDIA_HOOK_KNOWN_GAPS` 並附理由。
   同時它是**帶語料下限哨兵**的(`scripts/sentinel/corpus_sentinel.py`)——
   掃描 regex 壞掉、掃到的分支數掉到門檻以下時**直接紅**,
   不會變成「掃到 0 個格子 ⇒ 0 個缺口 ⇒ 綠」。
2. **症狀級 scenario,狀態由產品自己產生** —
   `scenarios/cc_media_attachment_missing.py`(`ac64d04`)。
   用 CC 自己的 transcript jsonl + 工作目錄裡真的存在的 `render.mp4` / `report.pdf`,
   只打 `GET /app/v2/sessions/{id}/cards` 與 `download_url`,
   斷言拿得到**位元組**。
   `scripts/prove-scenario.sh cc_media_attachment_missing 36bb691^` 已 PROVEN。
3. **三態 vet,且「有待重試就不標記掃過」** —
   `media_artifacts.vet_attachable_path`(`media_artifacts.py:162`)+
   `carddigest.media_refs_in_cards` 的 `seen` / `scanned_cards` 分流
   (`carddigest.py:86-130`)。
   這條是第 1 隻 bug 的結構性修法:把「還沒出現」與「永久不合」**分成兩個值**。
   `tests/test_cc_media_attachments.py` / `tests/test_cc_media_attach_e2e.py`
   有專門的時序測試(卡先到、檔後到),做過變異測試確認會紅。
4. **根目錄收窄成一份共用的授權判斷** — `media_artifacts.attach_roots()`
   擋掉家目錄 / `/Users` / `/var` 等過大的根;
   `within_roots` / `local_path_of` 與 `_safe_local_path` 共用同一份判斷
   (`2fb8c4c`)。第 5 隻的修法,同時消掉「兩份授權邏輯會漂移」這個未來的 bug。
5. **掃附件移出共用路由** — `_v2_card_store` 還原成純路由,
   掃描移到 `_v2_card_store_with_media`(只給三個卡片端點,`bridge.py:31285`)。
   在這之前連**未認證的 guest 頁每 5 秒輪詢**都會驅動檔案系統讀寫。
6. **新 provider 檢核項寫進文件,並由第 1 條的矩陣守門背書** —
   `docs/ADDING_A_PROVIDER.md` 的 "Card attachments" 一節與 Checklist 新增一行
   (`2fb8c4c`)。單獨的 checkbox 不是 guardrail(沒勾沒後果),
   但它現在指向一個會紅的閘,所以算。

### 還沒做的

- **`docs/APP_BRIDGE_CONTRACT.md` §6.1 的 provider 對照表仍是手維護的。**
  正解是從 `bridge.py` 的掛點生成那張表(deepseek-harness 的
  `gen-X.ts --check` 形狀:同一支程式同時當 generator 與 checker,
  所以不可能漂移)。目前退一步:第 1 條的矩陣守門使那張表**漂了會紅**,
  但表的內容本身還不是生成的。

## Lessons

> 「一個橫跨 N 家 provider 的能力,如果沒有一張逼每個格子表態的表,
> 它就只會在你剛好測到的那一家上 work。
> 『我們有對齊矩陣制度』與『這個能力在矩陣上』是兩件完全不同的事。」

> 「**缺口是『沒有被呼叫』的時候,沒有任何靜態工具會說話。**
> 覆蓋率、型別、lint 都只看寫出來的程式碼;
> 『這支函式應該有 8 個呼叫點但只有 1 個』要先有人宣告 8,才有人能數。」

> 「手維護的散文表格會漂到**比真相更樂觀**的方向。
> 一格寫 ✅ 而程式碼交不出來的表,比寫 ⚠️ 糟糕得多 ——
> 後者會讓人去查,前者會讓人停止查。」

> 「`cards=0`、`sent=0` 這種『成功但數量為零』的 log 是最安靜的失敗。
> 任何『本來就常常是 0』的指標都不能當警訊,所以它必須被做成**斷言**
> 而不是被做成日誌。」

> 「69 支新測試全綠,四路複查挖出 6 隻真 bug,其中 4 隻的使用者症狀
> 與原 bug 一模一樣。**新功能的測試證明的是作者想到的那些情況**,
> 不是這個功能的正確性 —— 時序類 bug(卡先到檔後到、兩條路徑共用記憶)
> 幾乎不可能由作者自己想到。」

## 三份 postmortem 的共同結論

0001、0002、0003 的「防線為什麼沒攔到」收斂到同一個答案:

**我們的測試幾乎都在驗「元件在它自己的假設下正確」,
而這三隻 bug 全部發生在兩個正確元件的接縫上。**

- 0001:發送端(全域序號嚴格遞增,對)╳ 消費端(連號才算完整,錯)。兩邊各自的測試都綠。
- 0002:偵測引擎(manifest 規則評估正確)╳ 上游 TUI(新增了一個沒登記的畫面)。
- 0003:擷取元件(授權與去重正確)╳ 呼叫圖(CC 這條線沒接上)。

接縫的特徵是**沒有擁有者**:它不屬於任何一個模組,所以沒有任何一支
以模組為單位寫的測試覺得它該管。三個放大器也一模一樣 ——
**兩種不同的事實共用一個值**(全域 seq 兼任 lane 斷層判準 /
`idle` 兼任「未登記畫面」/ `seen` 兼任「永久不合」與「還沒出現」),
於是其中一種永遠不會被看見。

這直接決定了本 repo 兩套制度的分工:

- **`scenarios/`** —— 以**使用者症狀**為單位,只打公開 HTTP/CLI 介面,
  並且必須在修復前的 commit 上失敗(`scripts/prove-scenario.sh`)。
  它給接縫一個擁有者:症狀不屬於模組,屬於使用者。
- **掃描式登記表 / 對齊矩陣守門** —— 逼每個格子表態(支援,或寫明理由的缺格)。
  它讓「沒有被呼叫」「沒有被登記」這種**不存在的東西**變成可以被數的東西。
  而這類守門自己必須帶**語料下限哨兵**,否則掃不到東西的時候它會報綠 ——
  那就又回到「安靜地什麼都沒有」這個病根。
