"""B1/B2/B3 OpenClaw 回合韌性(旗標 OPENCLAW_RESILIENCE)測試 —— 全假 WS/
client,不碰真 gateway。

對照 docs/HERMES_OPENCLAW_CONN_HARDENING_20260816.md:
- B1 看門狗:stall → chat.abort + 誠實終態(錯誤卡/busy→idle/turn end);
  健康回合(事件持續)絕不誤殺;回合正常收尾 → 看門狗取消。
- B2 重試殼:只重試 retryable;非冪等方法(approval.resolve)絕不重試;
  風暴門檻(次數/時間窗,env 可調)→ 合成終態。
- B3 斷線補洞:重連後只對「bridge 認定在跑、gateway 說收了」的 session
  補發 turnend 推播;冪等(第二次重連不重發);heartbeat run 照濾。
- 旗標關 = 三刀全缺席。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import asyncio
import os
import sys
import time
import unittest

os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ.pop("OPENCLAW_BASE_URL", None)
os.environ.pop("OPENCLAW_TOKEN", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carddigest as cd  # noqa: E402
import openclaw_provider as ocp  # noqa: E402
import bridge  # noqa: E402

KEY = "agent:main:dev"

# 旗標與門檻都是 per-call 讀 env,測試直接設/清 environ 即可,不用 reload。
_ENV_KEYS = ("OPENCLAW_RESILIENCE", "OPENCLAW_TURN_STALL_SECS",
             "OPENCLAW_RETRY_ESCALATE_MAX", "OPENCLAW_RETRY_ESCALATE_SECS",
             "OPENCLAW_CATCHUP_WINDOW_SECS", "OPENCLAW_CATCHUP_MAX_SESSIONS")


class _FakeOC:
    """注入 bridge.OPENCLAW 的假 client(照 test_openclaw_provider 同款,
    多了 fail_seq:method → 依序丟出的例外清單,耗盡後回 responses)。"""

    def __init__(self, configured=True):
        self._configured = configured
        self.calls = []
        self.responses = {}
        self.fail_seq = {}

    def configured(self):
        return self._configured

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params or {}))
        seq = self.fail_seq.get(method)
        if seq:
            raise seq.pop(0)
        return self.responses.get(method, {})

    def count(self, method):
        return sum(1 for m, _p in self.calls if m == method)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Base(unittest.TestCase):
    def setUp(self):
        self._orig = bridge.OPENCLAW
        self._env = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ["OPENCLAW_RESILIENCE"] = "1"
        self._clear_state()

    def tearDown(self):
        bridge.OPENCLAW = self._orig
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._clear_state()

    @staticmethod
    def _clear_state():
        bridge._OC_CARD_DIGESTS.clear()
        bridge._OC_TURN_STATE.clear()
        bridge._OC_TURN_WATCHDOGS.clear()
        bridge._OC_RETRY_ESCALATIONS.clear()
        bridge._OC_HEARTBEAT_RUNS.clear()

    @staticmethod
    def _start_turn(key=KEY, run="r1"):
        """建 digest + 餵 lifecycle start(經 _oc_events_feed,同 production 路)。"""
        d = bridge._OC_CARD_DIGESTS[key] = cd.OpenClawDigest()
        bridge._oc_events_feed("agent", {
            "sessionKey": key, "runId": run,
            "stream": "lifecycle", "data": {"phase": "start"}})
        return d

    @staticmethod
    def _cards(d):
        return d.store.snapshot()["cards"]

    @staticmethod
    async def _settle():
        """留著忙碌回合收場的測試,把看門狗收乾淨(loop 關掉前 cancel,
        免得 unittest 噴 pending task 警告)。"""
        for t in list(bridge._OC_TURN_WATCHDOGS.values()):
            t.cancel()
        await asyncio.sleep(0)


# ───────────────────────── B1:回合看門狗 ───────────────────────────────────

class WatchdogTests(_Base):
    def test_stall_synthesizes_terminal_and_idle(self):
        os.environ["OPENCLAW_TURN_STALL_SECS"] = "0.3"
        fake = _FakeOC()
        bridge.OPENCLAW = fake

        async def scenario():
            d = self._start_turn()
            self.assertTrue(d.busy)
            self.assertIn(KEY, bridge._OC_TURN_WATCHDOGS)
            await asyncio.sleep(0.8)   # > stall + 巡檢片
            return d

        d = _run(scenario())
        self.assertFalse(d.busy)                       # busy→idle
        self.assertEqual(fake.count("chat.abort"), 1)  # 有送 interrupt
        errs = [c for c in self._cards(d)
                if c["role"] == "system" and "卡住" in (c["body"].get("text") or "")]
        self.assertEqual(len(errs), 1)                 # 誠實錯誤卡
        ends = [e for e in d.store.events
                if e.get("type") == "turn" and (e.get("data") or {}).get("state") == "end"]
        self.assertTrue(ends)                          # turn end 事件(app 停轉)
        self.assertNotIn(KEY, bridge._OC_TURN_WATCHDOGS)
        self.assertFalse(bridge._OC_TURN_STATE[KEY]["busy"])

    def test_healthy_turn_untouched(self):
        os.environ["OPENCLAW_TURN_STALL_SECS"] = "0.4"
        fake = _FakeOC()
        bridge.OPENCLAW = fake

        async def scenario():
            d = self._start_turn()
            for i in range(6):   # 事件持續 0.6s > stall,但間隔遠小於 stall
                await asyncio.sleep(0.1)
                bridge._oc_events_feed("chat", {
                    "sessionKey": KEY, "runId": "r1", "state": "delta",
                    "deltaText": "x",
                    "message": {"role": "assistant",
                                "content": [{"type": "text",
                                             "text": "x" * (i + 1)}]}})
            await self._settle()
            return d

        d = _run(scenario())
        self.assertTrue(d.busy)                        # 沒被誤殺
        self.assertEqual(fake.count("chat.abort"), 0)
        self.assertFalse(any(c["role"] == "system" for c in self._cards(d)))

    def test_completion_cancels_watchdog(self):
        os.environ["OPENCLAW_TURN_STALL_SECS"] = "5"
        bridge.OPENCLAW = _FakeOC()

        async def scenario():
            d = self._start_turn()
            self.assertIn(KEY, bridge._OC_TURN_WATCHDOGS)
            bridge._oc_events_feed("agent", {
                "sessionKey": KEY, "runId": "r1",
                "stream": "lifecycle", "data": {"phase": "end"}})
            await asyncio.sleep(0.05)   # 讓 cancel 落地
            return d

        d = _run(scenario())
        self.assertFalse(d.busy)
        self.assertNotIn(KEY, bridge._OC_TURN_WATCHDOGS)
        st = bridge._OC_TURN_STATE[KEY]
        self.assertFalse(st["busy"])
        self.assertGreater(st["ended_at"], 0.0)        # B3 記帳:回合收尾時間

    def test_flag_off_no_watchdog(self):
        os.environ["OPENCLAW_RESILIENCE"] = "0"
        bridge.OPENCLAW = _FakeOC()

        async def scenario():
            return self._start_turn()

        d = _run(scenario())
        self.assertTrue(d.busy)                        # digest 行為不變
        self.assertEqual(bridge._OC_TURN_WATCHDOGS, {})
        self.assertEqual(bridge._OC_TURN_STATE, {})    # 記帳也整個缺席


# ───────────────────────── B2:重試預算 + 風暴升級 ──────────────────────────

def _retryable(msg="boom"):
    return ocp.OpenClawError(msg, code="CONNECTION_LOST", retryable=True)


class RetryTests(_Base):
    def test_retries_retryable_then_succeeds(self):
        fake = _FakeOC()
        fake.fail_seq["chat.send"] = [_retryable()]
        fake.responses["chat.send"] = {"runId": "run-9"}
        bridge.OPENCLAW = fake
        res = _run(bridge._oc_call_with_retry(
            "chat.send", {"sessionKey": KEY}, base_delay=0.01, session_key=KEY))
        self.assertEqual(res["runId"], "run-9")
        self.assertEqual(fake.count("chat.send"), 2)

    def test_retry_budget_exhausted_reraises(self):
        fake = _FakeOC()
        fake.fail_seq["chat.send"] = [_retryable(), _retryable()]
        bridge.OPENCLAW = fake
        with self.assertRaises(ocp.OpenClawError):
            _run(bridge._oc_call_with_retry(
                "chat.send", {}, base_delay=0.01, session_key=KEY))
        self.assertEqual(fake.count("chat.send"), 2)   # attempts=2 就停

    def test_non_retryable_error_single_shot(self):
        fake = _FakeOC()
        fake.fail_seq["chat.send"] = [
            ocp.OpenClawError("bad request", code="INVALID", retryable=False)]
        bridge.OPENCLAW = fake
        with self.assertRaises(ocp.OpenClawError):
            _run(bridge._oc_call_with_retry("chat.send", {}, base_delay=0.01))
        self.assertEqual(fake.count("chat.send"), 1)

    def test_non_idempotent_method_never_retried(self):
        """approval.resolve 是一次性副作用 —— 就算 retryable 也單發。"""
        fake = _FakeOC()
        fake.fail_seq["exec.approval.resolve"] = [_retryable()]
        bridge.OPENCLAW = fake
        with self.assertRaises(ocp.OpenClawError):
            _run(bridge._oc_call_with_retry(
                "exec.approval.resolve", {"id": "a1", "decision": "deny"},
                base_delay=0.01, session_key=KEY))
        self.assertEqual(fake.count("exec.approval.resolve"), 1)
        self.assertEqual(bridge._OC_RETRY_ESCALATIONS, {})   # 也不進風暴計數

    def test_flag_off_passthrough_no_retry(self):
        os.environ["OPENCLAW_RESILIENCE"] = "0"
        fake = _FakeOC()
        fake.fail_seq["chat.send"] = [_retryable()]
        bridge.OPENCLAW = fake
        with self.assertRaises(ocp.OpenClawError):
            _run(bridge._oc_call_with_retry("chat.send", {}, base_delay=0.01))
        self.assertEqual(fake.count("chat.send"), 1)

    def test_escalation_after_count_synthesizes_terminal(self):
        os.environ["OPENCLAW_RETRY_ESCALATE_MAX"] = "3"
        os.environ["OPENCLAW_RETRY_ESCALATE_SECS"] = "9999"
        os.environ["OPENCLAW_TURN_STALL_SECS"] = "60"
        fake = _FakeOC()
        bridge.OPENCLAW = fake

        async def scenario():
            d = self._start_turn()
            for _ in range(3):
                bridge._oc_note_retryable_failure(KEY, _retryable("gateway down"))
            await asyncio.sleep(0.1)   # 讓升級背景任務跑完
            return d

        d = _run(scenario())
        self.assertFalse(d.busy)                       # 合成終態:忙碌收掉
        self.assertEqual(fake.count("chat.abort"), 1)
        errs = [c for c in self._cards(d)
                if c["role"] == "system"
                and "連線持續失敗" in (c["body"].get("text") or "")]
        self.assertEqual(len(errs), 1)
        self.assertNotIn(KEY, bridge._OC_RETRY_ESCALATIONS)  # 計數歸零

    def test_below_threshold_no_escalation(self):
        os.environ["OPENCLAW_RETRY_ESCALATE_MAX"] = "5"
        os.environ["OPENCLAW_RETRY_ESCALATE_SECS"] = "9999"
        os.environ["OPENCLAW_TURN_STALL_SECS"] = "60"
        fake = _FakeOC()
        bridge.OPENCLAW = fake

        async def scenario():
            d = self._start_turn()
            for _ in range(4):   # < max
                bridge._oc_note_retryable_failure(KEY, _retryable())
            await asyncio.sleep(0.05)
            await self._settle()
            return d

        d = _run(scenario())
        self.assertTrue(d.busy)                        # 還沒到門檻不開刀
        self.assertEqual(fake.count("chat.abort"), 0)
        self.assertEqual(bridge._OC_RETRY_ESCALATIONS[KEY]["count"], 4)

    def test_escalation_time_window(self):
        """跨過時間窗還在失敗 → 次數沒到也升級(CX 同語意)。"""
        os.environ["OPENCLAW_RETRY_ESCALATE_MAX"] = "30"
        os.environ["OPENCLAW_RETRY_ESCALATE_SECS"] = "0.05"
        os.environ["OPENCLAW_TURN_STALL_SECS"] = "60"
        fake = _FakeOC()
        bridge.OPENCLAW = fake

        async def scenario():
            d = self._start_turn()
            bridge._oc_note_retryable_failure(KEY, _retryable())
            await asyncio.sleep(0.08)   # 超過窗
            bridge._oc_note_retryable_failure(KEY, _retryable())
            await asyncio.sleep(0.1)
            return d

        d = _run(scenario())
        self.assertFalse(d.busy)
        self.assertNotIn(KEY, bridge._OC_RETRY_ESCALATIONS)


# ───────────────────────── B3:斷線窗補洞 ───────────────────────────────────

def _ms(ts: float) -> int:
    return int(ts * 1000)


class CatchupTests(_Base):
    def setUp(self):
        super().setUp()
        self._orig_push = bridge.push_notify
        self.pushes = []

        async def _fake_push(title, body, data=None, **kw):
            self.pushes.append((title, body, data))
        bridge.push_notify = _fake_push

    def tearDown(self):
        bridge.push_notify = self._orig_push
        super().tearDown()

    @staticmethod
    def _sessions_row(key, updated_at, active=False):
        return {"key": key, "updatedAt": _ms(updated_at), "hasActiveRun": active}

    @staticmethod
    def _history(text, run_id="run-gap"):
        return {"messages": [
            {"role": "user", "content": "問題",
             "__openclaw": {"id": "u1"}},
            {"role": "assistant", "content": [{"type": "text", "text": text}],
             "__openclaw": {"id": "a1", "runId": run_id},
             "timestamp": _ms(time.time())},
        ]}

    def test_catchup_fires_only_for_gap_finished(self):
        now = time.time()
        fake = _FakeOC()
        fake.responses["sessions.list"] = {"sessions": [
            self._sessions_row("agent:main:gap", now - 60),          # 斷線窗收尾
            self._sessions_row("agent:main:idle", now - 60),         # 本來就閒
            self._sessions_row("agent:main:running", now - 30, True),  # 還在跑
            self._sessions_row("agent:main:stale", now - 7200),      # 超過窗
        ]}
        fake.responses["chat.history"] = self._history("gap 收尾回覆")
        bridge.OPENCLAW = fake
        # bridge 記帳:gap/running/stale 認定在跑,idle 沒有
        for k in ("agent:main:gap", "agent:main:running", "agent:main:stale"):
            st = bridge._oc_turn_state(k)
            st["busy"] = True
            st["last_event"] = now - 120

        async def scenario():
            await bridge._oc_reconnect_catchup()
            await asyncio.sleep(0.05)   # push_notify 是 fire-and-forget task

        _run(scenario())
        self.assertEqual(len(self.pushes), 1)          # 只補 gap 那條
        title, body, data = self.pushes[0]
        self.assertIn("gap", data["pocket"]["sessionId"])
        self.assertIn("gap 收尾回覆", body)
        # 記帳翻 idle,還在跑/超窗的不動
        self.assertFalse(bridge._OC_TURN_STATE["agent:main:gap"]["busy"])
        self.assertTrue(bridge._OC_TURN_STATE["agent:main:running"]["busy"])
        self.assertTrue(bridge._OC_TURN_STATE["agent:main:stale"]["busy"])
        # 只對 gap 那條抓 history(便宜守則)
        hist_keys = [p.get("sessionKey") for m, p in fake.calls
                     if m == "chat.history"]
        self.assertEqual(hist_keys, ["agent:main:gap"])

    def test_catchup_idempotent_across_reconnects(self):
        now = time.time()
        fake = _FakeOC()
        fake.responses["sessions.list"] = {"sessions": [
            self._sessions_row("agent:main:gap", now - 60)]}
        fake.responses["chat.history"] = self._history("一次就好")
        bridge.OPENCLAW = fake
        st = bridge._oc_turn_state("agent:main:gap")
        st["busy"] = True

        async def scenario():
            await bridge._oc_reconnect_catchup()
            await bridge._oc_reconnect_catchup()   # 第二次重連:缺口已補
            await asyncio.sleep(0.05)

        _run(scenario())
        self.assertEqual(len(self.pushes), 1)

    def test_catchup_skips_heartbeat_run(self):
        """gap 收尾但那是 heartbeat 自跑回合 → 沿用既有推播 dedup,不吵人。"""
        now = time.time()
        fake = _FakeOC()
        fake.responses["sessions.list"] = {"sessions": [
            self._sessions_row("agent:main:gap", now - 60)]}
        fake.responses["chat.history"] = self._history("heartbeat 產物",
                                                       run_id="hb-1")
        bridge.OPENCLAW = fake
        bridge._OC_HEARTBEAT_RUNS["hb-1"] = True
        st = bridge._oc_turn_state("agent:main:gap")
        st["busy"] = True

        async def scenario():
            await bridge._oc_reconnect_catchup()
            await asyncio.sleep(0.05)

        _run(scenario())
        self.assertEqual(self.pushes, [])
        # 但記帳照樣收掉(下次重連不再看到這個缺口)
        self.assertFalse(bridge._OC_TURN_STATE["agent:main:gap"]["busy"])

    def test_catchup_settles_watched_digest(self):
        """有 digest 的 session:補洞也要讓畫面停轉(turn end + idle)。"""
        now = time.time()
        fake = _FakeOC()
        fake.responses["sessions.list"] = {"sessions": [
            self._sessions_row(KEY, now - 60)]}
        fake.responses["chat.history"] = self._history("回來了")
        bridge.OPENCLAW = fake

        async def scenario():
            os.environ["OPENCLAW_TURN_STALL_SECS"] = "60"
            d = self._start_turn()          # digest busy + 記帳 busy + 看門狗
            await bridge._oc_reconnect_catchup()
            await asyncio.sleep(0.05)
            return d

        d = _run(scenario())
        self.assertFalse(d.busy)
        self.assertNotIn(KEY, bridge._OC_TURN_WATCHDOGS)
        ends = [e for e in d.store.events
                if e.get("type") == "turn" and (e.get("data") or {}).get("state") == "end"]
        self.assertTrue(ends)
        # 正常收尾(不是 stall)→ 不該有錯誤卡
        self.assertFalse(any(c["role"] == "system" for c in self._cards(d)))
        self.assertEqual(len(self.pushes), 1)

    def test_on_connect_gates_catchup(self):
        fake = _FakeOC()
        bridge.OPENCLAW = fake

        async def scenario(was_reconnect, flag):
            fake.calls.clear()
            os.environ["OPENCLAW_RESILIENCE"] = flag
            bridge._oc_on_connect(was_reconnect)
            await asyncio.sleep(0.1)   # 背景任務(reseed/catchup)跑完
            return [m for m, _p in fake.calls]

        # 冷啟:不掃 sessions.list(沒有前態可比)
        methods = _run(scenario(False, "1"))
        self.assertNotIn("sessions.list", methods)
        # 重連 + 旗標開:補洞
        methods = _run(scenario(True, "1"))
        self.assertIn("sessions.list", methods)
        # 重連 + 旗標關:缺席
        methods = _run(scenario(True, "0"))
        self.assertNotIn("sessions.list", methods)

    def test_catchup_sessions_list_failure_is_safe(self):
        fake = _FakeOC()
        fake.fail_seq["sessions.list"] = [_retryable("still down")]
        bridge.OPENCLAW = fake
        st = bridge._oc_turn_state("agent:main:gap")
        st["busy"] = True
        _run(bridge._oc_reconnect_catchup())
        self.assertEqual(self.pushes, [])
        # 查不到就保留前態,下次重連再補
        self.assertTrue(bridge._OC_TURN_STATE["agent:main:gap"]["busy"])


if __name__ == "__main__":
    unittest.main()
