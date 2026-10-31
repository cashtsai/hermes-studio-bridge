"""Gemini 核准選項的形狀與決議(= 當初把入口藏起來的那個理由)。

## 這一檔在修什麼

2026-10-11 把 Gemini 的 UI 入口藏起來,`StudioChatUI.swift` 的註解寫的理由是
「權限請求**只能自動拒**」。之後 bridge 補了完整的核准管線(出卡、180 秒窗口、
`gemini_permission_approved` 事件),看起來已經修好 —— **但端到端還是壞的**,
而且壞得更隱蔽:

  · bridge 把 ACP 原形 `{optionId,name,kind}` 直接存進 canonical approvals
  · app 的 `TerminalApprovalOption` 只認 `{key,label,style}` → 兩個都是空字串
  · 於是畫出**沒有文字的空白按鈕**
  · 按下去送 `{"key": ""}`
  · 決議端只讀 `b["approve"]` → `bool(None)` = False
  · **一律駁回**,而且沒有錯誤:卡片消失、agent 收到 cancelled

從「明講自動拒」變成「使用者按了空白鈕然後被拒」。後者更糟,因為它看起來能用。

## 兩個方向都釘

正規化(給得出可按的按鈕)與決議(按了真的生效)。少任何一半,這條路都是
「看起來能用」—— 而那正是這個功能被藏起來的原因。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="gm-approval-opt-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

_ACP = [
    {"optionId": "o-allow1", "name": "允許這一次", "kind": "allow_once"},
    {"optionId": "o-allowA", "name": "永遠允許", "kind": "allow_always"},
    {"optionId": "o-deny1", "name": "拒絕", "kind": "reject_once"},
]


class NormalizeTest(unittest.TestCase):

    def test_shape_matches_the_contract(self):
        """三個鍵都要在,而且名字要跟其他四家一致 —— app 只認這個形狀。"""
        for o in bridge._gm_normalize_options(_ACP):
            self.assertEqual(set(o), {"key", "label", "style"})

    def test_label_is_never_empty(self):
        """**空 label 就是那顆空白按鈕的來源。**"""
        out = bridge._gm_normalize_options([
            {"optionId": "x", "kind": "allow_once"},          # 沒有 name
            {"optionId": "y"},                                 # 連 kind 都沒有
        ])
        self.assertEqual(len(out), 2)
        for o in out:
            self.assertTrue(o["label"].strip(), f"label 不得為空:{o}")

    def test_allow_and_reject_map_to_primary_and_deny(self):
        out = {o["key"]: o["style"] for o in bridge._gm_normalize_options(_ACP)}
        self.assertEqual(out["o-allow1"], "primary")
        self.assertEqual(out["o-allowA"], "primary")
        self.assertEqual(out["o-deny1"], "deny")

    def test_unknown_kind_is_secondary_not_guessed(self):
        """ACP 之後若加新 kind:可見但不搶眼,**不猜它是放行還是駁回**。"""
        out = bridge._gm_normalize_options(
            [{"optionId": "z", "name": "某個新選項", "kind": "ask_later"}])
        self.assertEqual(out[0]["style"], "secondary")

    def test_options_without_id_are_dropped(self):
        """沒有 optionId 的選項送回 agent 也沒用 —— 不要畫出來讓人按空氣。"""
        out = bridge._gm_normalize_options([
            {"optionId": "", "name": "壞的", "kind": "allow_once"},
            {"name": "也壞的", "kind": "allow_once"},
            "not-a-dict",
            None,
        ])
        self.assertEqual(out, [])


class DecisionTest(unittest.TestCase):

    def test_legacy_approve_flag_still_works(self):
        """審核中心的二元鈕走這條,不能改壞。"""
        self.assertTrue(bridge._gm_decision_is_approve("a", {"approve": True}))
        self.assertFalse(bridge._gm_decision_is_approve("a", {"approve": False}))

    def test_generic_key_words(self):
        for k in ("approve", "allow", "primary"):
            self.assertTrue(bridge._gm_decision_is_approve("a", {"key": k}), k)
        for k in ("deny", "reject", "cancel"):
            self.assertFalse(bridge._gm_decision_is_approve("a", {"key": k}), k)

    def test_option_id_resolves_through_the_stored_options(self):
        """app 核准卡送的是 optionId —— **這條就是原本一律被駁回的那個洞。**"""
        stored = bridge._gm_normalize_options(_ACP)
        with patch.object(bridge, "_approval_get_row",
                          return_value={"options": stored}):
            self.assertTrue(
                bridge._gm_decision_is_approve("aid", {"key": "o-allow1"}))
            self.assertTrue(
                bridge._gm_decision_is_approve("aid", {"key": "o-allowA"}))
            self.assertFalse(
                bridge._gm_decision_is_approve("aid", {"key": "o-deny1"}))

    def test_options_stored_as_json_string_also_resolve(self):
        """canonical 表把 options 存成 JSON 字串時也要認得。"""
        import json
        stored = json.dumps(bridge._gm_normalize_options(_ACP))
        with patch.object(bridge, "_approval_get_row",
                          return_value={"options": stored}):
            self.assertTrue(
                bridge._gm_decision_is_approve("aid", {"key": "o-allow1"}))

    # ── fail-closed:猜錯只能往「少給權限」偏 ──────────────────

    def test_unknown_key_denies(self):
        with patch.object(bridge, "_approval_get_row", return_value=None):
            self.assertFalse(
                bridge._gm_decision_is_approve("aid", {"key": "no-such"}))

    def test_empty_body_denies(self):
        self.assertFalse(bridge._gm_decision_is_approve("aid", {}))
        self.assertFalse(bridge._gm_decision_is_approve("aid", {"key": ""}))

    def test_lookup_failure_denies(self):
        """查表爆掉也要駁回,不能因為我們的錯誤而多給權限。"""
        with patch.object(bridge, "_approval_get_row",
                          side_effect=RuntimeError("db gone")):
            self.assertFalse(
                bridge._gm_decision_is_approve("aid", {"key": "o-allow1"}))


if __name__ == "__main__":
    unittest.main()
