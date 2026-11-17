# Postmortems

一份事後檢討是**回頭看一次失敗的記錄**:什麼壞了、機制是什麼、
**每一道既有防線為什麼沒攔到**,以及為了讓同一類 bug 下次大聲壞掉而
**實際 commit 進 repo 的閘門**。

制度抄自 deepseek-harness 的 `docs/postmortem/`
(研究報告:`~/apps/pa-ship/docs/DEEPSEEK_HARNESS_RESEARCH_20261117.md` §5.5)。
抄的理由很單純:那個 repo 的 `postmortem → 常駐規則 → 閘門` 是一條**完整的三段鏈**,
我們本來只有第一段(commit 訊息裡的 2am 筆記)和第三段(測試),
中間缺一份「為什麼五道防線同時失效」的橫向記錄 —— 於是同一個病根換個模組又來一次。

## 什麼時候寫(三條合格條件,必須**同時**成立)

1. **Subtle(微妙)** —— 機制不顯然。一個認真的工程師讀過現場的程式碼之後,
   還是會重新吃一次同樣的苦。
   *反例:型別打錯、忘記 await 一個明顯的 coroutine —— 修掉就好。*
2. **Systemic(系統性)** —— 它逃脫的原因是**測試/工具/慣例的缺口**,
   不是一次性的手滑。判準:問「如果換一個人、換一個模組,同一條路徑還在嗎?」
   答「在」才合格。
   *反例:某人改錯一行後立刻自己發現 —— 沒有防線被繞過。*
3. **Costly to rediscover(重新發現的代價高)** —— 查這隻病花掉的是小時/天,
   不是分鐘;或者它在 production 上**安靜地**活了一段時間。

**三條缺一條就不要寫。** 氾濫的 postmortem 會退化成流水帳,
而流水帳沒有人讀 —— 那時候連真正重要的三份也一起失去效力。
不合格但值得留的東西,寫進 commit 訊息(本 repo 的 commit 訊息就是長的)
或對應模組的 zh-Hant 註解裡,那是它們的家。

### 這不是 Agent Note、也不是 spec

- **Agent Note / 設計文件**是向前看的:要做什麼、為什麼這樣選。
- **Postmortem 是向後看的**:已經壞了,機制是什麼,防線為什麼全漏。

所以 postmortem 裡不准出現 `## Proposal` / `## Plan` / `## Acceptance criteria`
這類 spec 語言。要提案就另開文件,從這裡連過去。

## 必要結構

逐欄照 [`TEMPLATE.md`](TEMPLATE.md) 填。`tests/test_postmortem_format.py` 是機械閘,
它檢查的是**結構**(章節齊不齊、Status 行在不在、Guardrails 有沒有指到已 commit 的東西);
內容的誠實度只有 review 看得出來,所以 CONTRIBUTING 那一節同時規定了 review 要看什麼。

| 章節 | 要答什麼 | 常見的作弊寫法 |
|---|---|---|
| `Status:`(第三行) | `Resolved` / `Mitigated` / `Open` + 一句話 | 寫 `Resolved` 但 Guardrails 是空的 |
| `## Executive summary` | 一段、三十秒讀完:壞了什麼、平白講的根因、為何逃脫、持久教訓 | 把 Timeline 搬上來 |
| `## Impact` | 誰受影響、多久、怎麼被發現的(**使用者症狀**而不是 stack trace) | 「可能影響部分使用者」 |
| `## Timeline` | 帶日期的事實序列,含**引入**那一刻與**發現**那一刻 | 只寫修復那天 |
| `## Why every safety net missed it` | **逐道列出既有防線,每道一條,說它為什麼沒攔到。不准跳過任何一道。** | 只列兩道然後說「其他不適用」 |
| `## Root cause` | 結構性的那一句 —— 型別表達力不足、兩個真相、契約被壓縮成子字串… | 「某人忘了」(人因結論一律退回) |
| `## Guardrails added` | **已 commit 的閘門**:測試/scenario/lint/CI/腳本,附路徑與 commit | 「下次注意」、「加到 backlog」 |
| `## Lessons` | 可以搬到別的模組用的句子,每句能獨立站著 | 覆述 Root cause |

### 兩條硬規則

**規則一:`## Why every safety net missed it` 要逐道列,不准跳。**
至少要對這幾道各表態一次(不適用也要寫出「為什麼這道結構上碰不到」):

- 單元測試(`tests/`)
- scenario(`scenarios/`,症狀級行為證明)
- 掃描式守門(登記表完整性、識別洩漏、對齊矩陣…)
- 型別/契約面(回傳型別、API 契約文件)
- 部署後煙霧測試(`scripts/post-deploy-smoke.py`)
- 人工驗收(機主自己用)

這一節是整份文件**唯一無法用別的東西代替**的部分。bug 的機制會過期,
「五道防線為什麼同時沒出聲」不會。

**規則二:`## Guardrails added` 必須是閘門,不是感想。**
每一條都要指到一個**已經 commit、會在 CI 紅的東西**:檔案路徑、測試名、commit sha。
判準很硬:**「這條 guardrail 如果我把修復 revert 掉,它會紅嗎?」答不出「會」就不是 guardrail。**
`scripts/prove-scenario.sh <name> <base>` 就是在機械化地問這個問題。

## 編號與檔名

`NNNN-kebab-case-title.md`,四位數、依**事故發生**時間遞增(不是寫檔時間)。
編號不回收、不重排 —— 外面的 commit 訊息與註解會連進來。

## 現有的

| # | 事故 | 日期 | 共同病根 |
|---|---|---|---|
| [0001](0001-global-seq-treated-as-contiguous.md) | Console 把全域 seq 當連號 → 無限補接 | 2026-11-10 | 跨 lane 的事實被當成 per-lane 契約 |
| [0002](0002-survey-modal-blocked-cc-input.md) | CC 評分問卷佔走輸入框 → 送訊息一律 409 | 2026-11-13 / 11-15 | 第三方 TUI 的新畫面態沒有登記表逼它表態 |
| [0003](0003-cc-media-pipeline-only-wired-to-codex.md) | 媒體附件管線只接 codex,CC/cc2 的檔案看不到 | 2026-11-17 | 新 provider 的掛點缺口沒有任何東西會紅 |

這三份的「防線為什麼沒攔到」收斂到**同一個答案**,寫在
[0003 的 Lessons](0003-cc-media-pipeline-only-wired-to-codex.md#lessons) 末尾:
我們的測試幾乎都在驗**元件在它自己的假設下正確**,
而這三隻 bug 全部發生在**兩個正確元件的接縫上** —— 接縫沒有擁有者,
所以沒有任何一支測試覺得它該管。
`scenarios/`(症狀級、只打公開介面)與掃描式登記表守門(逼每個格子表態)
就是為了給接縫一個擁有者才存在的。
