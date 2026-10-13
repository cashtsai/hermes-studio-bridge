"""人格工具權限的自動核准:永不挑「永久」、認不出就拒絕。

2026-10-12 上架前稽核查到的最嚴重一條:Hermes 人格的 `session/request_permission`
是**自動同意**的,而產品文案賣的是「在它卡住時放行」「所有 agent 的權限請求進
同一個收件匣」。

查下去發現它**不是無聲**(對話裡有「🔐 自動允許 X」的步驟註記),但原本的挑選
邏輯有兩個危險:

  1. 關鍵字含 `always` → agent 給 ["Allow always","Allow once","Reject"] 時
     選中的是 **Allow always**,授出的是**永久**權限;
  2. 全都比不到就 `opts[0]` 盲選 —— 選項順序由 agent 決定,等於替使用者按下
     一個不知道是什麼的選項。

這刀**不改成人工閘門**(那是行為變更,人格已經這樣出貨數月;機主 2026-10-12
指示上架前不堆新的),只把危險的部分拿掉並讓拒絕看得見。

釘住的事:
  ‧ 有 kind 就用 kind(`allow_once`),比 name 可靠
  ‧ **永不**挑任何帶 always/all/永久 的選項 —— 即使那是唯一的「允許」
  ‧ 認不出 → 回 None → 呼叫端 fail-closed 回 cancelled,**不是**盲選
  ‧ 畸形 options(非 dict、缺 optionId)不得當成可用選項
"""
import _isolation  # noqa: F401
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="acp-perm-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from acp_client import _pick_allow_once  # noqa: E402


class PickAllowOnceTest(unittest.TestCase):

    def test_prefers_kind_allow_once(self):
        opts = [{"optionId": "A", "name": "Allow always", "kind": "allow_always"},
                {"optionId": "B", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "C", "name": "Reject", "kind": "reject_once"}]
        self.assertEqual(_pick_allow_once(opts), "B")

    def test_never_picks_always_even_when_listed_first(self):
        """**這是修前的實害**:舊邏輯會選中 Allow always(授出永久權限)。"""
        opts = [{"optionId": "A", "name": "Allow always"},
                {"optionId": "B", "name": "Allow once"},
                {"optionId": "C", "name": "Reject"}]
        self.assertEqual(_pick_allow_once(opts), "B")

    def test_never_picks_always_even_if_it_is_the_only_allow(self):
        """只有「永久允許」可選時,**寧可拒絕**也不要自動授出永久權限。"""
        opts = [{"optionId": "A", "name": "Allow always"},
                {"optionId": "C", "name": "Reject"}]
        self.assertIsNone(_pick_allow_once(opts))

    def test_kind_always_is_excluded_too(self):
        opts = [{"optionId": "A", "name": "好", "kind": "allow_always"}]
        self.assertIsNone(_pick_allow_once(opts))

    def test_chinese_allow_names(self):
        self.assertEqual(
            _pick_allow_once([{"optionId": "Z", "name": "允許這一次"}]), "Z")
        self.assertIsNone(
            _pick_allow_once([{"optionId": "Z", "name": "永久允許"}]))

    def test_no_blind_first_option_fallback(self):
        """修前是 `opts[0]` 盲選 —— 選項順序由 agent 決定。現在要回 None。"""
        opts = [{"optionId": "A", "name": "Delete everything"},
                {"optionId": "B", "name": "Cancel"}]
        self.assertIsNone(_pick_allow_once(opts))

    def test_never_auto_tokens_do_not_eat_allow(self):
        """防止再犯:第一版 _NEVER_AUTO 放了裸 "all",子字串命中 "allow",
        把 "Allow once" 也擋掉了。任何新增的排除詞都不准吃掉正常的允許。"""
        for name in ("Allow once", "Allow this time", "允許這一次", "Yes, allow"):
            self.assertIsNotNone(
                _pick_allow_once([{"optionId": "ok", "name": name}]),
                f"{name!r} 必須仍被認成「允許一次」")

    def test_empty_and_malformed(self):
        self.assertIsNone(_pick_allow_once([]))
        self.assertIsNone(_pick_allow_once(None))
        self.assertIsNone(_pick_allow_once(["not-a-dict"]))
        self.assertIsNone(_pick_allow_once([{"name": "Allow once"}]))  # 缺 optionId
        self.assertIsNone(_pick_allow_once([{"optionId": "", "name": "Allow"}]))

    def test_reject_is_never_mistaken_for_allow(self):
        for name in ("Reject", "No", "Deny", "拒絕", "取消"):
            self.assertIsNone(_pick_allow_once([{"optionId": "X", "name": name}]),
                              f"{name} 不可被當成允許")


class BridgeConsumesDenialTest(unittest.TestCase):
    """fail-closed 的拒絕必須看得見 —— 工具沒跑,回覆可能因此不完整。"""

    def test_perm_denied_renders_in_transcript(self):
        import bridge
        line = bridge._fmt_item("perm_denied", "WriteFile")
        self.assertIsNotNone(line, "perm_denied 必須有 transcript 行")
        self.assertIn("已拒絕", line)
        self.assertIn("WriteFile", line)
        self.assertIn("沒有執行", line, "要講明工具沒跑,回覆可能不完整")

    def test_perm_allowed_still_renders(self):
        """既有的自動允許行不可被這刀改壞。"""
        import bridge
        line = bridge._fmt_item("perm", "Bash")
        self.assertIn("自動允許", line)
        self.assertIn("Bash", line)

    def test_perm_denied_branch_exists_in_source(self):
        """最低保證:兩個消費點都在(渲染函式名可能變,分支不該消失)。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "bridge.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count('kind == "perm_denied"'), 2,
                         "steps 註記與 transcript 行兩處都要消費 perm_denied")


if __name__ == "__main__":
    unittest.main(verbosity=2)
