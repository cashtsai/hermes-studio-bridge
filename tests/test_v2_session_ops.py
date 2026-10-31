"""v2 統一 session 操作表 —— **對齊矩陣的守門**。

機主 2026-10-31:「每次維護其中一組的時候,另外兩組也會跟著被優化,不希望
三者之間的維護成本差異太多。」

這一檔就是那句話的執行機制:`_V2_SESSION_OPS` 是對齊矩陣本身,本檔斷言它的
覆蓋 —— **加 op 或加 provider 時,缺的格子要嘛補上,要嘛寫進
`_V2_OPS_KNOWN_GAPS` 並附一句理由。不准安靜地缺。**

(Gemini 之前就是安靜地缺:rename/archive/history 四散的三家都有、它沒有,
而且沒有任何地方看得出來 —— 要靠一次人工盤點才發現。這張表 + 這個守門
讓下一次的缺口在 CI 就紅。)
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="v2-ops-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import gemini_provider  # noqa: E402

#: 已知且有理由的缺格。每一筆都要有「為什麼」—— 沒理由的缺格是債,不是決定。
#: 格式:(op, provider): "理由"
_V2_OPS_KNOWN_GAPS = {
    ("rename", "codex"): "app-server 沒有 thread rename API(thread 標題由內容生成)",
    ("rename", "openclaw"): "session 身分在 OpenClaw 端,bridge 無權改名",
    ("rename", "hermes"): "persona 的名字是人格定義的一部分,不從 session 面改",
    ("archive", "openclaw"): "OpenClaw 端自己管生命週期,bridge 只讀",
    ("archive", "hermes"): "persona 對話無封存概念(TG 式常駐)",
    ("history", "openclaw"): "事件表就是歷史(/app/v2/…/events 已涵蓋),無另一份逐字稿",
    ("history", "hermes"): "persona 走 canonical store 的訊息分頁,不在這張表",
}

#: 表上該出現的 provider 全集 —— 新 provider 上線時加進來,守門會逼每個 op
#: 都表態(支援或進 KNOWN_GAPS)。
_ALL_PROVIDERS = ("claude_code", "codex", "cc2", "gemini", "openclaw", "hermes")


class OpsMatrixCoverageTest(unittest.TestCase):
    """矩陣完整性:每個 (op, provider) 要嘛有 handler,要嘛有理由。"""

    def test_every_cell_is_accounted_for(self):
        missing = []
        for op, table in bridge._V2_SESSION_OPS.items():
            for prov in _ALL_PROVIDERS:
                if prov not in table and (op, prov) not in _V2_OPS_KNOWN_GAPS:
                    missing.append((op, prov))
        self.assertEqual(missing, [],
                         f"這些格子沒有 handler 也沒有理由:{missing}。"
                         "補實作,或在 _V2_OPS_KNOWN_GAPS 寫明為什麼不做。")

    def test_no_stale_gap_entries(self):
        """格子補上了就把 KNOWN_GAPS 的那筆刪掉 —— 留著會誤導下一個人。"""
        stale = [(op, prov) for (op, prov) in _V2_OPS_KNOWN_GAPS
                 if prov in (bridge._V2_SESSION_OPS.get(op) or {})]
        self.assertEqual(stale, [],
                         f"這些缺口已經補了,KNOWN_GAPS 裡的理由該刪:{stale}")

    def test_gemini_supports_all_three_ops(self):
        """本輪的驗收條件:Gemini 三個 op 都要在表上(2026-10-31 對齊)。"""
        for op in ("rename", "archive", "history"):
            self.assertIn("gemini", bridge._V2_SESSION_OPS[op],
                          f"gemini 的 {op} 不見了 —— UI 的入口會變死按鈕")

    def test_unknown_combo_is_a_loud_404(self):
        """查不到的組合要**講清楚**,不是裸 404,更不准掉進別家的 fallback。

        cc2 seam 的教訓(2026-10-29):沒有明確分支的 id 落進 Codex 分支,
        拿別家的 key 當 thread id 去呼 —— 壞得安靜又離奇。
        """
        import asyncio
        from fastapi import HTTPException

        class _Req:  # 最小 Request 替身:_check_auth 只讀 headers
            headers = {"authorization": "Bearer test-unit-token"}
            query_params = {}

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(
                bridge._v2_session_op("rename", "codex:some-thread", _Req()))
        self.assertEqual(ctx.exception.status_code, 404)
        detail = str(ctx.exception.detail)
        self.assertIn("codex", detail, "要指名是哪家不支援")
        self.assertIn("rename", detail, "要指名是哪個 op")


class GeminiOpsBehaviorTest(unittest.TestCase):
    """gemini 三個 op 的實際行為(名錄層,不經 HTTP)。"""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="gm-ops-")
        self.p1 = patch.object(gemini_provider, "_STATE_DIR", self._dir)
        self.p2 = patch.object(gemini_provider, "_SESSIONS_FILE",
                               os.path.join(self._dir, "sessions.json"))
        self.p1.start(); self.p2.start()
        gemini_provider.remember_session("k1", "acp-1", title="原名")

    def tearDown(self):
        self.p1.stop(); self.p2.stop()

    def test_rename_changes_title_only(self):
        row = gemini_provider.rename_session("k1", "新名字")
        self.assertEqual(row["title"], "新名字")
        self.assertEqual(row["id"], "k1", "gemini 的身分是 key,改名不得搬鍵")

    def test_rename_missing_session_returns_none(self):
        self.assertIsNone(gemini_provider.rename_session("nope", "x"))

    def test_archive_hides_from_list_but_keeps_the_row(self):
        """封存=不列出來,**不是刪**。app 的手勢叫「封存」,按下去永久刪除
        就是文字與行為不一致(這正是不用 forget_session 的理由)。"""
        gemini_provider.set_archived("k1", True)
        self.assertEqual(gemini_provider.list_sessions(), [], "封存後不該列出")
        self.assertIsNotNone(gemini_provider.get_session("k1"), "但名錄列要還在")
        gemini_provider.set_archived("k1", False)
        self.assertEqual(len(gemini_provider.list_sessions()), 1, "復原要回得來")

    def test_history_page_shape_matches_cc2(self):
        """形狀對齊 cc2 的 history_page:{messages, more, total}。
        同形狀 = app 只要一個解析器 —— 這就是「維護一組,三組受益」。"""
        for i in range(10):
            gemini_provider.transcript_append("k1", "user", f"msg-{i}")
        page = gemini_provider.transcript_page("k1", limit=3, offset=0)
        self.assertEqual(set(page), {"messages", "more", "total"})
        self.assertEqual(page["total"], 10)
        self.assertEqual([m["content"] for m in page["messages"]],
                         ["msg-7", "msg-8", "msg-9"], "offset=0 是最新一頁,舊→新")
        self.assertTrue(page["more"])

        older = gemini_provider.transcript_page("k1", limit=3, offset=3)
        self.assertEqual([m["content"] for m in older["messages"]],
                         ["msg-4", "msg-5", "msg-6"], "offset 從最新往回數")

        tail = gemini_provider.transcript_page("k1", limit=5, offset=8)
        self.assertEqual([m["content"] for m in tail["messages"]],
                         ["msg-0", "msg-1"], "最後一頁不足額就給剩下的")
        self.assertFalse(tail["more"], "翻到底要說到底了")

    def test_history_empty_session(self):
        page = gemini_provider.transcript_page("never-spoke", limit=10)
        self.assertEqual(page, {"messages": [], "more": False, "total": 0})


if __name__ == "__main__":
    unittest.main()
