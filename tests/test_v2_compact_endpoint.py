"""`POST /app/v2/sessions/{id}/compact`:CX / Gemini 也能像 CC 一樣一直接續。

機主 2026-10-10:「cx 內的 session 有什麼辦法可以跟 cc 一樣一直能夠接續」。
三家其實都有壓縮,只是 bridge 只接了 CC:
  ‧ CC     `/ccsessions/{name}/compress`(自建 ccsess)        ← 早就有
  ‧ CX     app-server `thread/compact/start {threadId}`        ← 本刀接上
  ‧ Gemini gemini-cli 的 `/compress`(它也會自動壓)           ← 本刀接上

順帶記一個查證結果,免得後人又走冤枉路:**壓縮清的是模型 context,不是
磁碟上的 rollout 檔**。那支檔案是 append-only,壓縮後照樣變大;而
「thread/read 逾時」實測與檔案大小無關(158MB 的 thread 熱機讀 0.1s)。
"""
import _isolation  # noqa: F401
import asyncio
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="v2-compact-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

TID = "019f6e0e-4680-7d62-8ea1-786acd93e71a"


class _Req:
    def __init__(self):
        self.headers = {"authorization": f"Bearer {bridge.BRIDGE_TOKEN}"}
        self.client = type("C", (), {"host": "127.0.0.1"})()
        self.query_params = {}
    class _URL:
        path = "/app/v2/sessions/x/compact"
    url = _URL()


class V2CompactTest(unittest.TestCase):
    def setUp(self):
        self.orig_call = bridge.CODEX_APP.call
        self.orig_gm_input = bridge._gm_input_core
        self.orig_busy = bridge._gm_busy
        self.orig_conf = bridge.GEMINI.configured
        bridge._CX_COMPACT_RUNNING.clear()

    def tearDown(self):
        bridge.CODEX_APP.call = self.orig_call
        bridge._gm_input_core = self.orig_gm_input
        bridge._gm_busy = self.orig_busy
        bridge.GEMINI.configured = self.orig_conf
        bridge._CX_COMPACT_RUNNING.clear()

    def test_cx_calls_thread_compact_start(self):
        seen = {}

        async def fake(method, params, timeout=None):
            seen["method"], seen["params"] = method, params
            return {}
        bridge.CODEX_APP.call = fake
        out = asyncio.run(bridge.v2_session_compact(f"codex:{TID}", _Req()))
        self.assertEqual(seen["method"], "thread/compact/start")
        self.assertEqual(seen["params"], {"threadId": TID})
        self.assertTrue(out["started"])

    def test_cx_double_tap_is_rejected(self):
        """壓縮要跑一陣子 —— 連點不該疊第二次。"""
        async def fake(method, params, timeout=None):
            return {}
        bridge.CODEX_APP.call = fake
        asyncio.run(bridge.v2_session_compact(f"codex:{TID}", _Req()))
        with self.assertRaises(bridge.HTTPException) as cm:
            asyncio.run(bridge.v2_session_compact(f"codex:{TID}", _Req()))
        self.assertEqual(cm.exception.status_code, 409)

    def test_gemini_goes_through_the_normal_send_path(self):
        """壓縮不開第二條送出管線 —— 走 _gm_input_core,與一般送話同一條。"""
        seen = {}

        async def fake_input(key, session_id, body):
            seen["key"], seen["content"] = key, body.get("content")
            return {"ok": True}
        bridge._gm_input_core = fake_input
        bridge._gm_busy = lambda k: False
        bridge.GEMINI.configured = lambda: True
        out = asyncio.run(bridge.v2_session_compact("gemini:default", _Req()))
        self.assertEqual(seen["content"], "/compress")
        self.assertTrue(out["started"])

    def test_gemini_busy_is_rejected(self):
        bridge._gm_busy = lambda k: True
        bridge.GEMINI.configured = lambda: True
        with self.assertRaises(bridge.HTTPException) as cm:
            asyncio.run(bridge.v2_session_compact("gemini:default", _Req()))
        self.assertEqual(cm.exception.status_code, 409)

    def test_cc_points_at_its_own_endpoint(self):
        """CC 有自己的壓縮管線,不在這裡重做 —— 但要明確指路,不能默默失敗。

        直接釘路由:測試環境沒有真的 CC session(來源解析會先 404),
        所以把 `_v2_card_source` 換掉,驗的是「認出是 CC 之後怎麼回」。
        """
        orig = bridge._v2_card_source
        bridge._v2_card_source = lambda sid: ("cc", "Pocket-Main")
        try:
            with self.assertRaises(bridge.HTTPException) as cm:
                asyncio.run(bridge.v2_session_compact("claude_code:Pocket-Main", _Req()))
        finally:
            bridge._v2_card_source = orig
        self.assertEqual(cm.exception.status_code, 409)
        self.assertIn("ccsessions", cm.exception.message)


if __name__ == "__main__":
    unittest.main()
