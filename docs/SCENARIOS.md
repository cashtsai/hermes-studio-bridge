# Scenarios —— 「base 失敗 / head 通過」證明制

> 一條測試說自己是綠的,不等於那個功能活著。
> 一個 scenario 必須先在壞掉的世界裡紅過,才有資格在修好的世界裡綠。

這個 repo 的鐵則是**驗行為不驗存活**(`docs/` 與機主記憶裡都有:`is-active` /
`exit 0` / `HTTP 200` 都會騙人)。2026-10-28 一天之內記錄了**七次**「代理指標
說謊」的實例 —— 指標全綠、功能全死。Scenarios 是那條鐵則的工業化版本:把
「使用者會踩到的現象」寫成可執行的形式,並且**用修復前的 commit 當對照組**,
證明這條線真的看得見那個現象。

---

## 什麼是 scenario

**一個 scenario = 一個使用者會踩到的現象的行為證明。**

它住在 `scenarios/`,一個現象一個檔,檔名就是那個現象:

| 檔 | 使用者看到什麼 |
|---|---|
| `cc_input_not_accepted_when_survey_modal.py` | CC 跳評分問卷時,從 Pocket 送訊息一律失敗(紅字「不在可輸入狀態」) |
| `cc_media_attachment_missing.py` | CC 產出的影片/報告在 Pocket 看不到、點不開,只能去開 Claude |
| `global_seq_resync_storm.py` | 一進 session 就卡死在「連線中斷,正在補接」,訊息永遠不出來 |

每個檔的 docstring 都要寫三件事:**現象**、**根因**、**base commit**(修復前
那一顆)。沒有 base,它就不是 scenario。

技術上它是一個 `unittest.TestCase`(本 repo 既有的測試基礎設施,沒有引入新
框架),只是離開碼的語意被 `scenarios/_scenario.py` 重新定義過。

---

## 跑法

```sh
# 單跑(當下的工作樹)
~/apps/hermes-agent/venvs/hermes-0.22.2/bin/python \
    scenarios/cc_input_not_accepted_when_survey_modal.py

# 證明它真的捕捉到那個 bug
scripts/prove-scenario.sh cc_input_not_accepted_when_survey_modal 4c87829^
```

`prove-scenario.sh` 做兩步,兩步都對才印 `PROVEN`:

1. 在**暫存 worktree** checkout `<base_sha>`(修復前的世界),把 head 的
   `scenarios/` 複製進去,跑 —— **必須失敗**。
2. 回到 head 跑同一個 scenario —— **必須通過**。

已證明過的三個(2026-11-17 實跑):

```sh
scripts/prove-scenario.sh cc_input_not_accepted_when_survey_modal 4c87829^
scripts/prove-scenario.sh cc_media_attachment_missing             36bb691^
scripts/prove-scenario.sh global_seq_resync_storm                 1cc59e5^
```

---

## 為什麼一定要 base 失敗

因為「綠色」本身沒有資訊量。一條從來沒紅過的線,和一條根本沒在看的線,
在報告上長得一模一樣 —— 而我們已經被後者咬過七次。

base 沒失敗,只有三種可能,**沒有一種是好消息**:

- 這個 scenario 驗的東西跟那個 bug 無關(又在驗存活);
- base commit 選錯了(修復其實更早就進去,所謂的「修復」沒修到任何東西);
- 替身把出問題的那條路繞過去了(驗到了一條不存在的程式)。

所以腳本在這一步直接拒絕發證,並把三種可能印出來。`tests/test_prove_scenario.py`
釘住的就是這條:拿一個永遠綠的假 scenario 餵給腳本,它**必須**擋下來。

### 「非 0」不等於「證據」

舊 commit 上 `import bridge` 炸掉也是非 0 離開碼,長得跟「bug 重現了」
一模一樣。那是**假證據**:它的非 0 來自 scenario 自己壞掉。

所以底座把離開碼分成四個,prove 腳本**兩個都查**(離開碼 + 判決標記):

| 碼 | 標記 | 意思 |
|---|---|---|
| 0 | `SCENARIO_PASSED` | 現象不存在 = 行為正確(head 必須是這個) |
| 1 | `SCENARIO_FAILED` | 現象重現了 = bug 在(base 必須是這個) |
| 2 | `SCENARIO_ERROR` | 跑不起來 —— **不是觀測結果**,一律拒收 |
| 77 | `SCENARIO_SKIPPED` | 這台機器缺東西(沿用 `tests/_requires.py` 的慣例)。跳過不是證據 |

對映方式很直接:`unittest` 的 **failure**(assert 不成立)→ 1,**error**
(任何例外)→ 2。這也是為什麼 scenario 裡的「前提查核」要用 `raise`
而不是 `assert` —— 前提消失了應該報成 2(無效),不能被誤讀成 bug 重現。
`global_seq_resync_storm.py` 就這樣處理「全域序號到底有沒有洞」這個前提。

---

## 怎麼加一個

1. **先找到現象**。不是「某個函式回錯值」,是「使用者看到/沒看到什麼」。
   寫不出一句使用者視角的話,就還不是 scenario。
2. **找到修復那顆 commit**,base = 它的父節點(`<fix>^`)。
   `git log --oneline --all -i --grep=...` 很好用。
3. **寫檔**`scenarios/<現象>.py`:

```python
"""現象:(使用者看到什麼)

根因:(為什麼)
修復:(`<sha>`)
base = `<sha>^`
"""
import _scenario
import unittest


class 現象(unittest.TestCase):
    def setUp(self):
        self.tmux = _scenario.FakeTmux()          # 替身只替外面的世界
        self.addCleanup(self.tmux.stop)
        self.tmux.add_session("demo")
        _scenario.register_cc_session("demo", _scenario.tmpdir("w-"))
        self.http = _scenario.http_client()        # 打真的 HTTP 路由
        self.addCleanup(self.http.close)

    def test_使用者視角的那句話(self):
        res = self.http.post("/app/v2/sessions/claude_code:demo/input",
                             headers=_scenario.auth_header(),
                             json={"text": "hello"})
        self.assertEqual(res.status_code, 200,
                         "(assert 訊息要寫使用者看到什麼,不是寫 expected/actual)")


if __name__ == "__main__":
    _scenario.main()
```

4. **跑 prove**。沒有 `PROVEN` 就還沒完工。
5. 把 base sha 記進上面那張清單。

### 三條紀律(不是風格,是跨 commit 跑得動的前提)

**① 只碰產品對外的介面** —— HTTP 路由、CLI。

不准 `import bridge` 然後斷言內部函式。`bridge._cc_survey_overlay` 在 base
上**不存在**,於是 scenario 在 base 上拿到 `AttributeError` → 離開碼 2 →
腳本判成無效證據。這不是底座在為難你,這是它在告訴你:你驗的是實作細節,
不是行為。

**② 狀態一律由產品自己造**

要有卡片就打 API 讓它長出卡片;不要手刻卡片 JSON、不要寫 `canonical.db`。
schema 會在 base/head 之間漂移 —— 手刻的狀態在 base 上不是「舊行為」,是
**無效輸入**,於是你會得到一個紅燈,但紅的原因跟那個 bug 毫無關係。
(這條設計是照抄 t3code 的 `prove-scenario.ts`,是整套制度最關鍵的一條。)

實例:`cc_media_attachment_missing` 要一張「CC 說檔案在這」的卡,作法是寫一份
**Claude Code 自己的 transcript jsonl**(外部輸入)+ 在工作目錄放真的檔案,
然後**只打** `GET /app/v2/sessions/{id}/cards` 讀回來。卡是產品自己長的,
所以 base 上跑的是 base 的媒體管線。

`global_seq_resync_storm` 更進一步:餵給 Console 的那串全域序號不是手編的
數字,是先用 API 建兩條 session 交錯送訊息,再從 `durable_seq` 讀回來的
**真實** event_log 序號(實測 lane-a 拿到 2、5、7 —— 3/4/6 被另一條佔走)。
scenario 會先確認它真的有洞,沒洞就報 `SCENARIO_ERROR`。

**③ 替身只替外面的世界**

tmux / node / Claude Code 的 transcript 檔 —— 這些是產品的**外部依賴**,
它們的介面(CLI 參數、檔案格式)跨版本穩定,所以同一份替身在 base 上一樣
接得上。產品自己的狀態不准替。

`scenarios/_faketmux.py` 是一隻**真的可執行**的假 tmux(不是 monkeypatch
`_tmux_run`):bridge 從 `create_subprocess_exec` 往下的每一行照跑 ——
參數組裝、rc 判讀、逾時守衛全都在驗證範圍內。

明文例外只有兩個,都寫在底座的 docstring 裡:
- `bridge.TMUX_BIN`(「tmux 執行檔在哪」這一個字串。它是模組常數、沒有 env
  把手,而加把手只會存在於 head);
- `scenarios/_scenario.py::register_cc_session` 寫 ccsess `sessions.conf`
  —— 使用者手寫的純文字常駐名單,是**環境設定**而不是產品狀態
  (`tests/_isolation.py` 早就把 `CCSESS_CONF` 導到 tmp)。

---

## 與一般單元測試的分工

兩者都要有,但問的問題不同。

| | `tests/test_*.py` | `scenarios/*.py` |
|---|---|---|
| 問題 | 這個零件的規則對不對 | 使用者會不會踩到那個現象 |
| 層級 | 直接呼叫內部函式 | 只走對外介面(HTTP/CLI) |
| 狀態 | fixture、mock、字串樣本都可以 | 一律由產品自己造 |
| 數量 | 多,細,快 | 少,粗,慢(秒級) |
| 跨 commit | 不保證(內部函式會改名/消失) | **必須**(這是存在的理由) |
| 什麼時候寫 | 每次改規則 | 每次有**使用者踩到的 bug** |
| 失敗時你知道 | 哪一條規則破了 | 哪一個使用者體驗壞了 |

具體對照同一個 bug:

- `tests/test_cc_survey_overlay.py` 釘的是 `_cc_survey_overlay()` 的判定規則
  —— 包含那條最重要的**誤觸防護**(對話裡原封不動聊到那句英文時不准誤判,
  誤判的代價是把 `0` 打進使用者的草稿)。這種邊界只有單元測試驗得細。
- `scenarios/cc_input_not_accepted_when_survey_modal.py` 釘的是
  「問卷擋著的時候,我送的訊息到不到得了 CC」—— 它不知道也不在乎
  `_cc_survey_overlay` 存不存在,所以 `4c87829` 之後不管內部怎麼重構、
  那支函式改名還是被拆掉,這條線照樣有效。

**不要把 scenario 寫成第二套單元測試。**scenario 貴(要起 worktree、要跑兩次),
它的價值在「跨 commit 仍然成立」與「說的是人話」。規則細節留給
`tests/`;現象留給 `scenarios/`。

反過來也成立:**一般單元測試不受這套證明制約束**。它們本來就該直接戳內部
函式,也本來就不需要在舊 commit 上跑得動。

---

## 制度本身的測試

`tests/test_prove_scenario.py` —— 四條,跟著正常測試套跑:

1. **永遠綠的假 scenario 必須被擋下來**(本檔主角;並且第二步根本不該開跑);
2. 在 base 上自己炸掉的 scenario 必須被擋下來(非 0 ≠ 證據);
3. 真的 base 紅 / head 綠 → 必須印 `PROVEN`(否則上面兩條只是「什麼都擋」);
4. 不存在的 scenario 名 → 用法錯誤(離開碼 2)。
