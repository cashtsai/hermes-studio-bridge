# -*- coding: utf-8 -*-
"""CC_TOKEN_STREAM(pane-diff 草稿卡)單元測試。

fixture 是仿真的 CC TUI pane 擷取(tmux capture-pane -p 形狀):歡迎框、
使用者回顯、⏺ 工具塊 + ⎿ 結果、spinner/狀態行、輸入框、快捷鍵列。
鐵律驗證:diff 對不上就跳過(不出垃圾)、工具輸出不進草稿、正典卡
接管草稿 id(同 id rev++ final:true)。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import os
import unittest
from unittest import mock

import bridge
import carddigest


SPINNER_A = "✻ Deliberating… (12s · ↓ 0.4k tokens · esc to interrupt)"
SPINNER_B = "✽ Pondering… (14s · ↓ 0.6k tokens · esc to interrupt)"

WELCOME = (
    "╭───────────────────────────────╮\n"
    "│ ✻ Claude Code v2.0.44         │\n"
    "╰───────────────────────────────╯\n"
)

INPUT_BOX = (
    "╭──────────────────────────────────────────────╮\n"
    "│ >                                            │\n"
    "╰──────────────────────────────────────────────╯\n"
    "  ? for shortcuts\n"
)


def pane(transcript: str, spinner: str = SPINNER_A) -> str:
    """組一張仿真 pane:transcript + spinner 狀態行 + 輸入框 + 快捷列。"""
    return f"{transcript}\n\n{spinner}\n\n{INPUT_BOX}"


T0 = (
    WELCOME + "\n"
    "> 幫我看一下 bridge 的部署腳本\n\n"
    "⏺ Bash(ls deploy/)\n"
    "  ⎿  deploy.sh\n"
    "     rollback.sh"
)
T1 = T0 + "\n\n⏺ 部署腳本有兩支:deploy.sh 負責"
T2 = T1 + "主要流程,"
T3 = T2 + "\n  rollback.sh 負責回滾。"
T4 = T3 + (
    "\n\n⏺ Bash(cat deploy/deploy.sh)\n"
    "  ⎿  #!/bin/zsh\n"
    "     set -euo pipefail"
)
T5 = T4 + "\n\n⏺ 結論:可以直接跑。"


class FlagTests(unittest.TestCase):
    def test_default_off(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CC_TOKEN_STREAM", None)
            self.assertFalse(bridge._cc_token_stream_enabled())

    def test_on_values(self):
        for v in ("1", "true", "yes", "on"):
            with mock.patch.dict(os.environ, {"CC_TOKEN_STREAM": v}):
                self.assertTrue(bridge._cc_token_stream_enabled())

    def test_off_values(self):
        for v in ("0", "false", "", "off"):
            with mock.patch.dict(os.environ, {"CC_TOKEN_STREAM": v}):
                self.assertFalse(bridge._cc_token_stream_enabled())


class ContentStripTests(unittest.TestCase):
    def test_strips_input_box_and_spinner(self):
        c = bridge._cc_stream_content(pane(T0))
        self.assertIsNotNone(c)
        self.assertIn("deploy.sh", c)
        self.assertNotIn("? for shortcuts", c)
        self.assertNotIn("Deliberating", c)
        self.assertNotIn("esc to interrupt", c)

    def test_spinner_variation_is_invisible(self):
        """spinner 每張 capture 都在變(字樣/秒數/token 數)——剝乾淨後
        兩張 content 必須相等,否則 diff 永遠對不上。"""
        self.assertEqual(bridge._cc_stream_content(pane(T0, SPINNER_A)),
                         bridge._cc_stream_content(pane(T0, SPINNER_B)))

    def test_no_input_box_returns_none(self):
        self.assertIsNone(bridge._cc_stream_content("just some text\nno box"))
        self.assertIsNone(bridge._cc_stream_content(""))

    def test_ansi_defensively_stripped(self):
        c = bridge._cc_stream_content(pane(T0).replace("deploy.sh",
                                                       "\x1b[1mdeploy.sh\x1b[0m"))
        self.assertIn("deploy.sh", c)
        self.assertNotIn("\x1b", c)


class DiffAppendTests(unittest.TestCase):
    def test_pure_append(self):
        got, ok = bridge._cc_stream_diff_append("abc", "abcdef")
        self.assertTrue(ok)
        self.assertEqual(got, "def")

    def test_no_change(self):
        got, ok = bridge._cc_stream_diff_append("abc", "abc")
        self.assertTrue(ok)
        self.assertEqual(got, "")

    def test_scroll_reanchor(self):
        # 錨定窗最小 64 字元(太短的錨會誤咬):行要夠長才貼近真實 pane。
        lines = [f"這是第 {i} 行,故意寫得夠長,讓尾端錨定窗有足夠的字元可以咬住重新對齊"
                 for i in range(1, 5)]
        prev = "\n".join(lines)
        new = "\n".join(lines[2:]) + "\n這是捲動後才長出來的新行"   # 頂部被擠出視窗
        got, ok = bridge._cc_stream_diff_append(prev, new)
        self.assertTrue(ok)
        self.assertEqual(got, "\n這是捲動後才長出來的新行")

    def test_redraw_is_ambiguous(self):
        got, ok = bridge._cc_stream_diff_append("something old", "totally different")
        self.assertFalse(ok)
        self.assertEqual(got, "")


class PaneStreamExtractTests(unittest.TestCase):
    def setUp(self):
        self.st = bridge.CCPaneStream()

    def test_first_feed_sets_baseline_only(self):
        draft, changed = self.st.feed(pane(T0))
        self.assertFalse(changed)
        self.assertEqual(draft, "")

    def test_prose_streams_tool_output_does_not(self):
        self.st.feed(pane(T0))
        draft, changed = self.st.feed(pane(T1, SPINNER_B))
        self.assertTrue(changed)
        self.assertEqual(draft, "部署腳本有兩支:deploy.sh 負責")
        # 同一行長出 token(無換行)
        draft, changed = self.st.feed(pane(T2))
        self.assertTrue(changed)
        self.assertEqual(draft, "部署腳本有兩支:deploy.sh 負責主要流程,")
        # 折行的續行(縮排)
        draft, changed = self.st.feed(pane(T3, SPINNER_B))
        self.assertTrue(changed)
        self.assertEqual(draft,
                         "部署腳本有兩支:deploy.sh 負責主要流程,\n"
                         "rollback.sh 負責回滾。")
        # 工具塊:一個字都不准進草稿
        draft, changed = self.st.feed(pane(T4))
        self.assertFalse(changed)
        self.assertNotIn("zsh", draft)
        self.assertNotIn("pipefail", draft)
        # 工具塊之後的新 prose 繼續累積
        draft, changed = self.st.feed(pane(T5, SPINNER_B))
        self.assertTrue(changed)
        self.assertTrue(draft.endswith("結論:可以直接跑。"))

    def test_skipped_ticks_catch_up(self):
        """漏抓幾張(節流/慢 tick)→ 一次吃多段附加,結果不變。"""
        self.st.feed(pane(T0))
        draft, changed = self.st.feed(pane(T3))
        self.assertTrue(changed)
        self.assertEqual(draft,
                         "部署腳本有兩支:deploy.sh 負責主要流程,\n"
                         "rollback.sh 負責回滾。")

    def test_scroll_keeps_streaming(self):
        self.st.feed(pane(T3))
        # 頂部(歡迎框+使用者回顯)捲出視窗,底部長出新 prose
        scrolled = "\n".join(T3.splitlines()[6:]) + "\n\n⏺ 結論:可以直接跑。"
        draft, changed = self.st.feed(pane(scrolled))
        self.assertTrue(changed)
        self.assertEqual(draft, "結論:可以直接跑。")

    def test_redraw_skips_tick_no_garbage(self):
        self.st.feed(pane(T1))
        draft0 = self.st.feed(pane(T2))[0]
        # 全螢幕重繪(rewrap / clear):diff 對不上 → 保持原草稿、重定基線
        redraw = pane(WELCOME + "\n> 完全不同的畫面\n\n⏺ Read(bridge.py)")
        draft, changed = self.st.feed(redraw)
        self.assertFalse(changed)
        self.assertEqual(draft, draft0)
        # 新基線之後繼續能流
        draft, changed = self.st.feed(
            pane(WELCOME + "\n> 完全不同的畫面\n\n⏺ Read(bridge.py)\n\n⏺ 新的回覆"))
        self.assertTrue(changed)
        self.assertTrue(draft.endswith("新的回覆"))

    def test_unrecognized_layout_skips(self):
        self.st.feed(pane(T0))
        draft, changed = self.st.feed("no box at all")
        self.assertFalse(changed)
        self.assertEqual(self.st.baseline, bridge._cc_stream_content(pane(T0)))

    def test_oversized_burst_is_skipped(self):
        self.st.feed(pane(T0))
        blob = T0 + "\n\n⏺ " + "壘" * (bridge._CC_STREAM_MAX_TICK + 100)
        draft, changed = self.st.feed(pane(blob))
        self.assertFalse(changed)
        self.assertEqual(draft, "")

    def test_draft_saturates_at_cap(self):
        self.st.feed(pane(T0))
        with mock.patch.object(bridge, "_CC_STREAM_MAX_DRAFT", 20):
            self.st.feed(pane(T1))
            draft, _ = self.st.feed(pane(T2))
        self.assertLessEqual(len(draft), 20)

    def test_user_echo_never_enters_draft(self):
        self.st.feed(pane(T1))
        follow = T1 + "\n\n> 使用者又打了一句\n\n⏺ 收到。"
        draft, changed = self.st.feed(pane(follow))
        self.assertTrue(changed)
        self.assertNotIn("使用者又打了一句", draft)
        self.assertTrue(draft.endswith("收到。"))


class DraftCardTests(unittest.TestCase):
    def _store(self):
        store = carddigest.SessionCardStore()
        store.turn_id = "turn-abc123"
        return store

    def test_draft_upserts_final_false_rev_increments(self):
        store = self._store()
        st = bridge._cc_stream_state(store)
        bridge._cc_stream_upsert_draft(store, st, "部署腳本有兩支")
        cid = st.draft_id
        self.assertTrue(cid.startswith("card-cc-stream-"))
        card = store.cards[cid]
        self.assertFalse(card["final"])
        self.assertEqual(card["rev"], 1)
        self.assertEqual(card["body"]["origin"], "pane.stream")
        bridge._cc_stream_upsert_draft(store, st, "部署腳本有兩支:deploy.sh")
        card = store.cards[cid]
        self.assertEqual(card["rev"], 2)          # 同卡 rev++(app 原位換文)
        self.assertFalse(card["final"])

    def test_canonical_takes_over_draft_id(self):
        store = self._store()
        st = bridge._cc_stream_state(store)
        bridge._cc_stream_upsert_draft(store, st, "部署腳本有兩支:deploy.sh 負責")
        cid = st.draft_id
        full = "部署腳本有兩支:deploy.sh 負責主要流程,rollback.sh 負責回滾。"
        canonical = carddigest.make_card(
            "card-cc-9f3a1b-0", "turn-abc123", "assistant", "markdown",
            {"text": full, "fallback_text": full})
        out = bridge._cc_stream_reconcile(store, canonical)
        self.assertEqual(out["id"], cid)          # 正典卡接管草稿 id
        store.upsert_card(out)
        card = store.cards[cid]
        self.assertEqual(card["rev"], 2)
        self.assertTrue(card["final"])
        self.assertEqual(card["body"]["text"], full)
        self.assertNotIn("card-cc-9f3a1b-0", store.cards)
        self.assertEqual(st.draft_id, "")         # 草稿已關,下一塊另起新卡

    def test_reconcile_ignores_non_markdown_and_no_draft(self):
        store = self._store()
        st = bridge._cc_stream_state(store)
        bridge._cc_stream_upsert_draft(store, st, "草稿")
        tool = carddigest.make_card("card-cc-x-1", "turn-abc123", "assistant",
                                    "tool_call", {"tool": "Bash",
                                                  "fallback_text": "› 🔧 Bash"})
        self.assertEqual(bridge._cc_stream_reconcile(store, tool)["id"],
                         "card-cc-x-1")
        st.resolve_draft()
        md = carddigest.make_card("card-cc-x-2", "turn-abc123", "assistant",
                                  "markdown", {"text": "hi", "fallback_text": "hi"})
        self.assertEqual(bridge._cc_stream_reconcile(store, md)["id"],
                         "card-cc-x-2")           # 沒草稿:原 id 通過

    def test_orphan_draft_finalizes_after_grace(self):
        """正典卡沒來(被 digest 濾掉等)→ 寬限到期草稿自行定稿,
        不讓 app 永遠掛著 final:false 的打字中。"""
        store = self._store()
        st = bridge._cc_stream_state(store)
        bridge._cc_stream_upsert_draft(store, st, "孤兒草稿")
        cid = st.draft_id
        bridge._cc_stream_on_turn_end(store)
        self.assertGreater(st.final_deadline, 0)
        st.final_deadline = 1.0                   # 模擬寬限已過
        bridge._cc_stream_finalize_expired(store)
        card = store.cards[cid]
        self.assertTrue(card["final"])
        self.assertEqual(card["body"]["text"], "孤兒草稿")
        self.assertEqual(st.draft_id, "")

    def test_turn_begin_finalizes_leftover_and_resets(self):
        store = self._store()
        st = bridge._cc_stream_state(store)
        bridge._cc_stream_upsert_draft(store, st, "上一輪殘稿")
        cid = st.draft_id
        bridge._cc_stream_on_turn_begin(store)
        self.assertTrue(store.cards[cid]["final"])
        self.assertEqual(st.draft_id, "")
        self.assertEqual(st.draft, "")

    def test_digest_lines_reconciles_via_pipeline(self):
        """走真的 _cc_digest_lines:jsonl 正典行進來 → 草稿卡原位換文。"""
        store = self._store()
        store.media_session_id = "claude_code:test"
        st = bridge._cc_stream_state(store)
        bridge._cc_stream_upsert_draft(store, st, "回覆打到一半")
        cid = st.draft_id
        full = "回覆打到一半,現在是完整的正典全文。"
        line = ('{"type":"assistant","uuid":"aabbccdd-0000",'
                '"message":{"content":[{"type":"text","text":"%s"}]}}' % full)
        with mock.patch.object(bridge, "_schedule_media_capture"):
            n = bridge._cc_digest_lines(store, [line], "/tmp/x.jsonl", 0)
        self.assertEqual(n, 1)
        card = store.cards[cid]
        self.assertTrue(card["final"])
        self.assertEqual(card["body"]["text"], full)
        self.assertEqual(st.draft_id, "")


if __name__ == "__main__":
    unittest.main()
