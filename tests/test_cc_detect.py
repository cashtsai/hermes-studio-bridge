"""cc_detect —— manifest 偵測引擎測試。

守三件事:
1. 引擎語意與 herdr 對齊(優先序、region、邏輯門、OSC title 訊號源)。
2. 舊 bridge 忙碌判讀的**嚴格超集**:凡舊表達式(`_CC_BUSY_RE` or
   "esc to interrupt")判 busy 的 pane,新引擎必為 working(parity suite)。
3. 覆寫檔:載入、mtime 熱更新、壞檔退回內建且不 crash(輪詢路徑的鐵則)。
"""
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cc_detect


# 舊 bridge 的忙碌表達式(bridge.py 五處 inline 的原樣重演)—— parity 基準。
_OLD_BUSY_RE = re.compile(r"\((?:\d+m\s*)?\d+(?:\.\d+)?s\s*·.*tokens", re.IGNORECASE)


def _old_busy(pane: str) -> bool:
    return bool(_OLD_BUSY_RE.search(pane)) or ("esc to interrupt" in pane.lower())


RULE = "─" * 40

IDLE_PROMPT_BOX = f"""some earlier output
{RULE}
 ❯
{RULE}
  ? for shortcuts
"""

BASH_PERMISSION_MENU = """  Bash command
  ⎿  rm -rf build/

Do you want to proceed?
❯ 1. Yes
  2. No, and tell Claude what to do differently (esc)
"""

GENERIC_PERMISSION_MENU = f"""  Edit file
{RULE}
Do you want to proceed?
❯ 1. Yes
  2. Yes, allow all edits during this session
  3. No, and tell Claude what to do differently (esc)
Esc to cancel
"""

LIVE_BLOCKED_FORM = f"""  Question time
{RULE}
Which option?
❯ 1. First
  2. Second

Enter to select · ↑/↓ to navigate · Esc to cancel
"""

BUSY_SPINNER_PANE = """· Fermenting… (1m 51s · ↓ 6.5k tokens)
"""

BUSY_AND_MENU_PANE = """· Crunching… (12s · ↑ 2.1k tokens)

Do you want to proceed?
❯ 1. Yes
  2. No, and tell Claude what to do differently (esc)
Esc to cancel
"""


class EngineTests(unittest.TestCase):
    """引擎語意:優先序、region、邏輯門。"""

    def test_priority_busy_outranks_blocked(self):
        # 舊語意:busy 優先於選單判讀(_cc_prompt 忙碌時回 None)。
        # manifest 裡 spinner(1060)壓過 blocked(980/850)必須保持這點。
        r = cc_detect.classify(BUSY_AND_MENU_PANE)
        self.assertEqual(r["state"], "working")
        self.assertEqual(r["rule"], "pane_busy_spinner")

    def test_priority_ties_keep_manifest_order(self):
        rules = cc_detect.compile_manifest({"rules": [
            {"id": "a", "state": "working", "priority": 10, "contains": ["x"]},
            {"id": "b", "state": "blocked", "priority": 10, "contains": ["x"]},
        ]})
        self.assertEqual([r["id"] for r in rules], ["a", "b"])

    def test_idle_fallback_when_nothing_matches(self):
        r = cc_detect.classify("hello world\nnothing special here")
        self.assertEqual(r["state"], "idle")
        self.assertEqual(r["rule"], cc_detect.DEFAULT_IDLE_FALLBACK)

    def test_prompt_box_idle(self):
        r = cc_detect.classify(IDLE_PROMPT_BOX)
        self.assertEqual(r["state"], "idle")
        self.assertEqual(r["rule"], "live_prompt_box")

    def test_prompt_box_not_gate_rejects_menu_layout(self):
        # 框裡其實是選單(esc to cancel 在場)→ not 門要把 idle 擋掉。
        pane = f"""output
{RULE}
 ❯ 1. Yes
Enter to select · ↑/↓ to navigate · Esc to cancel
{RULE}
"""
        r = cc_detect.classify(pane)
        self.assertNotEqual(r["rule"], "live_prompt_box")

    def test_blocked_bash_permission_prompt(self):
        r = cc_detect.classify(BASH_PERMISSION_MENU)
        self.assertEqual(r["state"], "blocked")
        self.assertEqual(r["rule"], "bash_permission_prompt")

    def test_blocked_generic_permission_prompt(self):
        r = cc_detect.classify(GENERIC_PERMISSION_MENU)
        self.assertEqual(r["state"], "blocked")

    def test_blocked_live_form_esc_to_cancel_plus_confirm(self):
        r = cc_detect.classify(LIVE_BLOCKED_FORM)
        self.assertEqual(r["state"], "blocked")
        self.assertEqual(r["rule"], "live_blocked_form")

    def test_osc_title_spinner_working_even_when_pane_idle(self):
        # braille spinner 在標題、pane 看起來待命 → 仍是 working(1100 最高)。
        for title in ("⠧ compacting the repo", "◐ thinking hard"):
            r = cc_detect.classify(IDLE_PROMPT_BOX, title)
            self.assertEqual(r["state"], "working", title)
            self.assertEqual(r["rule"], "osc_title_working", title)

    def test_osc_title_idle_marker(self):
        r = cc_detect.classify("plain output, no prompt box", "✳ done thing")
        self.assertEqual(r["state"], "idle")
        self.assertEqual(r["rule"], "osc_title_idle")

    def test_transcript_viewer_unknown(self):
        pane = "…lots of transcript…\nShowing detailed transcript · ctrl+o to toggle"
        r = cc_detect.classify(pane)
        self.assertEqual(r["state"], "unknown")
        self.assertEqual(r["rule"], "transcript_viewer")

    def test_btw_overlay_line_regex_in_bottom_region(self):
        pane = "old scrollback\nmore\n  /btw running things\nprogress...\n  esc to close"
        r = cc_detect.classify(pane)
        self.assertEqual(r["state"], "working")
        self.assertEqual(r["rule"], "btw_overlay_working")

    def test_region_bottom_non_empty_lines(self):
        pane = "a\nb\nc\n\n\nd\ne\n"
        # 由下數 2 條非空行(d、e)起、含中間空行、到畫面底。
        self.assertEqual(cc_detect._bottom_non_empty_lines(pane, 2), "d\ne\n")
        self.assertEqual(cc_detect._bottom_non_empty_lines(pane, 3), "c\n\n\nd\ne\n")
        self.assertEqual(cc_detect._bottom_non_empty_lines("\n\n", 2), "")

    def test_region_top_non_empty_lines(self):
        pane = "\na\nb\n\nc\nd"
        self.assertEqual(cc_detect._top_non_empty_lines(pane, 2), "\na\nb")
        self.assertEqual(cc_detect._top_non_empty_lines("\n \n", 1), "")

    def test_region_after_last_horizontal_rule(self):
        pane = f"before\n{RULE}\nmiddle\n{RULE}\nafter one\nafter two"
        self.assertEqual(cc_detect._after_last_horizontal_rule(pane),
                         "after one\nafter two")
        self.assertEqual(cc_detect._after_last_horizontal_rule("no rules here"),
                         "no rules here")

    def test_region_prompt_box_body(self):
        self.assertEqual(cc_detect._prompt_box_body(IDLE_PROMPT_BOX).strip(), "❯")
        self.assertEqual(cc_detect._prompt_box_body("no box at all"), "")

    def test_gates_all_any_not_nested(self):
        rules = cc_detect.compile_manifest({"rules": [{
            "id": "g", "state": "blocked", "priority": 1,
            "contains": ["base"],
            "all": [{"contains": ["need"]}],
            "any": [{"contains": ["opt-a"]}, {"contains": ["opt-b"]}],
            "not": [{"contains": ["veto"]}],
        }]})
        gate = rules[0]["gate"]

        def m(text):
            return cc_detect._gate_matches(gate, text, text.lower())

        self.assertTrue(m("base need opt-a"))
        self.assertTrue(m("base need opt-b"))
        self.assertFalse(m("base need"))            # any 全空手
        self.assertFalse(m("base opt-a"))           # all 沒過
        self.assertFalse(m("base need opt-a veto"))  # not 否決
        self.assertTrue(m("BASE NEED OPT-A"))       # contains 不分大小寫

    def test_never_crashes_on_garbage(self):
        for junk in ("", "\x00\x1b[2J", None):
            r = cc_detect.classify(junk)
            self.assertIn(r["state"], cc_detect.STATES)


class ParityTests(unittest.TestCase):
    """凡舊表達式判 busy → 新引擎必 working;代表性負例不誤報。"""

    BUSY_FIXTURES = [
        "· Fermenting… (1m 51s · ↓ 6.5k tokens)",
        "✻ Deliberating… (3s · ↑ 100 tokens · esc to interrupt)",
        "(12.5s · 2.3k tokens · thinking)",
        "some output\n  esc to interrupt",
        "Esc to Interrupt",                        # 大小寫
        BUSY_SPINNER_PANE,
        BUSY_AND_MENU_PANE,
    ]

    NOT_BUSY_FIXTURES = [
        "? for shortcuts",
        IDLE_PROMPT_BOX,
        BASH_PERMISSION_MENU,
        GENERIC_PERMISSION_MENU,
        "plain conversation text",
        "",
    ]

    def test_old_busy_implies_working(self):
        for pane in self.BUSY_FIXTURES:
            self.assertTrue(_old_busy(pane), f"fixture 該是舊 busy:{pane!r}")
            self.assertEqual(cc_detect.classify(pane)["state"], "working",
                             f"parity 破口:{pane!r}")

    def test_negatives_stay_not_working(self):
        for pane in self.NOT_BUSY_FIXTURES:
            self.assertFalse(_old_busy(pane))
            self.assertNotEqual(cc_detect.classify(pane)["state"], "working",
                                f"新引擎誤報 working:{pane!r}")


class OverrideTests(unittest.TestCase):
    """CC_DETECT_MANIFEST 覆寫:載入、按 id 合併、mtime 熱更新、壞檔降級。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "claude.json")
        self._old_env = os.environ.get("CC_DETECT_MANIFEST")
        os.environ["CC_DETECT_MANIFEST"] = self.path
        cc_detect._OVERRIDE_CACHE.clear()

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("CC_DETECT_MANIFEST", None)
        else:
            os.environ["CC_DETECT_MANIFEST"] = self._old_env
        cc_detect._OVERRIDE_CACHE.clear()
        self.tmp.cleanup()

    def _write(self, obj, mtime):
        with open(self.path, "w", encoding="utf-8") as fh:
            if isinstance(obj, str):
                fh.write(obj)
            else:
                json.dump(obj, fh, ensure_ascii=False)
        os.utime(self.path, (mtime, mtime))

    def test_missing_file_falls_back_to_embedded(self):
        r = cc_detect.classify(BUSY_SPINNER_PANE)
        self.assertEqual(r["state"], "working")

    def test_override_adds_rule_and_merges_by_id(self):
        self._write({"rules": [
            # 新規則:自訂 blocked 字樣。
            {"id": "custom_blocker", "state": "blocked", "priority": 2000,
             "contains": ["magic word"]},
            # 覆蓋內建:esc to interrupt 改判 blocked(證明同 id 整條取代)。
            {"id": "pane_esc_to_interrupt", "state": "blocked", "priority": 1050,
             "contains": ["esc to interrupt"]},
            # 刪掉內建 spinner 規則。
            {"id": "pane_busy_spinner", "remove": True},
        ]}, mtime=1000)
        self.assertEqual(cc_detect.classify("hey magic word here"),
                         {"state": "blocked", "rule": "custom_blocker",
                          "priority": 2000})
        self.assertEqual(cc_detect.classify("esc to interrupt")["state"], "blocked")
        self.assertEqual(cc_detect.classify(BUSY_SPINNER_PANE)["state"], "idle")

    def test_replace_true_uses_only_override_rules(self):
        self._write({"replace": True, "rules": [
            {"id": "only", "state": "working", "priority": 1,
             "contains": ["zzz"]},
        ]}, mtime=1000)
        self.assertEqual(cc_detect.classify("zzz")["rule"], "only")
        # 內建規則全下架:spinner 不再命中 → fallback idle。
        self.assertEqual(cc_detect.classify(BUSY_SPINNER_PANE)["rule"],
                         cc_detect.DEFAULT_IDLE_FALLBACK)

    def test_hot_reload_by_mtime(self):
        self._write({"rules": [{"id": "hot", "state": "blocked", "priority": 3000,
                                "contains": ["hotword"]}]}, mtime=1000)
        self.assertEqual(cc_detect.classify("hotword")["state"], "blocked")
        # 同 mtime 改內容 → 不重載(便宜的 stat 檢查,herdr 同精神)。
        self._write({"rules": [{"id": "hot", "state": "working", "priority": 3000,
                                "contains": ["hotword"]}]}, mtime=1000)
        self.assertEqual(cc_detect.classify("hotword")["state"], "blocked")
        # mtime 前進 → 熱更新生效。
        os.utime(self.path, (2000, 2000))
        self.assertEqual(cc_detect.classify("hotword")["state"], "working")

    def test_corrupt_file_falls_back_and_keeps_serving(self):
        self._write("{not json!!", mtime=1000)
        r = cc_detect.classify(BUSY_SPINNER_PANE)
        self.assertEqual(r["state"], "working")     # 內建規則照跑
        self.assertEqual(cc_detect.explain("x")["manifest_source"], "embedded")
        # 壞檔已快取(logged)→ 再呼叫也不炸、不重複解析。
        self.assertEqual(cc_detect.classify("esc to interrupt")["state"], "working")
        # 修好 + mtime 前進 → 復活。
        self._write({"rules": [{"id": "fixed", "state": "blocked", "priority": 5000,
                                "contains": ["fixedword"]}]}, mtime=2000)
        self.assertEqual(cc_detect.classify("fixedword")["state"], "blocked")

    def test_bad_regex_in_override_falls_back(self):
        self._write({"rules": [{"id": "bad", "state": "working", "priority": 1,
                                "regex": ["([unclosed"]}]}, mtime=1000)
        self.assertEqual(cc_detect.classify(BUSY_SPINNER_PANE)["state"], "working")

    def test_explain_reports_override_source(self):
        self._write({"rules": [{"id": "src", "state": "blocked", "priority": 4000,
                                "contains": ["srcword"]}]}, mtime=1000)
        ex = cc_detect.explain("srcword")
        self.assertEqual(ex["manifest_source"], self.path)
        self.assertEqual(ex["matched_rule"]["id"], "src")


class ExplainTests(unittest.TestCase):
    def test_explain_matched_rule_and_evidence(self):
        ex = cc_detect.explain(BASH_PERMISSION_MENU)
        self.assertEqual(ex["state"], "blocked")
        self.assertEqual(ex["matched_rule"]["id"], "bash_permission_prompt")
        self.assertTrue(any("Do you want to proceed?" in ln
                            for ln in ex["evidence_lines"]))
        # 每條規則都要出現在 evaluated_rules(可解釋性 = 全量列出)。
        ids = {r["id"] for r in ex["evaluated_rules"]}
        self.assertIn("osc_title_working", ids)
        self.assertIn("live_prompt_box", ids)

    def test_explain_fallback_has_no_matched_rule(self):
        ex = cc_detect.explain("plain text, nothing to see")
        self.assertEqual(ex["state"], "idle")
        self.assertIsNone(ex["matched_rule"])
        self.assertEqual(ex["rule"], cc_detect.DEFAULT_IDLE_FALLBACK)

    def test_explain_uses_title_signal(self):
        ex = cc_detect.explain(IDLE_PROMPT_BOX, "⠋ crunching")
        self.assertEqual(ex["matched_rule"]["id"], "osc_title_working")
        self.assertEqual(ex["evidence_lines"], ["⠋ crunching"])


class BridgeRoutingTests(unittest.TestCase):
    """bridge._cc_pane_busy / _cc_pane_blocked 必須路由到 cc_detect(可 patch)。"""

    @classmethod
    def setUpClass(cls):
        import bridge                       # 慢(整包 app),整類共用一次
        cls.bridge = bridge

    def test_pane_busy_routes_through_cc_detect(self):
        orig = cc_detect.classify
        try:
            cc_detect.classify = lambda pane, title=None: {
                "state": "working", "rule": "patched", "priority": 1}
            self.assertTrue(self.bridge._cc_pane_busy("anything at all"))
            cc_detect.classify = lambda pane, title=None: {
                "state": "idle", "rule": "patched", "priority": 1}
            self.assertFalse(self.bridge._cc_pane_busy(BUSY_SPINNER_PANE))
        finally:
            cc_detect.classify = orig

    def test_pane_blocked_helper_exported(self):
        self.assertTrue(self.bridge._cc_pane_blocked(BASH_PERMISSION_MENU))
        self.assertFalse(self.bridge._cc_pane_blocked(BUSY_SPINNER_PANE))

    def test_pane_busy_parity_through_bridge(self):
        for pane in ParityTests.BUSY_FIXTURES:
            self.assertTrue(self.bridge._cc_pane_busy(pane), repr(pane))
        for pane in ParityTests.NOT_BUSY_FIXTURES:
            self.assertFalse(self.bridge._cc_pane_busy(pane), repr(pane))


if __name__ == "__main__":
    unittest.main(verbosity=2)
