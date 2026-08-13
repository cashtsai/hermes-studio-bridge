"""CX 狀態顯示的根因修復 —— provider(codex app-server)才是權威來源。

症狀:Pocket 上 Codex 的狀態幾乎永遠是 idle。四層獨立缺陷:

1. `runtime_status()` 只認 raw 值 {"completed","done","success"},而 **codex
   從來不吐這三個**。它的 ThreadStatus 只有
   `notLoaded | idle | systemError | active{activeFlags}`,於是 `active` 與
   `systemError` 全部掉到最後的 `return "idle"`。
2. `is_active()` 只讀 `active_turns`,那是「Pocket 自己發起、且 bridge 一重啟
   就清空」的記憶 → 桌面版/VS Code/CLI 開的 thread 一律看不見。
3. `_handle_notification` 沒有處理 `thread/status/changed`(權威通知),
   也從來沒呼叫過 `thread/loaded/list`。
4. `/app/v2/sessions` 的 codex 列自己重算狀態,把 failed/stalled/done 壓成
   waiting_approval/running/idle 三選一。

實測依據(codex-cli 0.147.0,隔離 CODEX_HOME,兩條連線接同一顆 app-server):
  * `thread/status/changed` 是**全連線廣播** —— 沒有 start/resume 過該 thread
    的連線同樣收到完整的 active → idle / active → systemError。
  * 只在狀態**真的轉變**時送,同狀態不重送。
  * `thread/list` 對沒載入的 thread 一律回 `{"type":"notLoaded"}`,載入中的
    才回真實狀態 → notLoaded 必須當成「沒有資訊」,不能當 idle。
"""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# import bridge 會在模組層建 AgentRegistry 等物件,務必先把 DB/家目錄導到 tmp
# (run-tests.sh 已經做了一份;單跑這支時這裡是保險)。
_TMP = tempfile.mkdtemp(prefix="cx-provider-status-")
os.environ.setdefault("POCKET_REGISTRY_DB", os.path.join(_TMP, "registry.db"))
os.environ.setdefault("HARNESS_DB", os.path.join(_TMP, "harness.db"))
os.environ.setdefault("POCKET_CODEX_HOME", os.path.join(_TMP, "codex-home"))
os.environ.setdefault("OPENCLAW_CONFIG_FILE", os.path.join(_TMP, "openclaw.json"))

import bridge  # noqa: E402

TID = "019ff8aa-e70f-7c23-b68d-fab565cda0c4"


def _fresh_client():
    """乾淨的 client 實例,不碰模組層那顆 CODEX_APP。"""
    return bridge.CodexAppServerClient()


def _reset(app):
    app.active_turns.clear()
    app.provider_status.clear()
    app.thread_errors.clear()
    app.turn_terminal_at.clear()
    app.turn_started_at.clear()
    app.last_event_at.clear()
    app.pending_approvals.clear()
    app.pending_approvals_by_thread.clear()


# ── provider 狀態的七種可能(None = 完全沒收到過) ──────────────────────
PROVIDER_CASES = {
    "none": None,
    "notLoaded": {"type": "notLoaded"},
    "idle": {"type": "idle"},
    "active": {"type": "active", "activeFlags": []},
    "active_approval": {"type": "active", "activeFlags": ["waitingOnApproval"]},
    "active_user_input": {"type": "active", "activeFlags": ["waitingOnUserInput"]},
    "systemError": {"type": "systemError"},
}


def expected_status(approval, bridge_active, provider, local, raw):
    """把**文件上的優先序**寫成一張規則表(順序即優先序)。

    刻意用「有序規則清單」而不是 if/elif 串,寫法與實作不同 → 實作若漂移,
    這張表會抓到。優先序來源:runtime_status() 的 docstring 與契約 §4.1a。
        1. 本地 pending approval
        2. bridge 自知的 active turn(逾時 → stalled)
        3. provider 權威狀態(active / systemError;notLoaded、idle 不下結論)
        4. 本地錯誤 / 終局
        5. 舊式 raw 字串
        6. idle
    """
    prov = PROVIDER_CASES[provider] or {}
    ptype = prov.get("type")
    flags = prov.get("flags") or prov.get("activeFlags") or []
    rules = [
        (approval, "waiting_approval"),
        (bridge_active == "stale", "stalled"),
        (bridge_active == "fresh", "running"),
        (ptype == "active" and ("waitingOnApproval" in flags
                                or "waitingOnUserInput" in flags),
         "waiting_approval"),
        (ptype == "active", "running"),
        (ptype == "systemError", "failed"),
        ("error" in local, "failed"),
        ("terminal" in local, "done"),
        (raw in ("completed", "done", "success"), "done"),
        (True, "idle"),
    ]
    for cond, result in rules:
        if cond:
            return result
    raise AssertionError("unreachable")


class PrecedenceTableTests(unittest.TestCase):
    """整張優先序表:每一種組合都要對(2×3×7×4×2 = 336 組)。"""

    def test_every_combination(self):
        app = _fresh_client()
        checked = 0
        seen_results = set()
        for approval in (False, True):
            for bridge_active in ("none", "fresh", "stale"):
                for provider in PROVIDER_CASES:
                    for local in ("clean", "error", "terminal",
                                  "error+terminal"):
                        for raw in ("", "completed"):
                            _reset(app)
                            if approval:
                                rec = {"id": "a1", "thread_id": TID}
                                app.pending_approvals["a1"] = rec
                                app.pending_approvals_by_thread[TID]["a1"] = rec
                            if bridge_active == "fresh":
                                app.active_turns[TID] = "turn-1"
                                app.last_event_at[TID] = bridge.time.time()
                            elif bridge_active == "stale":
                                app.active_turns[TID] = "turn-1"
                                app.last_event_at[TID] = (
                                    bridge.time.time()
                                    - bridge.CODEX_TURN_STALL_SECS - 5)
                            if PROVIDER_CASES[provider] is not None:
                                app.note_provider_status(
                                    TID, PROVIDER_CASES[provider])
                            if "error" in local:
                                app.thread_errors[TID] = "boom"
                            if "terminal" in local:
                                app.turn_terminal_at[TID] = bridge.time.time()

                            want = expected_status(approval, bridge_active,
                                                   provider, local, raw)
                            got = app.runtime_status(TID, raw)
                            self.assertEqual(
                                got, want,
                                f"approval={approval} active={bridge_active} "
                                f"provider={provider} local={local} raw={raw!r}")
                            checked += 1
                            seen_results.add(got)
        self.assertEqual(checked, 2 * 3 * 7 * 4 * 2)
        # 六個狀態都要真的被走到過,否則這張表等於沒驗到。
        self.assertEqual(seen_results,
                         {"idle", "running", "waiting_approval", "failed",
                          "stalled", "done"})

    def test_provider_active_is_never_silently_dropped(self):
        """本次事故的核心:provider 說 active,結果報 idle。"""
        app = _fresh_client()
        _reset(app)
        app.note_provider_status(TID, {"type": "active", "activeFlags": []})
        self.assertEqual(app.runtime_status(TID, ""), "running")

    def test_system_error_maps_to_failed(self):
        app = _fresh_client()
        _reset(app)
        app.note_provider_status(TID, {"type": "systemError"})
        self.assertEqual(app.runtime_status(TID, ""), "failed")

    def test_not_loaded_is_no_information_not_idle(self):
        """notLoaded 不得蓋掉本地已知的 failed/done。"""
        app = _fresh_client()
        _reset(app)
        app.thread_errors[TID] = "boom"
        app.note_provider_status(TID, {"type": "notLoaded"})
        self.assertEqual(app.runtime_status(TID, ""), "failed")

    def test_provider_idle_does_not_clobber_local_error(self):
        app = _fresh_client()
        _reset(app)
        app.thread_errors[TID] = "boom"
        app.note_provider_status(TID, {"type": "idle"})
        self.assertEqual(app.runtime_status(TID, ""), "failed")

    def test_active_flags_surface_as_waiting_approval(self):
        for flag in ("waitingOnApproval", "waitingOnUserInput"):
            app = _fresh_client()
            _reset(app)
            app.note_provider_status(TID, {"type": "active",
                                           "activeFlags": [flag]})
            self.assertEqual(app.runtime_status(TID, ""), "waiting_approval",
                             flag)

    def test_provider_active_beats_local_terminal_state(self):
        """provider 說在跑,就算本地記得上一輪已結束也要以 provider 為準。"""
        app = _fresh_client()
        _reset(app)
        app.turn_terminal_at[TID] = bridge.time.time()
        app.note_provider_status(TID, {"type": "active", "activeFlags": []})
        self.assertEqual(app.runtime_status(TID, ""), "running")


class RestartSurvivalTests(unittest.TestCase):
    """bridge 重啟 = active_turns 全空。只要 provider 還說 active,就得照報。"""

    def test_status_survives_bridge_restart(self):
        app = _fresh_client()
        _reset(app)
        # 重啟前:Pocket 自己發起的回合
        app.active_turns[TID] = "turn-1"
        app.last_event_at[TID] = bridge.time.time()
        app.note_provider_status(TID, {"type": "active", "activeFlags": []})
        self.assertEqual(app.runtime_status(TID, ""), "running")

        # 模擬重啟:bridge 的記憶清空,provider 的說法由 align 重新灌回
        app.active_turns.clear()
        app.last_event_at.clear()
        app.turn_started_at.clear()
        self.assertEqual(app.runtime_status(TID, ""), "running",
                         "重啟後 provider 仍說 active,卻報成 idle")

    def test_without_provider_status_restart_falls_back_to_idle(self):
        """對照組:沒有 provider 資訊時(舊行為)確實只能是 idle。"""
        app = _fresh_client()
        _reset(app)
        self.assertEqual(app.runtime_status(TID, ""), "idle")


class NotificationTests(unittest.TestCase):
    """thread/status/changed 要真的更新 provider_status。"""

    def _notify(self, app, status):
        app._handle_notification({
            "method": "thread/status/changed",
            "params": {"threadId": TID, "status": status},
        })

    def test_notification_updates_map(self):
        app = _fresh_client()
        _reset(app)
        self._notify(app, {"type": "active", "activeFlags": []})
        rec = app.provider_status_for(TID)
        self.assertEqual(rec["type"], "active")
        self.assertEqual(rec["flags"], [])
        self.assertEqual(app.runtime_status(TID, ""), "running")

    def test_notification_carries_active_flags(self):
        app = _fresh_client()
        _reset(app)
        self._notify(app, {"type": "active",
                           "activeFlags": ["waitingOnApproval"]})
        self.assertEqual(app.provider_status_for(TID)["flags"],
                         ["waitingOnApproval"])
        self.assertEqual(app.runtime_status(TID, ""), "waiting_approval")

    def test_full_observed_lifecycle(self):
        """實測到的兩條真實序列:active → idle,以及 active → systemError。"""
        app = _fresh_client()
        _reset(app)
        self._notify(app, {"type": "active", "activeFlags": []})
        self.assertEqual(app.runtime_status(TID, ""), "running")
        self._notify(app, {"type": "idle"})
        self.assertEqual(app.runtime_status(TID, ""), "idle")

        _reset(app)
        self._notify(app, {"type": "active", "activeFlags": []})
        self._notify(app, {"type": "systemError"})
        self.assertEqual(app.runtime_status(TID, ""), "failed")

    def test_malformed_status_is_ignored(self):
        app = _fresh_client()
        _reset(app)
        for bad in (None, "active", {}, {"type": None}, {"type": ""}, 7):
            self.assertIsNone(app.note_provider_status(TID, bad), repr(bad))
        self.assertEqual(app.provider_status, {})
        # threadId 缺席也不能炸
        app._handle_notification({"method": "thread/status/changed",
                                  "params": {"status": {"type": "idle"}}})
        self.assertEqual(app.provider_status, {})

    def test_map_is_bounded(self):
        app = _fresh_client()
        _reset(app)
        for i in range(bridge.CODEX_PROVIDER_STATUS_MAX + 200):
            app.note_provider_status(f"t{i:05d}", {"type": "idle"})
        self.assertLessEqual(len(app.provider_status),
                             bridge.CODEX_PROVIDER_STATUS_MAX)
        # 最新的一定還在(被淘汰的是最舊的)
        last = f"t{bridge.CODEX_PROVIDER_STATUS_MAX + 199:05d}"
        self.assertIn(last, app.provider_status)

    def test_disconnect_clears_provider_status(self):
        """連線斷了,provider 的說法就失效 —— 不能讓 active 永遠掛著。"""
        app = _fresh_client()
        _reset(app)
        app.active_turns[TID] = "turn-1"
        app.note_provider_status(TID, {"type": "active", "activeFlags": []})
        asyncio.run(app._reader_cleanup())
        self.assertEqual(app.provider_status, {})
        # 掉線時本地會把進行中的回合標成錯誤 → failed(不是繼續 running)
        self.assertEqual(app.runtime_status(TID, ""), "failed")

    def test_disconnect_without_active_turn_falls_back_to_idle(self):
        app = _fresh_client()
        _reset(app)
        app.note_provider_status(TID, {"type": "active", "activeFlags": []})
        asyncio.run(app._reader_cleanup())
        self.assertEqual(app.runtime_status(TID, ""), "idle")


class AlignOnStartTests(unittest.TestCase):
    """開機 / 每次重連要用 thread/loaded/list + thread/list 對齊一次。"""

    def _client_with_calls(self, responses):
        app = _fresh_client()
        seen = []

        async def fake_call(method, params=None, timeout=30.0):
            seen.append((method, params))
            if method in responses:
                out = responses[method]
                if isinstance(out, Exception):
                    raise out
                return out
            raise AssertionError(f"unexpected call {method}")

        app.call = fake_call
        return app, seen

    def test_align_pulls_loaded_list_and_statuses(self):
        app, seen = self._client_with_calls({
            "thread/loaded/list": {"data": [TID], "nextCursor": None},
            "thread/list": {"data": [
                {"id": TID, "status": {"type": "active", "activeFlags": []}},
                {"id": "other", "status": {"type": "notLoaded"}},
            ]},
        })
        n = asyncio.run(app.align_provider_status())
        self.assertEqual(n, 1)
        self.assertEqual([m for m, _ in seen],
                         ["thread/loaded/list", "thread/list"])
        self.assertEqual(app.runtime_status(TID, ""), "running")
        self.assertIn(TID, app.loaded_threads)
        # 沒 loaded 的那條不該被灌進來
        self.assertNotIn("other", app.provider_status)

    def test_align_is_the_restart_survival_path(self):
        """重啟後 active_turns 是空的,狀態完全靠 align 撈回來。"""
        app, _ = self._client_with_calls({
            "thread/loaded/list": {"data": [TID]},
            "thread/list": {"data": [
                {"id": TID, "status": {"type": "active",
                                       "activeFlags": ["waitingOnApproval"]}}]},
        })
        self.assertEqual(app.runtime_status(TID, ""), "idle")   # 對齊之前
        asyncio.run(app.align_provider_status())
        self.assertFalse(app.active_turns)
        self.assertEqual(app.runtime_status(TID, ""), "waiting_approval")

    def test_align_reads_threads_missing_from_thread_list(self):
        """實測:剛開、還沒落地的 thread **不會出現在 thread/list**,但它就在
        loaded/list 裡 —— 而那正是最可能「正在跑」的一條。要用 thread/read 補。"""
        app, seen = self._client_with_calls({
            "thread/loaded/list": {"data": [TID]},
            "thread/list": {"data": []},          # 還沒落地 → 清單裡沒有
            "thread/read": {"thread": {
                "id": TID, "status": {"type": "active", "activeFlags": []}}},
        })
        self.assertEqual(asyncio.run(app.align_provider_status()), 1)
        self.assertEqual([m for m, _ in seen],
                         ["thread/loaded/list", "thread/list", "thread/read"])
        self.assertEqual(app.runtime_status(TID, ""), "running")

    def test_align_does_not_read_threads_already_covered(self):
        """thread/list 給得到狀態的就不再多打一發 thread/read。"""
        app, seen = self._client_with_calls({
            "thread/loaded/list": {"data": [TID]},
            "thread/list": {"data": [
                {"id": TID, "status": {"type": "idle"}}]},
        })
        self.assertEqual(asyncio.run(app.align_provider_status()), 1)
        self.assertNotIn("thread/read", [m for m, _ in seen])

    def test_align_read_failure_is_survivable(self):
        app, _ = self._client_with_calls({
            "thread/loaded/list": {"data": [TID]},
            "thread/list": {"data": []},
            "thread/read": bridge.CodexAppServerError("boom"),
        })
        self.assertEqual(asyncio.run(app.align_provider_status()), 0)

    def test_align_read_fanout_is_bounded(self):
        many = [f"t{i:04d}" for i in range(bridge.CODEX_STATUS_ALIGN_READ_MAX + 25)]
        app, seen = self._client_with_calls({
            "thread/loaded/list": {"data": many},
            "thread/list": {"data": []},
            "thread/read": {"thread": {"status": {"type": "idle"}}},
        })
        asyncio.run(app.align_provider_status())
        reads = [m for m, _ in seen if m == "thread/read"]
        self.assertEqual(len(reads), bridge.CODEX_STATUS_ALIGN_READ_MAX)

    def test_align_survives_loaded_list_failure(self):
        app, _ = self._client_with_calls({
            "thread/loaded/list": bridge.CodexAppServerError("nope"),
        })
        self.assertEqual(asyncio.run(app.align_provider_status()), 0)

    def test_align_survives_thread_list_failure(self):
        app, _ = self._client_with_calls({
            "thread/loaded/list": {"data": [TID]},
            "thread/list": bridge.CodexAppServerError("nope"),
        })
        self.assertEqual(asyncio.run(app.align_provider_status()), 0)
        self.assertIn(TID, app.loaded_threads)

    def test_align_noop_when_nothing_loaded(self):
        app, seen = self._client_with_calls({"thread/loaded/list": {"data": []}})
        self.assertEqual(asyncio.run(app.align_provider_status()), 0)
        self.assertEqual([m for m, _ in seen], ["thread/loaded/list"])


class ThreadListFeedsMapTests(unittest.TestCase):
    """thread/list 每刷一次就順手更新 provider_status(通知之外的第二條保險)。"""

    def test_session_summary_records_provider_status(self):
        bridge.CODEX_APP.provider_status.pop(TID, None)
        try:
            bridge._codex_session_summary(
                {"id": TID, "status": {"type": "active",
                                       "activeFlags": ["waitingOnUserInput"]}})
            rec = bridge.CODEX_APP.provider_status_for(TID)
            self.assertEqual(rec["type"], "active")
            self.assertEqual(rec["flags"], ["waitingOnUserInput"])
        finally:
            bridge.CODEX_APP.provider_status.pop(TID, None)


class IsoTimestampTests(unittest.TestCase):
    """契約 §4.1 的 last_event_at 是 ISO8601 字串;送 float 的話 app 解出 nil。"""

    def test_epoch_to_iso(self):
        self.assertEqual(bridge._v2_iso_utc(1786000000.0),
                         "2026-08-06T07:06:40Z")

    def test_passthrough_and_none(self):
        self.assertEqual(bridge._v2_iso_utc("2026-07-04T10:33:42Z"),
                         "2026-07-04T10:33:42Z")
        self.assertIsNone(bridge._v2_iso_utc(None))
        self.assertIsNone(bridge._v2_iso_utc(""))
        # 字串一律原樣放行(呼叫端可能已經是 ISO)
        self.assertEqual(bridge._v2_iso_utc("nonsense"), "nonsense")

    def test_garbage_does_not_raise(self):
        self.assertIsNone(bridge._v2_iso_utc(object()))
        self.assertIsNone(bridge._v2_iso_utc(float("nan")))


class V2SessionRowTests(unittest.TestCase):
    """/app/v2/sessions 的 codex 列不得再把 failed/stalled/done 壓平。"""

    def _rows(self, threads, prepare=None):
        app = bridge.CODEX_APP
        saved = (dict(app.provider_status), dict(app.active_turns),
                 dict(app.thread_errors), dict(app.turn_terminal_at),
                 dict(app.last_event_at))
        app.provider_status.clear(); app.active_turns.clear()
        app.thread_errors.clear(); app.turn_terminal_at.clear()
        app.last_event_at.clear()
        if prepare:
            prepare(app)

        async def fake_visible(wanted=20):
            return threads

        async def fake_delegations():
            return []

        try:
            with patch.object(bridge, "_check_auth", return_value=None), \
                 patch.object(bridge, "_delegation_v2_sessions", fake_delegations), \
                 patch.object(bridge, "_cc_conf_rows", lambda: []), \
                 patch.object(bridge, "_hermes_pending_by_session", lambda: {}), \
                 patch.object(bridge, "PERSONAS", {}), \
                 patch.object(bridge, "_delegated_codex_thread_ids",
                              lambda: set()), \
                 patch.object(bridge, "_codex_v2_visible_threads", fake_visible), \
                 patch.object(bridge.OPENCLAW, "configured", lambda: False):
                res = asyncio.run(bridge.v2_sessions(object()))
        finally:
            (app.provider_status, app.active_turns, app.thread_errors,
             app.turn_terminal_at, app.last_event_at) = (
                dict(saved[0]), dict(saved[1]), dict(saved[2]),
                dict(saved[3]), dict(saved[4]))
        return {r["id"]: r for r in res["sessions"]}

    def test_provider_active_thread_reports_running(self):
        rows = self._rows([{"id": TID, "cwd": "/tmp",
                            "status": {"type": "active", "activeFlags": []}}])
        self.assertEqual(rows[f"codex:{TID}"]["status"], "running")

    def test_system_error_reaches_the_app_as_failed(self):
        """以前:壓成 idle。"""
        rows = self._rows([{"id": TID, "cwd": "/tmp",
                            "status": {"type": "systemError"}}])
        self.assertEqual(rows[f"codex:{TID}"]["status"], "failed")

    def test_stalled_is_not_flattened(self):
        def prep(app):
            app.active_turns[TID] = "turn-1"
            app.last_event_at[TID] = (bridge.time.time()
                                      - bridge.CODEX_TURN_STALL_SECS - 5)

        rows = self._rows([{"id": TID, "cwd": "/tmp",
                            "status": {"type": "idle"}}], prep)
        self.assertEqual(rows[f"codex:{TID}"]["status"], "stalled")

    def test_done_is_not_flattened(self):
        def prep(app):
            app.turn_terminal_at[TID] = bridge.time.time()

        rows = self._rows([{"id": TID, "cwd": "/tmp",
                            "status": {"type": "notLoaded"}}], prep)
        self.assertEqual(rows[f"codex:{TID}"]["status"], "done")

    def test_failed_is_not_flattened(self):
        def prep(app):
            app.thread_errors[TID] = "boom"

        rows = self._rows([{"id": TID, "cwd": "/tmp",
                            "status": {"type": "idle"}}], prep)
        self.assertEqual(rows[f"codex:{TID}"]["status"], "failed")

    def test_last_event_at_is_iso_not_float(self):
        def prep(app):
            app.last_event_at[TID] = 1786000000.0

        rows = self._rows([{"id": TID, "cwd": "/tmp",
                            "status": {"type": "idle"}}], prep)
        self.assertEqual(rows[f"codex:{TID}"]["last_event_at"],
                         "2026-08-06T07:06:40Z")

    def test_last_event_at_falls_back_to_provider_updated_at(self):
        """外部(桌面版/CLI)開的 thread 沒有本地 lastEventAt,不能就這樣空白。"""
        rows = self._rows([{"id": TID, "cwd": "/tmp", "updatedAt": 1786000000,
                            "status": {"type": "idle"}}])
        self.assertEqual(rows[f"codex:{TID}"]["last_event_at"],
                         "2026-08-06T07:06:40Z")

    def test_local_last_event_at_wins_over_updated_at(self):
        def prep(app):
            app.last_event_at[TID] = 1786000000.0

        rows = self._rows([{"id": TID, "cwd": "/tmp", "updatedAt": 1,
                            "status": {"type": "idle"}}], prep)
        self.assertEqual(rows[f"codex:{TID}"]["last_event_at"],
                         "2026-08-06T07:06:40Z")

    def test_status_is_in_the_documented_enum(self):
        legal = {"idle", "running", "waiting_approval", "failed", "stalled",
                 "done"}
        for status in ({"type": "active", "activeFlags": []},
                       {"type": "active", "activeFlags": ["waitingOnApproval"]},
                       {"type": "systemError"}, {"type": "idle"},
                       {"type": "notLoaded"}):
            rows = self._rows([{"id": TID, "cwd": "/tmp", "status": status}])
            self.assertIn(rows[f"codex:{TID}"]["status"], legal, str(status))


class EnrichSummaryTests(unittest.TestCase):
    """v1 codexsessions 面靠 activeTurn 推 lifecycle,也得吃到 provider 真相。"""

    def _enrich(self, status, prepare=None):
        app = bridge.CODEX_APP
        saved = (dict(app.provider_status), dict(app.active_turns),
                 dict(app.last_event_at))
        app.provider_status.clear(); app.active_turns.clear()
        app.last_event_at.clear()
        try:
            if prepare:
                prepare(app)
            return bridge._codex_enrich_summary(
                bridge._codex_session_summary({"id": TID, "status": status}))
        finally:
            (app.provider_status, app.active_turns, app.last_event_at) = (
                dict(saved[0]), dict(saved[1]), dict(saved[2]))

    def test_active_turn_true_when_provider_says_active(self):
        s = self._enrich({"type": "active", "activeFlags": []})
        self.assertTrue(s["activeTurn"])
        self.assertEqual(s["runtimeStatus"], "running")
        self.assertEqual(s["status"], "running")

    def test_active_turn_false_when_idle(self):
        s = self._enrich({"type": "idle"})
        self.assertFalse(s["activeTurn"])
        self.assertEqual(s["status"], "idle")

    def test_provider_status_field_is_preserved(self):
        """原始 provider 型別不能被 runtimeStatus 蓋掉(診斷要看得到)。"""
        s = self._enrich({"type": "systemError"})
        self.assertEqual(s["providerStatus"], "systemError")
        self.assertEqual(s["status"], "failed")


class WarmFailureLoggingTests(unittest.TestCase):
    """warm 失敗的 log 一定要帶得到人話 —— 實機曾累積上萬筆只有 error type
    的紀錄,完全無法診斷。"""

    def _warm_and_capture(self, exc):
        events = []

        async def boom(tid, cwd=None):
            raise exc

        with patch.object(bridge, "_log_event",
                          lambda name, **kw: events.append((name, kw))), \
             patch.object(bridge.CODEX_APP, "ensure_thread_loaded", boom), \
             patch.object(bridge.CODEX_APP, "spawned_bin", "/usr/bin/codex"), \
             patch.object(bridge.CODEX_APP, "loaded_threads", set()), \
             patch.object(bridge.CODEX_APP, "thread_locks", {}):
            asyncio.run(bridge._codex_warm_threads([TID]))
        return [kw for name, kw in events if name == "codex_thread_warm_failed"]

    def test_app_server_error_logs_message(self):
        err = bridge.CodexAppServerError("thread/resume timed out", code=-32000)
        got = self._warm_and_capture(err)
        self.assertEqual(len(got), 1)
        self.assertIn("timed out", got[0]["error_message"])
        self.assertEqual(got[0]["code"], -32000)

    def test_generic_exception_logs_message(self):
        """以前這條分支只記 error type,訊息整個丟掉。"""
        got = self._warm_and_capture(RuntimeError("no rollout found for thread"))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["error"], "RuntimeError")
        self.assertIn("no rollout", got[0]["error_message"])

    def test_message_is_length_bounded(self):
        got = self._warm_and_capture(RuntimeError("x" * 5000))
        self.assertLessEqual(len(got[0]["error_message"]), 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
