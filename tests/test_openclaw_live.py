"""S4 OpenClaw provider — 真靶機層(需本機隔離 gateway,預設 skip)。

跑法(靶機環境見 docs/OPENCLAW_PROVIDER_SPEC.md §0):
    OPENCLAW_LIVE=1 OPENCLAW_BASE_URL=ws://127.0.0.1:19801 \
    OPENCLAW_TOKEN=openclaw-dev-pocket-7f3a \
    python tests/test_openclaw_live.py

驗:真握手(protocol 4)、sessions.list 形狀、chat.send → chat final 事件
→ OpenClawDigest 出卡、chat.history seed 對得上。地端小模型回覆可能要
數十秒(冷載更久),整體 timeout 給 240s。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carddigest as cd  # noqa: E402
import openclaw_provider as ocp  # noqa: E402

LIVE = os.environ.get("OPENCLAW_LIVE") == "1"


@unittest.skipUnless(LIVE, "OPENCLAW_LIVE=1 才跑真靶機層")
class LiveGatewayTests(unittest.TestCase):
    def test_end_to_end_turn(self):
        async def run():
            client = ocp.OpenClawClient()
            self.assertTrue(client.configured(), "OPENCLAW_BASE_URL 未設")
            digest = cd.OpenClawDigest()
            final = asyncio.Event()
            key_holder = {}

            def on_event(event, payload):
                k = str(payload.get("sessionKey") or "")
                if key_holder.get("key") and k != key_holder["key"]:
                    return
                digest.handle(event, payload)
                # 完成訊號:session 上任何「帶 message」的 chat final/error。
                # 不賭自己的 runId —— 靶機 gateway 會把緊接的 send 併進
                # in-flight run(bare final ack 也不算完成,SPEC §3);
                # 逐 run 對位的不變量已由 mock 層覆蓋,live 層驗的是
                # 真握手/真事件/真 history。
                if event == "chat" and (
                        payload.get("state") == "error" or (
                            payload.get("state") == "final" and payload.get("message"))):
                    final.set()

            client.on_event = on_event

            # 1. sessions.list 形狀
            res = await client.call("sessions.list", {"limit": 10})
            self.assertIn("sessions", res)

            # 2. 發話(chat.send 立即回 runId)
            key = os.environ.get("OPENCLAW_LIVE_SESSION", "agent:main:main")
            key_holder["key"] = key
            idem = f"live-test-{int(time.time())}"
            sent = await client.call("chat.send", {
                "sessionKey": key,
                "message": "Reply with exactly: LIVE_OK",
                "idempotencyKey": idem})
            self.assertTrue(sent.get("runId"))
            self.assertEqual(sent.get("status"), "started")
            key_holder["run"] = str(sent["runId"])

            # 3. 等帶 message 的 final(地端模型冷載/排隊可能很久)
            await asyncio.wait_for(final.wait(), timeout=240)
            cards = digest.store.snapshot()["cards"]
            self.assertTrue(cards, "final 事件後 digest 必須有卡")
            self.assertTrue(all(c["body"].get("fallback_text") is not None
                                for c in cards))

            # 4. chat.history:我們的 user 訊息已落庫(idempotencyKey 對位),
            #    且 seed 出卡、__openclaw.id 穩定去重。
            h = await client.call("chat.history", {"sessionKey": key, "limit": 50})
            msgs = h.get("messages") or []
            self.assertTrue(any(idem in str(m.get("idempotencyKey") or "")
                                for m in msgs),
                            "chat.send 的 user 訊息必須出現在 chat.history")
            d2 = cd.OpenClawDigest()
            d2.seed_messages(msgs)
            n = len(d2.store.snapshot()["cards"])
            self.assertGreater(n, 0)
            d2.seed_messages(msgs)   # 重放不得雙份
            self.assertEqual(len(d2.store.snapshot()["cards"]), n)

            await client.close()

        asyncio.new_event_loop().run_until_complete(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
