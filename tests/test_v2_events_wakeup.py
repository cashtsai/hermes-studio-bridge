"""v2 SSE 事件驅動喚醒(取代 0.5s 輪詢)驗證。

跑法(repo 慣例,不用 pytest):
    python3 tests/test_v2_events_wakeup.py

驗證的對外行為:
1. SessionCardStore waker:_push 落 ring 即刻 set 所有訂閱者的 Event;
   多訂閱者各自一顆(無共用 set/clear 競態);detach 後不再被喚醒、不洩漏。
2. 端到端:GET /app/v2/sessions/{sid}/events 開流後,upsert 一落地事件
   就 flush —— 延遲遠低於舊 0.5s 輪詢量化(門檻 0.25s,實測應 <0.05s)。
   直接以自備 receive/send 驅動 ASGI app(httpx ASGITransport 會緩衝
   整條 response body,無限 SSE 流會掛死,不能用)。
3. keepalive 節奏不變:沒事件時照 SSE_KEEPALIVE_SECS 吐 ping。
4. 斷線清理:generator 收尾同時歸還 subscribers 計數與 waker(無洩漏)。
"""
import asyncio
import json
import os
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="v2wakeup-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ["POCKET_MEDIA_DIR"] = os.path.join(_TMP, "media")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import carddigest as cd  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ── 1. store 層:waker 生命週期 ──────────────────────────────────────────

async def t_store_wakers():
    store = cd.SessionCardStore()
    w1 = store.attach_waker()
    w2 = store.attach_waker()
    check("初始未觸發", not w1.is_set() and not w2.is_set())
    store._push("turn", {"state": "begin", "turn_id": "t1"})
    check("_push 喚醒所有訂閱者", w1.is_set() and w2.is_set())
    w1.clear()
    w2.clear()
    store.upsert_card({"id": "c1", "kind": "markdown", "rev": 1,
                       "body": {"text": "x"}})
    check("upsert_card(經 _push)也喚醒", w1.is_set() and w2.is_set())
    w1.clear()
    ev = store.set_status({"busy": True})
    check("set_status 有變 → 事件 + 喚醒", ev is not None and w1.is_set())
    w1.clear()
    ev = store.set_status({"busy": True})
    check("set_status 沒變 → 不發不吵", ev is None and not w1.is_set())
    w2.clear()
    store.detach_waker(w2)
    store._push("turn", {"state": "end", "turn_id": "t1"})
    check("detach 後不再喚醒", not w2.is_set())
    store.detach_waker(w1)
    check("detach 全數歸還(無洩漏)", len(store._wakers) == 0)


# ── 2/3/4. 端到端:SSE 開流 → 中途 push 即刻 flush;ping 照舊;斷線清理 ──

class _SSEClient:
    """自備 receive/send 直驅 ASGI app,同一個事件圈裡讀無限 SSE 流。"""

    def __init__(self, app, path: str, query: str, token: str):
        self._sent: asyncio.Queue = asyncio.Queue()
        self._disconnect = asyncio.Event()
        self._buf = ""
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": path, "raw_path": path.encode(),
            "query_string": query.encode(), "root_path": "",
            "headers": [(b"authorization", b"Bearer " + token.encode()),
                        (b"host", b"test")],
            "client": ("127.0.0.1", 1234), "server": ("test", 80),
        }

        async def receive():
            await self._disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(msg):
            await self._sent.put(msg)

        self.task = asyncio.create_task(app(scope, receive, send))

    async def start(self, deadline=10.0) -> dict:
        msg = await asyncio.wait_for(self._sent.get(), deadline)
        assert msg["type"] == "http.response.start", msg
        return msg

    async def next_event(self, deadline=6.0) -> tuple[dict, float]:
        """下一個 SSE data 信封 + 收到時刻。"""
        end = time.monotonic() + deadline
        while True:
            if "\n\n" in self._buf:
                frame, self._buf = self._buf.split("\n\n", 1)
                for line in frame.splitlines():
                    if line.startswith("data: "):
                        return json.loads(line[6:]), time.monotonic()
                continue
            msg = await asyncio.wait_for(self._sent.get(),
                                         max(0.05, end - time.monotonic()))
            if msg["type"] == "http.response.body":
                self._buf += msg.get("body", b"").decode("utf-8")

    async def disconnect(self):
        self._disconnect.set()
        try:
            await asyncio.wait_for(self.task, 10.0)
        except Exception:  # noqa: BLE001 (starlette 收尾方式視版本而定)
            pass


async def t_e2e_flush_latency():
    store = cd.SessionCardStore()

    async def fake_store(session_id):
        return store

    orig = bridge._v2_card_store
    bridge._v2_card_store = fake_store
    try:
        c = _SSEClient(bridge.app, "/app/v2/sessions/cc:fake/events",
                       "since_seq=0", os.environ["BRIDGE_TOKEN"])
        start = await c.start()
        ctype = dict(start["headers"]).get(b"content-type", b"").decode()
        check("SSE 200 + content-type",
              start["status"] == 200 and "text/event-stream" in ctype)

        # 等 serve loop 追平進入等待段(收到第一個 ping 即代表已在事件驅動
        # 等待中),再量 push→flush 延遲才乾淨;這同時驗 keepalive 節奏還在。
        ev, _ = await c.next_event()
        check("空 ring 先收到 keepalive ping(節奏保留)", ev["type"] == "ping")

        t_push = time.monotonic()
        store.upsert_card({"id": "c-live", "kind": "markdown", "rev": 1,
                           "body": {"text": "hi"}})
        ev, t_recv = await c.next_event()
        dt = t_recv - t_push
        check("push 中途落地即刻 flush(card.upsert)",
              ev["type"] == "card.upsert" and
              ev["data"]["card"]["id"] == "c-live")
        check(f"flush 延遲 {dt * 1000:.0f}ms < 250ms(舊輪詢平均 250ms/"
              "最壞 500ms)", dt < 0.25)

        # session.status 帶 usage 原樣過流(CC follower v2 對齊 v1 的形狀)
        store.set_status({"busy": True, "phase": "run", "label": "回覆中",
                          "usage": {"used": 12345, "size": 200000}})
        ev, _ = await c.next_event()
        check("session.status 帶 usage {used,size} 原樣到 app",
              ev["type"] == "session.status" and
              ev["data"].get("usage") == {"used": 12345, "size": 200000})

        check("開流期間 subscribers/waker 已掛上",
              store.subscribers == 1 and len(store._wakers) == 1)

        await c.disconnect()
        for _ in range(100):     # generator 收尾是伺服端非同步,給幾個 tick
            if store.subscribers == 0 and not store._wakers:
                break
            await asyncio.sleep(0.05)
        check("斷線後 subscribers 歸零 + waker 歸還(無洩漏)",
              store.subscribers == 0 and len(store._wakers) == 0)
    finally:
        bridge._v2_card_store = orig


async def main():
    await t_store_wakers()
    await t_e2e_flush_latency()


asyncio.run(main())
print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
