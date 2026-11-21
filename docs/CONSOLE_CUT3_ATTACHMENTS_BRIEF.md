# W1-③ 決策單：讓「agent 傳的檔」真的掛得上（需機主拍板）

> 2026-11-21。機主回報:Pocket 裡 agent 說「檔案傳給你了」、對話卻沒附件泡泡
> (app+Console 都中)。根因已查清,但治本翻的是**有事故前科的安全護欄**,
> 不該擅自改,列三案請你選。診斷那半(把靜默丟棄變吵)已先做掉(commit dba501f)。

## 根因（已查證）
附件沒有「明確附檔」這個動作。整條管線是**啟發式**:回合結束掃卡片文字,找
「絕對路徑字串」,過安全門後複製進 artifact 庫、掛成 `/app/v2/artifacts/{id}`。
三關卡住了那個密碼檔:
1. **人格(hermes/persona)沒有工作目錄 root** → 只准 `/tmp` 的檔(`bridge.py:582-607`
   沒有 hermes 分支)。人格寫到家目錄/專案的檔一律被丟。
2. **`.txt/.md/.env` 被 `ATTACHABLE_KINDS` 明確排除**(`media_artifacts.py:115`)。
3. 路徑還得逐字出現在卡片文字裡才會被掃到。

## 為什麼不能直接放寬（護欄的來歷，程式碼註解白紙黑字）
- **把家目錄當 root = 外洩**:cc2 沒給 workdir 時 cwd=家目錄,agent 只要在逐字稿
  提到 `~/Downloads/某合約.pdf`,那份 PDF 就被複製進庫、長出 Bearer 可取的 URL
  (`media_artifacts.py:120-133`)。
- **把 text/file 列為可附件 = 洗版**:code agent 每步都在講 `.py/.json/.log`,全掛
  上去 → 每張卡一排垃圾附件(2026-10-29 真的發生過,一個 pptx 旁掛 15 個碎片,
  `media_artifacts.py:108-117`)。

即「放寬」會同時重開這兩個洞。所以要的是**用意圖當閘**,不是全面放行。

## 三案

### A.（建議）明確附檔機制 + 人格專屬 staging root
- 給 agent 一個**明確附檔**的方法:emit 一張結構化附件卡(`body.path`,carddigest
  已原生支援 `carddigest.py:139-141`,只差它目前仍被 kind-filter 擋)。對**明確**
  附件:**跳過 kind-filter**(.txt/密碼/任何類型都能掛),但**保留** root+symlink
  逃逸+存在性檢查(所以仍不能掛 `/etc/passwd`、仍不能掛 root 外的檔)。
- 啟發式文字掃描**維持嚴格**(ATTACHABLE_KINDS 不動)→ 洗版/外洩零風險,行為不變。
- 給人格一個**專屬 staging 目錄**當 root(例如 `~/.pocket/persona-outbox/<id>/`,
  **不是**人格家目錄)→ 人格把要給你的檔寫這、明確附上,既掛得上又不曝露人格內部狀態。
- 代價:agent 要**用**這個機制(system prompt 要教;我之後自己送檔會用)。舊的純文字
  「檔案傳給你了」仍不會自動掛——但至少現在 log 會記下 `media_ref_rejected` 讓你看到。
- **這是唯一同時「修好你的案例」又「不重開兩個洞」的選項。**

### B.（不建議）直接放寬護欄
ATTACHABLE_KINDS 加 text/file + 人格 root 給家目錄。立刻修好,但重開上述兩個事故洞
(家目錄外洩 + 附件洗版)。不建議。

### C. 只留診斷、先不改行為（已完成那半）
靠剛加的 `media_ref_rejected` log 收真實資料,看多常發生、哪種原因多,再決定。
零風險,但你的案例現在還是掛不上。

## 我的建議
**A**。它是「明確附檔跳過類型限制、但守住路徑邊界」——既讓你那種「把密碼檔/報告傳給我」
真的掛得上,又不讓啟發式掃描亂掛家目錄的東西。人格走專屬 outbox,安全又乾淨。
你點 A,我就實作(bridge 端明確附件繞過 kind-filter + 人格 outbox root + 測試),
並更新送檔的 agent 慣例。
