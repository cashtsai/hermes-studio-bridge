"""CC input 的冪等閘門:同一則訊息不准被貼進 TUI 兩次。

2026-10-25 機主實機:同一則訊息送出兩次(畫面兩顆一樣的泡泡)。bridge 日誌實證
同一則 POST /ccsessions/{name}/input 來自**兩個不同來源位址**(tailnet 100.x 與
IPv6 公網各一次)—— 人在移動、手機換網的瞬間新舊兩條連線各送了一次。

persona(_APP_TURN_INFLIGHT)與 CX(_CX_INPUT_INFLIGHT)早就有這層閘門,CC 一直
沒有:它收了 client_id 卻只拿去餵 input.accepted 卡片。而 CC 的送出是「貼進
live TUI 再按 Enter」—— 重送 = **重複執行一次指令**,比重複顯示嚴重得多。

釘住的契約:
  1. 同 client_id 的第二次請求 **不再貼字**,回放第一次的結果。
  2. 沒帶 client_id → 不擋(去重要有鍵才做得到),維持原行為。
  3. 不同 client_id / 不同 session → 各自獨立,不互相擋。
  4. 第一次**失敗**要釋放 claim —— `_cc_paste_text` 本來就會用 409 要求 app
     重送,不釋放的話那個「請重送」會被自己擋整整一個 TTL(永遠送不進去)。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須第一個 import
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge  # noqa: E402


class CCInputDedupTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bridge._CC_INPUT_INFLIGHT.clear()

    async def test_duplicate_client_id_pastes_only_once(self):
        """核心:換網雙送的情境 —— 第二次必須不貼字。"""
        calls = []

        async def fake_core(name, body):
            calls.append(body.get("text"))
            return {"ok": True, "delivery": "accepted"}

        body = {"text": "可以更新", "client_id": "abc-123"}
        with patch.object(bridge, "_cc_input_core_locked", side_effect=fake_core):
            r1 = await bridge._cc_input_core("probe", dict(body))
            r2 = await bridge._cc_input_core("probe", dict(body))

        self.assertEqual(len(calls), 1, f"同一則只該貼一次,實際 {len(calls)} 次")
        self.assertTrue(r1.get("ok"))
        self.assertEqual(r2.get("delivery"), "accepted", "第二次應回放第一次的結果")

    async def test_concurrent_duplicates_paste_only_once(self):
        """兩條連線**同時**送達(換網當下就是這樣),不是一前一後。"""
        calls = []

        async def slow_core(name, body):
            calls.append(body.get("text"))
            await asyncio.sleep(0.05)      # 模擬 bracketed paste + 回讀驗證
            return {"ok": True, "delivery": "accepted"}

        body = {"text": "可以更新", "client_id": "same-key"}
        with patch.object(bridge, "_cc_input_core_locked", side_effect=slow_core):
            r1, r2 = await asyncio.gather(
                bridge._cc_input_core("probe", dict(body)),
                bridge._cc_input_core("probe", dict(body)))

        self.assertEqual(len(calls), 1, f"併發重複只該貼一次,實際 {len(calls)} 次")
        self.assertTrue(r1.get("ok") or r2.get("ok"))

    async def test_without_client_id_is_not_blocked(self):
        """沒帶鍵就無從去重 —— 維持原行為,不能把正常送出擋掉。"""
        calls = []

        async def fake_core(name, body):
            calls.append(body.get("text"))
            return {"ok": True}

        with patch.object(bridge, "_cc_input_core_locked", side_effect=fake_core):
            await bridge._cc_input_core("probe", {"text": "一"})
            await bridge._cc_input_core("probe", {"text": "二"})
        self.assertEqual(len(calls), 2)

    async def test_different_keys_and_sessions_are_independent(self):
        calls = []

        async def fake_core(name, body):
            calls.append((name, body.get("client_id")))
            return {"ok": True}

        with patch.object(bridge, "_cc_input_core_locked", side_effect=fake_core):
            await bridge._cc_input_core("a", {"text": "x", "client_id": "k1"})
            await bridge._cc_input_core("a", {"text": "x", "client_id": "k2"})
            await bridge._cc_input_core("b", {"text": "x", "client_id": "k1"})
        self.assertEqual(len(calls), 3, "不同鍵/不同 session 不該互相擋")

    async def test_failure_releases_claim_so_retry_can_get_through(self):
        """第一次 409 之後,app 照指示重送 —— 那次重送必須進得來。"""
        calls = []

        async def flaky(name, body):
            calls.append(1)
            if len(calls) == 1:
                raise bridge.http_err(409, "CC_INPUT_NOT_ACCEPTED", "nope", "請重送")
            return {"ok": True, "delivery": "accepted"}

        body = {"text": "重送測試", "client_id": "retry-key"}
        with patch.object(bridge, "_cc_input_core_locked", side_effect=flaky):
            with self.assertRaises(bridge.BridgeError):
                await bridge._cc_input_core("probe", dict(body))
            r = await bridge._cc_input_core("probe", dict(body))   # 同鍵重送
        self.assertEqual(len(calls), 2, "失敗後的重送必須真的進得去")
        self.assertTrue(r.get("ok"))


if __name__ == "__main__":
    unittest.main()
