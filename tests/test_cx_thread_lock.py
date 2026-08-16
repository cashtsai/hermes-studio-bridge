"""CX thread-store 寫入鎖:被桌面版 Codex/ChatGPT 佔用時要**大聲**壞掉。

實機事故(2026-08-10,善彰的機器):ChatGPT.app 自帶的 codex app-server
握著某些 thread 的 writer lock,bridge 這顆 app-server 的 thread/resume 被拒:

    RPC   {"error": {"code": -32600,
                     "message": "thread 019f39d3-… already has an active writer"}}
    stderr "... ERROR codex_core::session::session: failed to initialize thread
            persistence: thread-store conflict: thread 019f39d3-… already has
            an active writer"

舊行為:-32600 被翻成 CX_TURN_IN_FLIGHT(「上一輪正在跑」,語意相反)、warm
loop 每幾秒重試一次只寫一行 error type、UI 完全沒有訊號 → 使用者看到的是
「點了沒反應」,查了一整天以為是送出佇列的 bug。

這裡驗六件事:
  1. 分類:conflict → CX_THREAD_LOCKED;**一般 app-server 錯誤不得誤判**。
  2. POST 兩條 input 路由(v1 / v2)回 409 + zh-TW 人話。
  3. 錯誤卡進到**那條 session** 的卡片流。
  4. 狀態 payload(v1 summary / v2 清單 / 卡片流 session.status)帶 locked。
  5. warm loop 抑制窗:窗內不重試,窗過了才重試。
  6. 復原:resume 成功 → locked 翻回 False + 推恢復卡。

跑法:
    PYTHONPATH=. python -m unittest tests.test_cx_thread_lock
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import asyncio
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="cxlock-canon-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import carddigest  # noqa: E402

# 實機 log 逐字複製(去掉 ANSI 色碼外的內容一字未改)。
LOCKED_TID = "019f39d3-e347-7203-ba4f-fa92948d149c"
REAL_STDERR_1 = (
    "2026-08-10T23:16:01.268459Z ERROR codex_core::session::session: "
    "failed to initialize thread persistence: thread-store conflict: "
    f"thread {LOCKED_TID} already has an active writer")
REAL_STDERR_2 = (
    "2026-08-10T23:16:01.268538Z ERROR codex_core::session: "
    "Failed to create session: thread-store conflict: "
    f"thread {LOCKED_TID} already has an active writer")
# `codex_provider_error` 實際收到的 RPC error(code 是 codex 的泛用 -32600)。
REAL_RPC_MESSAGE = f"thread {LOCKED_TID} already has an active writer"


def run(coro):
    return asyncio.run(coro)


def reset_auth_throttle():
    """認證失敗限流器是行程全域的,全套跑時 409 會被前面的測試擠成 429。"""
    with bridge._AUTH_LOCK:
        for attr in ("_AUTH_FAILS", "_AUTH_FAILS_BY_CLIENT", "_AUTH_FAIL_AGG"):
            container = getattr(bridge, attr, None)
            if container is not None:
                container.clear()


class FakeCall:
    """假 JSON-RPC:可指定 thread/resume 拋鎖衝突或其他錯誤。"""

    def __init__(self, resume_error=None):
        self.resume_error = resume_error
        self.calls = []

    async def __call__(self, method, params=None, timeout=None):
        self.calls.append(method)
        if method == "thread/resume" and self.resume_error is not None:
            raise self.resume_error
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        if method == "thread/read":
            return {"thread": {"id": (params or {}).get("threadId") or ""}}
        return {}

    def count(self, method):
        return sum(1 for m in self.calls if m == method)


def locked_error():
    return bridge.CodexAppServerError(REAL_RPC_MESSAGE, code=-32600)


def fresh_client(resume_error=None):
    c = bridge.CodexAppServerClient()
    c.call = FakeCall(resume_error=resume_error)
    return c


class ClassificationTests(unittest.TestCase):
    """1. 分類 —— 這是整個修復的地基,誤判的代價是把正常錯誤也標成鎖。"""

    def test_real_rpc_message_is_a_lock(self):
        tid = bridge._codex_thread_lock_conflict(locked_error())
        self.assertEqual(tid, LOCKED_TID, "RPC error 帶得到 thread id")

    def test_real_stderr_lines_are_locks(self):
        for line in (REAL_STDERR_1, REAL_STDERR_2):
            self.assertEqual(
                bridge._codex_thread_lock_conflict(
                    bridge.CodexAppServerError(line)), LOCKED_TID, line)

    def test_thread_id_is_taken_from_next_to_the_marker(self):
        """tracing span 也帶 uuid;抓「第一個 uuid」會把鎖記到無辜的 thread 上。"""
        other = "0192aaaa-bbbb-cccc-dddd-eeeeffff0000"
        line = (f"2026-08-10T23:16:01Z ERROR codex_core::session{{conversation_id={other}}}: "
                "failed to initialize thread persistence: thread-store conflict: "
                f"thread {LOCKED_TID} already has an active writer")
        self.assertEqual(
            bridge._codex_thread_lock_conflict(bridge.CodexAppServerError(line)),
            LOCKED_TID)

    def test_lock_without_thread_id_is_still_a_lock(self):
        got = bridge._codex_thread_lock_conflict(
            bridge.CodexAppServerError("thread-store conflict"))
        self.assertEqual(got, "", "是鎖但抓不到 id → 空字串,**不是** None")

    def test_ordinary_errors_are_not_locks(self):
        """誤判會讓真的壞掉的 provider 被講成「桌面 app 佔用」,更難查。"""
        for other in (
            bridge.CodexAppServerError("turn/start timed out"),
            bridge.CodexAppServerError("codex app-server stopped"),
            bridge.CodexAppServerError("thread is busy with another turn",
                                       code=-32600),
            bridge.CodexAppServerError(
                "Invalid request: unknown variant `thread/archive/set`",
                code=-32600),
            RuntimeError("boom"),
        ):
            self.assertIsNone(bridge._codex_thread_lock_conflict(other), str(other))

    def test_ensure_thread_loaded_recodes_the_error(self):
        c = fresh_client(resume_error=locked_error())
        with self.assertRaises(bridge.CodexAppServerError) as ctx:
            run(c.ensure_thread_loaded(LOCKED_TID))
        self.assertEqual(ctx.exception.code, bridge._CX_THREAD_LOCKED_CODE)
        self.assertTrue(c.is_thread_locked(LOCKED_TID))
        self.assertNotIn(LOCKED_TID, c.loaded_threads,
                         "resume 失敗卻留著 loaded 標記 → 之後直接跳過 resume")

    def test_ordinary_resume_failure_does_not_mark_locked(self):
        c = fresh_client(
            resume_error=bridge.CodexAppServerError("thread/resume timed out"))
        with self.assertRaises(bridge.CodexAppServerError):
            run(c.ensure_thread_loaded("t-normal"))
        self.assertFalse(c.is_thread_locked("t-normal"))

    def test_stderr_line_marks_the_thread_in_the_message(self):
        """輔助訊號:stderr 自己就帶 thread id,不必做時間相關性猜測。"""
        c = fresh_client()
        c._note_stderr_thread_lock(REAL_STDERR_1)
        self.assertTrue(c.is_thread_locked(LOCKED_TID))
        c2 = fresh_client()
        c2._note_stderr_thread_lock("2026-08-10T23:16:01Z INFO something normal")
        self.assertEqual(c2.thread_locks, {})

    def test_late_stderr_does_not_relock_a_loaded_thread(self):
        """stderr 是獨立 reader,可能遲到。已經 resume 成功的 thread 不得被打回。"""
        c = fresh_client()
        run(c.ensure_thread_loaded(LOCKED_TID))         # 桌面端已放開
        c._note_stderr_thread_lock(REAL_STDERR_2)       # 遲到的舊訊息
        self.assertFalse(c.is_thread_locked(LOCKED_TID))


class SuppressionTests(unittest.TestCase):
    """5. 抑制窗 —— 實機一天 2 萬多行 codex_app_server_stderr 的止血點。"""

    def test_first_detection_is_fresh_repeats_are_not(self):
        c = fresh_client()
        self.assertTrue(c.note_thread_locked(LOCKED_TID, REAL_RPC_MESSAGE))
        for _ in range(5):
            self.assertFalse(c.note_thread_locked(LOCKED_TID, REAL_RPC_MESSAGE),
                             "窗內重複偵測不該再記 log / 再推卡")
        self.assertEqual(c.thread_lock_info(LOCKED_TID)["attempts"], 6)

    def test_window_expiry_reopens_logging(self):
        c = fresh_client()
        c.note_thread_locked(LOCKED_TID)
        c.thread_locks[LOCKED_TID]["next_retry_at"] = 0.0     # 窗到期
        self.assertTrue(c.note_thread_locked(LOCKED_TID),
                        "窗過了要重新記一次,否則長期鎖住會完全沒有痕跡")

    def test_warm_loop_skips_locked_thread_inside_window(self):
        c = fresh_client(resume_error=locked_error())
        saved, bridge.CODEX_APP = bridge.CODEX_APP, c
        try:
            run(bridge._codex_warm_threads([LOCKED_TID]))
            self.assertEqual(c.call.count("thread/resume"), 1)
            for _ in range(10):           # 清單每刷一次就一輪 = 原本的 retry storm
                run(bridge._codex_warm_threads([LOCKED_TID]))
            self.assertEqual(c.call.count("thread/resume"), 1,
                             "抑制窗內不得重試")
        finally:
            bridge.CODEX_APP = saved

    def test_warm_loop_retries_after_the_window(self):
        c = fresh_client(resume_error=locked_error())
        saved, bridge.CODEX_APP = bridge.CODEX_APP, c
        try:
            run(bridge._codex_warm_threads([LOCKED_TID]))
            c.thread_locks[LOCKED_TID]["next_retry_at"] = 0.0
            run(bridge._codex_warm_threads([LOCKED_TID]))
            self.assertEqual(c.call.count("thread/resume"), 2,
                             "窗過了要再試一次 —— 這一次就是復原探針")
        finally:
            bridge.CODEX_APP = saved

    def test_non_lock_failure_on_a_locked_thread_still_rearms_the_window(self):
        """窗到期後的重試撞上**非鎖**錯誤(逾時、app-server 掉線)也要重新上膛,
        否則 next_retry_at 永遠停在過去 → 每次輪詢都再打一次 = 換個入口的
        retry storm。"""
        c = fresh_client(resume_error=locked_error())
        saved, bridge.CODEX_APP = bridge.CODEX_APP, c
        try:
            run(bridge._codex_warm_threads([LOCKED_TID]))
            c.thread_locks[LOCKED_TID]["next_retry_at"] = 0.0
            c.call.resume_error = bridge.CodexAppServerError("thread/resume timed out")
            run(bridge._codex_warm_threads([LOCKED_TID]))
            self.assertEqual(c.call.count("thread/resume"), 2)
            self.assertFalse(c.thread_lock_retry_due(LOCKED_TID),
                             "非鎖失敗沒有把窗推回去 → 下一輪又會重試")
            for _ in range(5):
                run(bridge._codex_warm_threads([LOCKED_TID]))
            self.assertEqual(c.call.count("thread/resume"), 2)
        finally:
            bridge.CODEX_APP = saved

    def test_warm_loop_still_reports_ordinary_failures(self):
        c = fresh_client(
            resume_error=bridge.CodexAppServerError("thread/resume timed out"))
        saved, bridge.CODEX_APP = bridge.CODEX_APP, c
        try:
            for _ in range(3):
                run(bridge._codex_warm_threads(["t-normal"]))
            self.assertEqual(c.call.count("thread/resume"), 3,
                             "非鎖的失敗不該被抑制窗吃掉")
        finally:
            bridge.CODEX_APP = saved


class CardAndStatusTests(unittest.TestCase):
    """3./4./6. 卡片、狀態欄、復原。"""

    def setUp(self):
        self.saved_app = bridge.CODEX_APP
        self.saved_digests = dict(bridge._CX_CARD_DIGESTS)
        bridge._CX_CARD_DIGESTS.clear()

    def tearDown(self):
        bridge.CODEX_APP = self.saved_app
        bridge._CX_CARD_DIGESTS.clear()
        bridge._CX_CARD_DIGESTS.update(self.saved_digests)

    @staticmethod
    def _texts(d):
        return [(card.get("body") or {}).get("text") or ""
                for card in d.store.snapshot(limit=50)["cards"]]

    def test_error_card_lands_in_that_session_stream(self):
        c = fresh_client(resume_error=locked_error())
        bridge.CODEX_APP = c
        d = bridge._CX_CARD_DIGESTS[LOCKED_TID] = carddigest.CodexThreadDigest()
        other = bridge._CX_CARD_DIGESTS["t-other"] = carddigest.CodexThreadDigest()
        with self.assertRaises(bridge.CodexAppServerError):
            run(c.ensure_thread_loaded(LOCKED_TID))
        self.assertTrue(any("桌面版 Codex/ChatGPT 佔用" in t for t in self._texts(d)),
                        self._texts(d))
        self.assertEqual(other.store.snapshot(limit=20)["cards"], [],
                         "卡片不可以跑到別條 session")

    def test_background_detection_does_not_create_a_digest(self):
        """warm loop 一次掃 20 條:替沒人開過的 thread 憑空建 digest,會讓一堆
        未 seed 的 digest 開始吸 live notification。卡片改由開 session 時補推。"""
        c = fresh_client(resume_error=locked_error())
        bridge.CODEX_APP = c
        run(bridge._codex_warm_threads([LOCKED_TID]))
        self.assertTrue(c.is_thread_locked(LOCKED_TID))
        self.assertNotIn(LOCKED_TID, bridge._CX_CARD_DIGESTS)

    def test_opening_a_locked_session_pushes_the_card(self):
        c = fresh_client(resume_error=locked_error())
        bridge.CODEX_APP = c
        run(bridge._codex_warm_threads([LOCKED_TID]))     # 背景先偵測到
        d = run(bridge._cx_card_digest(LOCKED_TID))       # 使用者現在才進來
        self.assertTrue(any("桌面版 Codex/ChatGPT 佔用" in t for t in self._texts(d)),
                        self._texts(d))
        self.assertTrue(d.store.status.get("locked"))

    def test_error_card_is_not_spammed(self):
        c = fresh_client(resume_error=locked_error())
        bridge.CODEX_APP = c
        d = bridge._CX_CARD_DIGESTS[LOCKED_TID] = carddigest.CodexThreadDigest()
        for _ in range(4):
            with self.assertRaises(bridge.CodexAppServerError):
                run(c.ensure_thread_loaded(LOCKED_TID))
        run(bridge._cx_card_digest(LOCKED_TID))           # 再開一次 session
        locked_cards = [c2 for c2 in d.store.snapshot(limit=50)["cards"]
                        if (c2.get("body") or {}).get("error_code") == "CX_THREAD_LOCKED"]
        self.assertEqual(len(locked_cards), 1, "同一次鎖定事件只該有一張卡")

    def test_card_stream_status_exposes_locked(self):
        c = fresh_client(resume_error=locked_error())
        bridge.CODEX_APP = c
        bridge._CX_CARD_DIGESTS[LOCKED_TID] = carddigest.CodexThreadDigest()
        with self.assertRaises(bridge.CodexAppServerError):
            run(c.ensure_thread_loaded(LOCKED_TID))
        status = bridge._CX_CARD_DIGESTS[LOCKED_TID].store.status
        self.assertTrue(status.get("locked"))
        self.assertEqual(status.get("lock_reason"), "thread_store_conflict")
        self.assertIn("桌面版 Codex/ChatGPT 佔用", status.get("lock_message") or "")
        self.assertIn("送不出去", status.get("label") or "",
                      "label 還講「閒置」的話,使用者會以為一切正常")

    def test_unlocked_by_default(self):
        d = carddigest.CodexThreadDigest()
        d._status()
        self.assertIs(d.store.status.get("locked"), False,
                      "locked 必須恆存在,app 才能無條件讀它")

    def test_summary_exposes_locked_and_recovers(self):
        c = fresh_client(resume_error=locked_error())
        bridge.CODEX_APP = c
        bridge._CX_CARD_DIGESTS[LOCKED_TID] = carddigest.CodexThreadDigest()
        with self.assertRaises(bridge.CodexAppServerError):
            run(c.ensure_thread_loaded(LOCKED_TID))
        summary = bridge._codex_enrich_summary({"thread_id": LOCKED_TID})
        self.assertTrue(summary["locked"])
        self.assertEqual(summary["lockReason"], "thread_store_conflict")
        self.assertEqual(summary["lockMessage"], bridge.CX_THREAD_LOCKED_MESSAGE)

        # 6. 桌面 app 放開 → 下一次 resume 成功 → 旗標翻回來,不需重啟 bridge。
        c.call.resume_error = None
        run(c.ensure_thread_loaded(LOCKED_TID))
        self.assertFalse(c.is_thread_locked(LOCKED_TID))
        after = bridge._codex_enrich_summary({"thread_id": LOCKED_TID})
        self.assertFalse(after["locked"])
        self.assertNotIn("lockReason", after)
        d = bridge._CX_CARD_DIGESTS[LOCKED_TID]
        self.assertIs(d.store.status.get("locked"), False,
                      "卡片流的 banner 旗標也要翻回來")
        texts = [(card.get("body") or {}).get("text") or ""
                 for card in d.store.snapshot(limit=20)["cards"]]
        self.assertTrue(any("已釋放" in t for t in texts), texts)

    def test_queue_drop_card_names_the_lock(self):
        """排隊中的訊息因為鎖而被丟掉時,卡片要講原因而不是 exception 名字。"""
        bridge.CODEX_APP = fresh_client()
        d = bridge._CX_CARD_DIGESTS[LOCKED_TID] = carddigest.CodexThreadDigest()
        bridge._cx_feed_queue_drop(LOCKED_TID, {"text": "在嗎"}, locked_error())
        texts = [(card.get("body") or {}).get("text") or ""
                 for card in d.store.snapshot(limit=20)["cards"]]
        self.assertTrue(any("桌面版 Codex/ChatGPT 佔用" in t for t in texts), texts)


class RecoveryProbeTests(unittest.IsolatedAsyncioTestCase):
    """4. 復原探針 —— 使用者只是盯著 banner、沒再送任何東西時的解鎖路徑。"""

    def setUp(self):
        self.saved_app = bridge.CODEX_APP
        self.saved_digests = dict(bridge._CX_CARD_DIGESTS)
        bridge._CX_CARD_DIGESTS.clear()

    def tearDown(self):
        bridge.CODEX_APP = self.saved_app
        bridge._CX_CARD_DIGESTS.clear()
        bridge._CX_CARD_DIGESTS.update(self.saved_digests)

    async def _drain(self):
        for _ in range(4):
            await asyncio.sleep(0)

    async def test_no_probe_inside_the_window(self):
        c = fresh_client(resume_error=locked_error())
        bridge.CODEX_APP = c
        c.note_thread_locked(LOCKED_TID)
        bridge._codex_lock_recheck(LOCKED_TID)
        await self._drain()
        self.assertEqual(c.call.count("thread/resume"), 0, "窗內不得再打 resume")

    async def test_probe_after_window_clears_the_flag(self):
        c = fresh_client()          # 桌面 app 已放開 → resume 會成功
        bridge.CODEX_APP = c
        c.note_thread_locked(LOCKED_TID)
        c.thread_locks[LOCKED_TID]["next_retry_at"] = 0.0
        bridge._codex_lock_recheck(LOCKED_TID)
        await self._drain()
        self.assertEqual(c.call.count("thread/resume"), 1)
        self.assertFalse(c.is_thread_locked(LOCKED_TID),
                         "桌面端放開之後 banner 必須自己消失(不必重啟 bridge)")

    async def test_probe_is_a_noop_for_unlocked_threads(self):
        c = fresh_client()
        bridge.CODEX_APP = c
        bridge._codex_lock_recheck("t-free")
        await self._drain()
        self.assertEqual(c.call.calls, [])


class HttpTests(unittest.TestCase):
    """2. 兩條 input 路由都要 409 + zh-TW 人話(不能再是 CX_TURN_IN_FLIGHT)。"""

    def setUp(self):
        from fastapi.testclient import TestClient
        reset_auth_throttle()
        self.auth = {"Authorization": "Bearer " + bridge.BRIDGE_TOKEN}
        self.saved_app = bridge.CODEX_APP
        self.saved_digests = dict(bridge._CX_CARD_DIGESTS)
        bridge._CX_CARD_DIGESTS.clear()
        self.client = fresh_client(resume_error=locked_error())
        bridge.CODEX_APP = self.client
        self.http = TestClient(bridge.app)

    def tearDown(self):
        bridge.CODEX_APP = self.saved_app
        bridge._CX_CARD_DIGESTS.clear()
        bridge._CX_CARD_DIGESTS.update(self.saved_digests)
        reset_auth_throttle()

    def _assert_locked_response(self, r):
        self.assertEqual(r.status_code, 409, r.text)
        err = r.json().get("error") or {}
        self.assertEqual(err.get("code"), "CX_THREAD_LOCKED", r.text)
        self.assertEqual(err.get("message"), bridge.CX_THREAD_LOCKED_MESSAGE)
        self.assertIn("thread 寫入鎖", err.get("message"))
        self.assertEqual(r.headers.get("X-Error-Code"), "CX_THREAD_LOCKED")

    def test_v1_input_returns_409(self):
        r = self.http.post(f"/codexsessions/{LOCKED_TID}/input",
                           json={"text": "在嗎", "client_id": "cid-v1"},
                           headers=self.auth)
        self._assert_locked_response(r)

    def test_v2_input_returns_409(self):
        r = self.http.post(f"/app/v2/sessions/codex:{LOCKED_TID}/input",
                           json={"content": "在嗎", "client_id": "cid-v2"},
                           headers=self.auth)
        self._assert_locked_response(r)

    def test_v1_input_also_pushes_the_card(self):
        self.http.post(f"/codexsessions/{LOCKED_TID}/input",
                       json={"text": "在嗎", "client_id": "cid-card"},
                       headers=self.auth)
        d = bridge._CX_CARD_DIGESTS.get(LOCKED_TID)
        self.assertIsNotNone(d)
        texts = [(card.get("body") or {}).get("text") or ""
                 for card in d.store.snapshot(limit=20)["cards"]]
        self.assertTrue(any("桌面版 Codex/ChatGPT 佔用" in t for t in texts), texts)

    def test_ordinary_error_is_not_reported_as_locked(self):
        self.client.call.resume_error = bridge.CodexAppServerError(
            "thread is busy with another turn", code=-32600)
        r = self.http.post(f"/codexsessions/{LOCKED_TID}/input",
                           json={"text": "在嗎", "client_id": "cid-other"},
                           headers=self.auth)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual((r.json().get("error") or {}).get("code"),
                         "CX_TURN_IN_FLIGHT", "非鎖的 -32600 行為不得改變")

    def test_status_endpoint_exposes_locked(self):
        self.client.note_thread_locked(LOCKED_TID, REAL_RPC_MESSAGE)
        r = self.http.get(f"/codexsessions/{LOCKED_TID}/status", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        session = r.json()["session"]
        self.assertTrue(session["locked"])
        self.assertEqual(session["lockReason"], "thread_store_conflict")
        self.assertEqual(session["lockMessage"], bridge.CX_THREAD_LOCKED_MESSAGE)

    def test_status_endpoint_reports_unlocked_thread(self):
        r = self.http.get("/codexsessions/t-free/status", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIs(r.json()["session"]["locked"], False)


if __name__ == "__main__":
    unittest.main()
