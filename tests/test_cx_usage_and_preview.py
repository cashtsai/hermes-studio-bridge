"""CX 用量條爆表 + 預覽停在開場白 + updatedAt 落後 —— 2026-08-15 實機回報。
另收:預覽冷啟兜底(修 ②)—— digest 覆蓋只在記憶體活著時有效,bridge 重啟
後列表預覽退回開場白;持久快取檔(~/.pocket/cx-previews.json)要把它接住。

實機數字(Cashcamp thread 019f39d3):
  used = 517,323,425 / size = 258,400 → **200,203%**,其中 494,226,944(96%)
  是同一段 context 被反覆 cache 重讀灌出來的累計值。
  preview 停在 07-07 的開場白(codex 的 state DB 裡 preview 與
  first_user_message 一字不差)。
  updatedAt bridge 報 07-07,codex 自己的 DB 是 08-15 —— 差 39 天。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import bridge


WINDOW = 258_400


def tu(input_tokens, cached=0, output=0, total=None, window=WINDOW):
    """codex 的 tokenUsage 形狀(**累計**,不是單輪)。"""
    return {
        "inputTokens": input_tokens,
        "cachedInputTokens": cached,
        "outputTokens": output,
        "totalTokens": total if total is not None else input_tokens + output,
        "modelContextWindow": window,
    }


class ContextUsedTests(unittest.TestCase):
    """`used` 要是「當前上下文佔用」,與 size 同一把尺。"""

    def test_delta_of_input_tokens_is_the_context(self):
        prev = tu(1_000_000, cached=900_000)
        cur = tu(1_120_000, cached=1_000_000)
        self.assertEqual(bridge._codex_context_used(cur, prev), 120_000)

    def test_no_previous_snapshot_gives_none(self):
        """算不出就別猜 —— 寧可不顯示,也不要顯示差兩三個數量級的數字。"""
        self.assertIsNone(bridge._codex_context_used(tu(500_000), None))

    def test_delta_is_clamped_to_the_window(self):
        """一個 turn 可能有多次模型請求,增量會超過視窗;夾到上限而不是畫 >100%。"""
        self.assertEqual(
            bridge._codex_context_clamp(686_279, WINDOW), WINDOW)
        self.assertEqual(bridge._codex_context_clamp(120_000, WINDOW), 120_000)
        self.assertIsNone(bridge._codex_context_clamp(None, WINDOW))

    def test_zero_or_negative_delta_gives_none(self):
        """thread 被 compact / app-server 重啟後累計會歸零或倒退。"""
        prev = tu(900_000)
        self.assertIsNone(bridge._codex_context_used(tu(900_000), prev))
        self.assertIsNone(bridge._codex_context_used(tu(10_000), prev))

    def test_regression_lifetime_total_is_never_used_as_context(self):
        """回歸:實機那組數字不得再產生 >100% 的用量條。"""
        prev = tu(515_000_000, cached=494_000_000, output=1_600_000,
                  total=516_600_000)
        cur = tu(515_686_279, cached=494_226_944, output=1_637_146,
                 total=517_323_425)
        out = bridge._codex_usage_map(cur, prev=prev)
        self.assertIsNotNone(out)
        self.assertLessEqual(out["used"], out["size"],
                             "用量條又爆表了(used 取到生命週期累計)")
        # 兩次通知之間夾了多輪請求 → 增量會超過視窗,夾到上限代表「已經滿了」
        # 累計值仍要保留 —— 花費是靠它算的
        self.assertEqual(out["total_tokens"], 517_323_425)


class UsageShapeTests(unittest.TestCase):
    def test_used_is_omitted_when_not_computable(self):
        """app 的 ContextUsage.used 是 optional,缺欄位 → 整條用量條不畫。"""
        out = bridge._codex_usage_map(tu(500_000), prev=None)
        self.assertIsNotNone(out)
        self.assertNotIn("used", out)
        self.assertEqual(out["size"], WINDOW, "size 仍要給(其他用途)")

    def test_cost_fields_survive_without_used(self):
        out = bridge._codex_usage_map(tu(500_000, cached=400_000, output=1_000),
                                      prev=None)
        self.assertEqual(out["input_tokens"], 500_000)
        self.assertEqual(out["cache_read_tokens"], 400_000)
        self.assertEqual(out["output_tokens"], 1_000)

    def test_used_present_and_sane_when_computable(self):
        out = bridge._codex_usage_map(tu(220_000, cached=200_000),
                                      prev=tu(100_000, cached=90_000))
        self.assertEqual(out["used"], 120_000)
        self.assertLess(out["used"], out["size"])


class _FakeStore:
    """真實卡片一定帶 role/kind(carddigest.make_card 的必填參數),
    fixture 沒給就補成最常見的對話卡 —— 否則測的是一種不存在的卡。
    要測「非對話卡」的案例就明確傳入 role/kind(見 PreviewKindFilterTests)。"""

    def __init__(self, cards):
        norm = [{**c,
                 "role": c.get("role", "assistant"),
                 "kind": c.get("kind", "text")} for c in cards]
        self.cards = {c["id"]: c for c in norm}
        self.order = [c["id"] for c in norm]


class _FakeDigest:
    def __init__(self, store):
        self.store = store


class LatestPreviewTests(unittest.TestCase):
    """preview 不該永遠是開場白。"""

    def test_newest_card_text_wins(self):
        d = _FakeDigest(_FakeStore([
            {"id": "c1", "body": {"fallback_text": "開場白"}},
            {"id": "c2", "body": {"fallback_text": "最新的一句話"}},
        ]))
        with mock.patch.dict(bridge._CX_CARD_DIGESTS, {"t1": d}, clear=False):
            self.assertEqual(bridge._cx_latest_card_text("t1"), "最新的一句話")

    def test_skips_empty_cards(self):
        d = _FakeDigest(_FakeStore([
            {"id": "c1", "body": {"fallback_text": "有內容"}},
            {"id": "c2", "body": {"fallback_text": "   "}},
        ]))
        with mock.patch.dict(bridge._CX_CARD_DIGESTS, {"t1": d}, clear=False):
            self.assertEqual(bridge._cx_latest_card_text("t1"), "有內容")

    def test_no_digest_is_empty_not_crash(self):
        """沒開過卡片流的 thread 維持原樣,不為了預覽多打一次 API。"""
        self.assertEqual(bridge._cx_latest_card_text("不存在的-thread"), "")

    def test_whitespace_is_collapsed(self):
        d = _FakeDigest(_FakeStore([
            {"id": "c1", "body": {"fallback_text": "多行\n\n  內容"}},
        ]))
        with mock.patch.dict(bridge._CX_CARD_DIGESTS, {"t1": d}, clear=False):
            self.assertEqual(bridge._cx_latest_card_text("t1"), "多行 內容")


class PreviewCacheTests(unittest.TestCase):
    """修 ②:預覽冷啟兜底 —— 快取檔寫入節流 / 讀取優先序 / 毀損容錯 / LRU。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="cx-preview-")
        self.path = os.path.join(self._tmp, "cx-previews.json")
        self._patches = [
            mock.patch.object(bridge, "CX_PREVIEW_CACHE_PATH", self.path),
            mock.patch.object(bridge, "_CX_PREVIEW_CACHE", None),
            mock.patch.object(bridge, "_CX_PREVIEW_LAST_WRITE", {}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _digest(self, tid, text):
        return mock.patch.dict(
            bridge._CX_CARD_DIGESTS,
            {tid: _FakeDigest(_FakeStore([{"id": "c1",
                                           "body": {"fallback_text": text}}]))},
            clear=False)

    def _raw(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def _reload(self):
        """模擬 bridge 重啟:記憶體快取歸零,強迫下一次讀走檔案。"""
        bridge._CX_PREVIEW_CACHE = None
        bridge._CX_PREVIEW_LAST_WRITE.clear()

    def test_note_persists_and_survives_restart(self):
        with self._digest("t-a", "最新進度:模型已回覆"):
            bridge._cx_preview_cache_note("t-a")
        self._reload()   # 重啟後 digest 不在了
        self.assertEqual(bridge._cx_list_preview_text("t-a"),
                         "最新進度:模型已回覆")
        raw = self._raw()
        self.assertIn("t-a", raw)
        self.assertGreater(raw["t-a"]["ts"], 0)

    def test_write_throttled_per_thread(self):
        """同 thread 30s 內第二次 note 不落盤;窗過了才會。"""
        with self._digest("t-b", "第一句"):
            bridge._cx_preview_cache_note("t-b")
        with self._digest("t-b", "第二句"):
            bridge._cx_preview_cache_note("t-b")   # 節流:不該寫
        raw = self._raw()
        self.assertEqual(raw["t-b"]["text"], "第一句")
        # 把節流基準撥回 31 秒前 → 同一呼叫就該落盤。
        bridge._CX_PREVIEW_LAST_WRITE["t-b"] -= (
            bridge.CX_PREVIEW_WRITE_MIN_SECS + 1)
        with self._digest("t-b", "第二句"):
            bridge._cx_preview_cache_note("t-b")
        raw = self._raw()
        self.assertEqual(raw["t-b"]["text"], "第二句")

    def test_memory_digest_wins_over_cache(self):
        """讀取優先序:記憶體 digest(新鮮)> 快取檔 > 空。"""
        with self._digest("t-c", "舊的快取內容"):
            bridge._cx_preview_cache_note("t-c")
        self._reload()
        with self._digest("t-c", "記憶體裡更新的"):
            self.assertEqual(bridge._cx_list_preview_text("t-c"),
                             "記憶體裡更新的")
        # digest 不在 → 退快取;連快取都沒有 → 空字串(維持開場白現狀)。
        self.assertEqual(bridge._cx_list_preview_text("t-c"), "舊的快取內容")
        self.assertEqual(bridge._cx_list_preview_text("t-無此人"), "")

    def test_corrupt_cache_file_is_tolerated(self):
        """檔案毀損:讀=空快取不炸;下一次寫入自癒成合法 JSON。"""
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{死掉的 json…")
        self.assertEqual(bridge._cx_list_preview_text("t-d"), "")
        with self._digest("t-d", "自癒後的內容"):
            bridge._cx_preview_cache_note("t-d")
        self._reload()
        self.assertEqual(bridge._cx_list_preview_text("t-d"), "自癒後的內容")
        raw = self._raw()   # 合法 JSON 了
        self.assertEqual(raw["t-d"]["text"], "自癒後的內容")

    def test_text_truncated_to_120_chars(self):
        long = "字" * 300
        with self._digest("t-e", long):
            bridge._cx_preview_cache_note("t-e")
        self._reload()
        self.assertEqual(bridge._cx_list_preview_text("t-e"), "字" * 120)

    def test_lru_cap_evicts_oldest(self):
        with mock.patch.object(bridge, "CX_PREVIEW_CACHE_MAX", 3):
            for i in range(5):
                with self._digest(f"t-{i}", f"內容 {i}"):
                    bridge._cx_preview_cache_note(f"t-{i}")
        raw = self._raw()
        # schema 版號也住在同一份 JSON 裡(見 CX_PREVIEW_SCHEMA_KEY),
        # 它不是一條 thread,也不佔 LRU 名額 —— 比的是剩下哪幾條 thread。
        self.assertEqual(set(raw) - {bridge.CX_PREVIEW_SCHEMA_KEY},
                         {"t-2", "t-3", "t-4"},
                         "超過上限要淘汰最舊的,不是最新的")

    def test_unchanged_text_skips_disk_write(self):
        """同文字重 note 不重寫盤(mtime 不動),熱串流不會白耗 IO。"""
        with self._digest("t-f", "同一句"):
            bridge._cx_preview_cache_note("t-f")
            bridge._CX_PREVIEW_LAST_WRITE.clear()   # 排除節流因素
            before = os.stat(self.path).st_mtime_ns
            bridge._cx_preview_cache_note("t-f")
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)


class PreviewKindFilterTests(unittest.TestCase):
    """列表副標只能是「對話」—— 工具步驟/思考/審核/狀態、以及 bridge 自己推的
    系統卡都不算最新訊息。

    2026-10-26 機主:「CX 列表怪怪的」。實查 ~/.pocket/cx-previews.json 28 條中
    3 條中毒:`› 🔧 imageGeneration`、`📭 這條 session 讀不到歷史訊息 …`
    (bridge 自己的 _cx_feed_empty_history 卡,從 10-08 釘到當天)、
    `I attempted to execute: ```sh …`。
    """

    def _digest(self, cards):
        return mock.patch.dict(bridge._CX_CARD_DIGESTS,
                               {"t1": _FakeDigest(_FakeStore(cards))},
                               clear=False)

    def test_tool_card_is_not_the_preview(self):
        """最新一張是工具卡 → 要往回找到上一句真的對話。"""
        with self._digest([
            {"id": "c1", "role": "assistant", "kind": "markdown",
             "body": {"fallback_text": "我來跑這個指令"}},
            {"id": "c2", "role": "assistant", "kind": "tool_call",
             "body": {"fallback_text": "› 🔧 imageGeneration"}},
        ]):
            self.assertEqual(bridge._cx_latest_card_text("t1"), "我來跑這個指令")

    def test_bridge_system_card_is_not_the_preview(self):
        """bridge 自己推的說明卡(role=system)不准變成副標 —— 實錄那條
        「📭 讀不到歷史訊息」在列表上釘了 18 天。"""
        with self._digest([
            {"id": "c1", "role": "user", "kind": "text",
             "body": {"fallback_text": "幫我看一下"}},
            {"id": "c2", "role": "system", "kind": "text",
             "body": {"fallback_text": "📭 這條 session 讀不到歷史訊息 ——"}},
        ]):
            self.assertEqual(bridge._cx_latest_card_text("t1"), "幫我看一下")

    def test_all_non_conversational_kinds_are_skipped(self):
        for kind in ("tool_call", "tool_result", "status", "approval",
                     "reasoning", "plan"):
            with self.subTest(kind=kind):
                with self._digest([
                    {"id": "c1", "role": "assistant", "kind": "text",
                     "body": {"fallback_text": "真正的回覆"}},
                    {"id": "c2", "role": "assistant", "kind": kind,
                     "body": {"fallback_text": f"不該出現的 {kind}"}},
                ]):
                    self.assertEqual(bridge._cx_latest_card_text("t1"),
                                     "真正的回覆")

    def test_user_and_assistant_text_both_count(self):
        """自己送的話也是對話,照樣可以當副標(app 端另外加「你:」前綴)。"""
        for role in ("user", "assistant"):
            with self.subTest(role=role):
                with self._digest([
                    {"id": "c1", "role": role, "kind": "text",
                     "body": {"fallback_text": "這句算數"}},
                ]):
                    self.assertEqual(bridge._cx_latest_card_text("t1"), "這句算數")

    def test_only_tool_cards_means_no_preview(self):
        """整條 digest 都沒有對話卡 → 回空,讓呼叫端退回開場白。
        寧可退回開場白,也不要拿工具步驟充數。"""
        with self._digest([
            {"id": "c1", "role": "assistant", "kind": "tool_call",
             "body": {"fallback_text": "› 🔧 shell"}},
        ]):
            self.assertEqual(bridge._cx_latest_card_text("t1"), "")


class PreviewCacheSchemaTests(unittest.TestCase):
    """舊格式快取要被作廢 —— 它只存文字,無法事後重新分類。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "cx-previews.json")
        self._patches = [
            mock.patch.object(bridge, "CX_PREVIEW_CACHE_PATH", self.path),
            mock.patch.object(bridge, "_CX_PREVIEW_CACHE", None),
        ]
        for p in self._patches:
            p.start()
        bridge._CX_PREVIEW_LAST_WRITE.clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_pre_schema_cache_is_discarded(self):
        """2026-10-26 之前寫的檔沒有版號 → 整份丟掉(裡面可能混著工具卡文字)。"""
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"t-old": {"text": "› 🔧 imageGeneration", "ts": 1.0}}, f)
        bridge._CX_PREVIEW_CACHE = None
        self.assertEqual(bridge._cx_preview_cache_get("t-old"), "")

    def test_current_schema_cache_is_kept(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({bridge.CX_PREVIEW_SCHEMA_KEY: bridge.CX_PREVIEW_SCHEMA,
                       "t-new": {"text": "真的訊息", "ts": 1.0}}, f)
        bridge._CX_PREVIEW_CACHE = None
        self.assertEqual(bridge._cx_preview_cache_get("t-new"), "真的訊息")

    def test_schema_key_is_not_served_as_a_thread(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({bridge.CX_PREVIEW_SCHEMA_KEY: bridge.CX_PREVIEW_SCHEMA,
                       "t-new": {"text": "真的訊息", "ts": 1.0}}, f)
        bridge._CX_PREVIEW_CACHE = None
        self.assertEqual(bridge._cx_preview_cache_get(bridge.CX_PREVIEW_SCHEMA_KEY), "")


if __name__ == "__main__":
    unittest.main()
