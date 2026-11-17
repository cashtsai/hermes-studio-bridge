"""媒體掛點對齊矩陣 —— postmortem 0003 的閘門。

## 為什麼要有這支

`docs/POSTMORTEMS/0003-cc-media-pipeline-only-wired-to-codex.md`:
agent 產出的影片/報告在 Pocket 上只是一行裸路徑,點不開。根因不是「實作錯了」
而是**「沒有被呼叫」** —— `attach_captured_media` 全 repo 只有 codex 的 seed
呼叫一次,CC/cc2 那條線從來沒接上,而且

  · 覆蓋率/型別/lint 對「這支函式應該有 8 個呼叫點但只有 1 個」**無話可說**;
  · 唯一宣稱它存在的東西是 `docs/APP_BRIDGE_CONTRACT.md` 的手維護表格,
    而那張表當時連 codex 都寫著 ✅(實際上 codex 連 cwd 分支都沒有);
  · `docs/ADDING_A_PROVIDER.md` 的檢核項是 markdown checkbox,沒勾沒後果。

我們當時**有**對齊矩陣制度(`tests/test_v2_session_ops.py` 逼
`_V2_SESSION_OPS` 的每個格子表態),但那張表只收 session 操作。
媒體掛點不在任何表上,所以 8 家裡少接 6 家是一個沒有任何東西在看的格子。

這一支把那張散文表格變成會紅的閘:**每家 provider 的兩個掛點要嘛有,
要嘛寫進 `_MEDIA_HOOK_KNOWN_GAPS` 並附一句理由。不准安靜地缺。**

## 兩個掛點(照 docs/ADDING_A_PROVIDER.md 的 "Card attachments")

1. **turn-end `_schedule_media_attach`** —— 沒有的話,檔案只會在**下一次**冷載
   才變可點,讀起來像「有時候才會動」。
2. **`_media_session_cwd` 的 provider 分支** —— 沒有的話 `attach_roots`
   塌成只有 `/tmp`,agent 寫進專案目錄的東西全部留在裸路徑狀態,
   **而且是安靜地失敗**(`media_cards_attached cards=0`)。

## 為什麼掃源碼而不是 import 來看

這兩個掛點都不是資料結構,是**呼叫點**:一個在 turn runner 的 `finally` 裡,
一個是 `if prov == "x"` 分支。沒有任何執行期物件可以列舉它們,
所以只能掃源碼 —— 而掃源碼的守門會因為 regex 配不到而假通過,
所以每個掃描點都先過語料下限哨兵(`scripts/sentinel/corpus_sentinel.py`)。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import re
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

from sentinel import corpus_sentinel  # noqa: E402

#: 這張表該涵蓋的 provider 全集。與 tests/test_v2_session_ops.py 的
#: `_ALL_PROVIDERS` 刻意各留一份:那張表管 session 操作,這張管媒體掛點,
#: 兩者的 provider 集合**現在**一樣但沒有道理永遠一樣
#: (例如未來的純文字 provider 可能上 session 操作而不上媒體)。
#: 一份共用常數會讓「某家故意不上媒體」這個決定無處表達。
_ALL_PROVIDERS = ("claude_code", "codex", "cc2", "gemini", "openclaw", "hermes",
                  "grok", "opencode")

#: 已知且有理由的缺格。沒理由的缺格是債,不是決定。
#: 格式:(hook, provider): "理由"
_MEDIA_HOOK_KNOWN_GAPS = {
    # ── openclaw:生命週期與檔案都在 gateway 端 ──────────────────────────────
    ("turn_end_attach", "openclaw"):
        "OpenClaw 端自己管回合與檔案;bridge 只讀事件流,沒有本機落地的產出",
    ("session_cwd", "openclaw"):
        "session 跑在 OpenClaw gateway 的機器上,本機沒有對應工作目錄 —— "
        "給一個 cwd 只會讓 attach_roots 授權到不相干的本機路徑",

    # ── hermes:persona 對話不是 code agent ────────────────────────────────
    ("turn_end_attach", "hermes"):
        "persona 走 canonical store 的訊息分頁,不產生本機檔案產出",
    ("session_cwd", "hermes"):
        "persona 沒有工作目錄概念(TG 式常駐),沒有可授權的根",

    # ── claude_code / cc2:掛點在,但不是這兩條 regex 認得的具名寫法 ────────
    # 這兩筆**不是**能力缺口 —— 它們有掛點,只是形狀不同,見下面兩支
    # test_*_hooked_through_its_own_shape 各自釘住真正的那一行。
    # 寫在這裡而不是放寬 regex,是因為放寬 regex 會讓它配到更多東西、
    # 連「真的缺」的那天也配得到。
    ("turn_end_attach", "claude_code"):
        "掛在 `_cc_digest_lines` 內(seed 與 live tail 共用一支),"
        "呼叫形狀是 `_schedule_media_attach(store, sid)` —— "
        "由 test_claude_code_hooked_through_digest_lines 釘住",
    ("turn_end_attach", "cc2"):
        "cc2 的回合收尾在 cc_sdk 裡,掛點是 `cc_sdk.configure(media_attach=…)` "
        "的注入 —— 由 test_cc2_hooked_through_sdk_injection 釘住",
}
# (`session_cwd` / `claude_code` 刻意**不在**這張表上:它的分支寫成
#  `prov in ("claude_code", "cc")`,而 `_cwd_branch_providers()` 會把元組裡的
#  每個名字都算成覆蓋 —— 真的有,就不該假裝是缺口。
#  那個元組形狀本身由 test_claude_code_cwd_branch_covers_bare_names 釘住。)


def _src(name: str) -> str:
    with open(os.path.join(_ROOT, name), encoding="utf-8") as f:
        return f.read()


_BRIDGE = _src("bridge.py")


def _media_cwd_body() -> str:
    """`_media_session_cwd()` 的函式本體(到下一個模組層 `def` 為止)。

    同 tests/test_event_taxonomy_b1.py 切 `_event_append` 的手法:
    掃分支只能在這支函式裡掃,不然 `if prov == "..."` 在 bridge.py 其他地方
    還有十幾處(推播、能力回報…),會把不相干的分支算成覆蓋。
    """
    i = _BRIDGE.index("def _media_session_cwd(")
    return _BRIDGE[i:_BRIDGE.index("\ndef ", i + 10)]


def _cwd_branch_providers() -> set:
    body = _media_cwd_body()
    tokens = re.findall(r"prov (?:==|in) (\(.*?\)|\"[a-z_0-9]+\")", body)
    corpus_sentinel.require("media_attach.cwd_branches", len(tokens),
                            detail="(_media_session_cwd 的分支改寫法了?"
                                   "掃到 0 個分支 ⇒ 0 個覆蓋 ⇒ 這支會"
                                   "『全部都缺』地紅,掃到 1 個才是危險的那種綠)")
    provs = set()
    for tok in tokens:
        provs.update(re.findall(r"[a-z_0-9]+", tok))
    return provs


def _turn_end_providers() -> set:
    """turn-end 具名呼叫點:`_schedule_media_attach(d.store, _store_media_sid(d.store, f"<prov>:…`"""
    provs = set(re.findall(
        r"_schedule_media_attach\(d\.store, _store_media_sid\("
        r"d\.store, f\"([a-z_0-9]+):", _BRIDGE))
    corpus_sentinel.require("media_attach.turn_end_sites", len(provs),
                            detail="(turn runner 的 finally 掛點改寫法了?)")
    return provs


class MediaHookMatrixCoverageTest(unittest.TestCase):
    """矩陣完整性:每個 (hook, provider) 要嘛有掛點,要嘛有理由。"""

    def test_every_cell_is_accounted_for(self):
        covered = {
            "turn_end_attach": _turn_end_providers(),
            "session_cwd": _cwd_branch_providers(),
        }
        corpus_sentinel.require(
            "media_attach.cells", len(covered) * len(_ALL_PROVIDERS),
            detail="(是 hook 少了還是 _ALL_PROVIDERS 少了?)")
        missing = []
        for hook, provs in covered.items():
            for prov in _ALL_PROVIDERS:
                if prov not in provs and (hook, prov) not in _MEDIA_HOOK_KNOWN_GAPS:
                    missing.append((hook, prov))
        self.assertEqual(
            missing, [],
            f"這些媒體掛點沒有實作也沒有理由:{missing}。\n"
            "  → 補掛點(抄 _gm_run_turn / _gk_run_turn / _oce_run_turn,三家一模一樣),"
            "或在 _MEDIA_HOOK_KNOWN_GAPS 寫明為什麼不做。\n"
            "  → 缺 turn-end 掛點 = 檔案只在下一次冷載才變可點(「有時候才會動」);"
            "缺 cwd 分支 = attach_roots 塌成只有 /tmp,**安靜地** cards=0。\n"
            "  → 見 docs/ADDING_A_PROVIDER.md 的 \"Card attachments\" "
            "與 docs/POSTMORTEMS/0003-cc-media-pipeline-only-wired-to-codex.md")

    def test_no_stale_gap_entries(self):
        """掛點補上了就把 KNOWN_GAPS 那筆刪掉 —— 留著會誤導下一個人。

        例外:claude_code / cc2 那三筆是「形狀不同」而非「缺」,
        它們的 provider 名字**本來就不會**出現在那兩條 regex 的結果裡,
        所以不會被判成 stale。真的被判成 stale,代表有人放寬了 regex ——
        那是一件要被看見的事。
        """
        covered = {
            "turn_end_attach": _turn_end_providers(),
            "session_cwd": _cwd_branch_providers(),
        }
        stale = [(hook, prov) for (hook, prov) in _MEDIA_HOOK_KNOWN_GAPS
                 if prov in covered.get(hook, set())]
        self.assertEqual(
            stale, [],
            f"這些缺口已經被掃到了,KNOWN_GAPS 裡的理由該刪:{stale}。"
            "(如果是因為放寬了 regex 才掃到,先想清楚放寬之後"
            "『真的缺』的那天還紅不紅)")


class HookShapesArePinnedTest(unittest.TestCase):
    """三個「形狀不同所以 regex 配不到」的掛點,各自釘住真正的那一行。

    KNOWN_GAPS 裡寫「它其實有,只是形狀不同」是一句**沒有證據的宣稱** ——
    除非有東西釘住那個形狀。這三支就是證據;掛點被拿掉時它們會紅。
    """

    def test_claude_code_hooked_through_digest_lines(self):
        i = _BRIDGE.index("def _cc_digest_lines(")
        body = _BRIDGE[i:_BRIDGE.index("\ndef ", i + 10)]
        self.assertIn("_schedule_media_attach(store, sid)", body,
                      "tmux CC 的媒體掛點不見了 —— 這正是 postmortem 0003 "
                      "那隻 bug 的原狀:CC 產出的檔案在 Pocket 只是裸路徑")

    def test_cc2_hooked_through_sdk_injection(self):
        self.assertRegex(
            _BRIDGE, r"media_attach\s*=\s*_schedule_media_attach",
            "cc2 的媒體掛點(cc_sdk.configure 的注入)不見了")

    def test_claude_code_cwd_branch_covers_bare_names(self):
        body = _media_cwd_body()
        self.assertIn('prov in ("claude_code", "cc")', body,
                      "CC 的 cwd 分支不見了 —— attach_roots 會塌成只有 /tmp")
        self.assertIn("if not rest:", body,
                      "裸 CC session 名(無 provider 前綴)的 cwd 分支不見了 —— "
                      "舊 app 與 console 兩條路徑都還會送裸名")


class KnownGapsAreHonestTest(unittest.TestCase):
    """缺格的理由本身也要過檢查 —— 空白理由等於沒有理由。"""

    def test_every_gap_has_a_real_reason(self):
        thin = [k for k, why in _MEDIA_HOOK_KNOWN_GAPS.items()
                if len(str(why).strip()) < 12]
        self.assertEqual(thin, [], f"這些缺格的理由太短,寫清楚:{thin}")

    def test_gap_keys_are_in_the_matrix(self):
        """KNOWN_GAPS 不准放矩陣外的格子 —— 打錯字的話缺口會安靜地被豁免。"""
        bad = [(hook, prov) for (hook, prov) in _MEDIA_HOOK_KNOWN_GAPS
               if hook not in ("turn_end_attach", "session_cwd")
               or prov not in _ALL_PROVIDERS]
        self.assertEqual(bad, [], f"KNOWN_GAPS 有矩陣外的 key(打錯字?):{bad}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
