"""/codexsessions 慢病三修(2026-08-18 深診定案)。

病:thread/list useStateDbOnly=False + limit 樓地板 40 >> 可見 13 條 →
daemon 為湊數全庫掃 rollout(1GB)→ 10-17s;v2 每 6s 輪詢 10s timeout
放棄但 daemon 照掃 → 排隊放大 45s;stale cache 吞 timeout →
degraded_providers 永遠 [] 說謊。
修:①三處改 useStateDbOnly=True(實測欄位等價、快 5-10 倍)②樓地板
40→20 ③v2 TTL 正向快取(5s)④stale 誠實旗標 → degraded 列 codex。
"""
import tests._isolation  # noqa: F401
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge  # noqa: E402


def _thread(i):
    return {"id": f"019f{i:04x}-0000-0000-0000-000000000000",
            "name": f"t{i}", "status": {"type": "notLoaded"},
            "sourceKind": "cli", "archived": False}


class TestStateDbOnly(unittest.TestCase):
    def test_all_thread_list_calls_use_statedb(self):
        # 碼面守衛:再有人把 rollout 掃描加回來,這裡先紅
        import inspect
        src = inspect.getsource(bridge)
        self.assertEqual(src.count('"useStateDbOnly": True'), 3)
        self.assertEqual(src.count('"useStateDbOnly": False'), 0)

    def test_overfetch_floor_is_20(self):
        import inspect
        src = inspect.getsource(bridge)
        self.assertIn('min(100, max(wanted, 20))', src)
        self.assertNotIn('max(wanted, 40)', src)


class TestV2TTLCacheAndHonesty(unittest.TestCase):
    def setUp(self):
        bridge._CODEX_V2_VISIBLE_CACHE = []
        bridge._CODEX_V2_VISIBLE_FRESH_AT = 0.0
        bridge._CODEX_V2_STALE = False

    def test_ttl_cache_absorbs_polling(self):
        calls = {"n": 0}

        async def _call(method, params=None, timeout=30.0):
            calls["n"] += 1
            return {"data": [_thread(1)], "nextCursor": None}

        with patch.object(bridge.CODEX_APP, "call", side_effect=_call):
            async def _run():
                a = await bridge._codex_v2_visible_threads(10)
                b = await bridge._codex_v2_visible_threads(10)   # TTL 內
                self.assertEqual(len(a), 1)
                self.assertEqual(a[0]["id"], b[0]["id"])
            asyncio.run(_run())
        self.assertEqual(calls["n"], 1)   # 第二次吃 TTL 快取,daemon 零打擾

    def test_ttl_expiry_refetches(self):
        calls = {"n": 0}

        async def _call(method, params=None, timeout=30.0):
            calls["n"] += 1
            return {"data": [_thread(calls["n"])], "nextCursor": None}

        with patch.object(bridge.CODEX_APP, "call", side_effect=_call):
            async def _run():
                await bridge._codex_v2_visible_threads(10)
                bridge._CODEX_V2_VISIBLE_FRESH_AT = time.time() - 99
                await bridge._codex_v2_visible_threads(10)
            asyncio.run(_run())
        self.assertEqual(calls["n"], 2)

    def test_stale_flag_set_on_timeout_and_cleared_on_fresh(self):
        ok = {"fail": False}

        async def _call(method, params=None, timeout=30.0):
            if ok["fail"]:
                raise bridge.CodexAppServerError("thread/list timed out")
            return {"data": [_thread(1)], "nextCursor": None}

        with patch.object(bridge.CODEX_APP, "call", side_effect=_call):
            async def _run():
                await bridge._codex_v2_visible_threads(10)      # 建快取
                self.assertFalse(bridge._CODEX_V2_STALE)
                ok["fail"] = True
                bridge._CODEX_V2_VISIBLE_FRESH_AT = 0.0          # 過 TTL
                rows = await bridge._codex_v2_visible_threads(10)  # 吃 stale
                self.assertEqual(len(rows), 1)
                self.assertTrue(bridge._CODEX_V2_STALE)          # 誠實旗標
                ok["fail"] = False
                bridge._CODEX_V2_VISIBLE_FRESH_AT = 0.0
                await bridge._codex_v2_visible_threads(10)       # 復原
                self.assertFalse(bridge._CODEX_V2_STALE)
            asyncio.run(_run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
