"""CX 設定面板:讀得回來、枚舉跟得上 codex、參數錯不要謊報忙碌。

背景(2026-08-14 對 codex-cli 0.147.0 隔離實測):
  • `thread/settings/update` 存在且生效 —— 寫入從來不是問題。
  • 但 app-server **沒有 settings getter**,`thread/read` 的回覆也不帶設定欄。
    舊版讀 `thread["model"]` 永遠 None → 面板永遠空白 → 看起來像「切模型
    沒生效」。這才是真症狀。
  • `on-failure` 在 0.147 已從 approvalPolicy 枚舉移除;送出去會拿到 -32600,
    而 -32600 舊路徑一律翻成「上一輪正在跑」——語意相反。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="codex-settings-canon-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

TID = "019ffe50-8072-74c0-9e71-e4082e247861"


class FakeRequest:
    def __init__(self, body=None):
        self._body = body or {}

    async def json(self):
        return self._body


class FakeCodexApp:
    """只實作設定面板會碰到的表面。"""

    def __init__(self, resume_result=None, update_error=None):
        self.thread_settings = {}
        self.loaded_threads = set()
        self.calls = []
        self._resume_result = resume_result or {}
        self._update_error = update_error

    # 真物件的方法,直接借用 —— 正規化邏輯本身就是受測目標之一。
    note_thread_settings = bridge.CodexAppServerClient.note_thread_settings

    async def ensure_thread_loaded(self, thread_id, cwd=None):
        self.calls.append(("thread/resume", thread_id))
        self.loaded_threads.add(thread_id)
        self.note_thread_settings(thread_id, self._resume_result)

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params))
        if method == "thread/settings/update" and self._update_error:
            raise self._update_error
        return {}


def _patched(app):
    return patch.object(bridge, "CODEX_APP", app), patch.object(
        bridge, "_check_auth", return_value=None)


class TestSettingsRead(unittest.IsolatedAsyncioTestCase):
    async def test_read_never_calls_thread_read(self):
        """thread/read 不帶設定欄 —— 拿它當來源就是舊版那個空白面板的成因。"""
        app = FakeCodexApp(resume_result={"model": "gpt-5.6-sol",
                                          "approvalPolicy": "on-request"})
        p1, p2 = _patched(app)
        with p1, p2:
            res = await bridge.codex_session_settings_read(TID, FakeRequest())
        self.assertNotIn("thread/read", [c[0] for c in app.calls])
        self.assertEqual(res["settings"]["model"], "gpt-5.6-sol")
        self.assertEqual(res["settings"]["approvalPolicy"], "on-request")

    async def test_read_seeds_from_resume_when_cache_cold(self):
        app = FakeCodexApp(resume_result={"model": "gpt-5.6-sol"})
        p1, p2 = _patched(app)
        with p1, p2:
            await bridge.codex_session_settings_read(TID, FakeRequest())
        self.assertIn(("thread/resume", TID), app.calls)

    async def test_read_uses_cache_without_touching_provider(self):
        """快取熱的時候不要為了讀設定去 resume(會跟桌面端搶寫入鎖)。"""
        app = FakeCodexApp()
        app.thread_settings[TID] = {"model": "gpt-5.1-codex-max", "at": 1.0}
        p1, p2 = _patched(app)
        with p1, p2:
            res = await bridge.codex_session_settings_read(TID, FakeRequest())
        self.assertEqual(app.calls, [])
        self.assertEqual(res["settings"], {"model": "gpt-5.1-codex-max"})
        self.assertNotIn("at", res["settings"])   # 內部時戳不外洩


class TestSettingsNotification(unittest.IsolatedAsyncioTestCase):
    def test_broadcast_field_names_are_normalized(self):
        """`thread/settings/updated` 用 effort/sandboxPolicy,start 用
        reasoningEffort/sandbox。兩套都要吃進同一個正規化後的欄位。"""
        app = FakeCodexApp()
        app.note_thread_settings(TID, {
            "model": "gpt-5.1-codex-max",
            "approvalPolicy": "never",
            "effort": "high",
            "sandboxPolicy": {"type": "readOnly"},
        })
        s = app.thread_settings[TID]
        self.assertEqual(s["model"], "gpt-5.1-codex-max")
        self.assertEqual(s["effort"], "high")
        self.assertEqual(s["sandbox"], {"type": "readOnly"})

    def test_start_shape_field_names_are_normalized(self):
        app = FakeCodexApp()
        app.note_thread_settings(TID, {"model": "gpt-5.6-sol",
                                       "reasoningEffort": "low",
                                       "sandbox": {"type": "workspaceWrite"}})
        s = app.thread_settings[TID]
        self.assertEqual(s["effort"], "low")
        self.assertEqual(s["sandbox"], {"type": "workspaceWrite"})

    def test_partial_update_merges_instead_of_clobbering(self):
        """只改 approvalPolicy 時不能把已知的 model 打成 None。"""
        app = FakeCodexApp()
        app.note_thread_settings(TID, {"model": "gpt-5.6-sol",
                                       "approvalPolicy": "on-request"})
        app.note_thread_settings(TID, {"approvalPolicy": "never"})
        s = app.thread_settings[TID]
        self.assertEqual(s["model"], "gpt-5.6-sol")
        self.assertEqual(s["approvalPolicy"], "never")


class TestApprovalPolicyEnum(unittest.IsolatedAsyncioTestCase):
    def test_on_failure_is_gone_in_0147(self):
        self.assertNotIn("on-failure", bridge._CODEX_APPROVAL_POLICIES)
        self.assertEqual(set(bridge._CODEX_APPROVAL_POLICIES),
                         {"untrusted", "on-request", "granular", "never"})

    async def test_stale_policy_rejected_locally_as_400(self):
        """本地就擋掉,不要送到 app-server 再被翻成『上一輪正在跑』。"""
        app = FakeCodexApp()
        p1, p2 = _patched(app)
        with p1, p2:
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge.codex_session_settings(
                    TID, FakeRequest({"approvalPolicy": "on-failure"}))
        self.assertEqual(cm.exception.status_code, 400)
        self.assertNotIn("thread/settings/update", [c[0] for c in app.calls])


class TestInvalidParamTranslation(unittest.IsolatedAsyncioTestCase):
    """-32600 是泛用碼。參數錯必須回 400,不能謊報 409 忙碌 —— 那正是
    thread-lock 事件裡『查了一整天』的同一種誤導。"""

    def _raise(self, message):
        err = bridge.CodexAppServerError(message)
        err.code = -32600
        with self.assertRaises(bridge.HTTPException) as cm:
            bridge._codex_http_error(err)
        return cm.exception

    def test_unknown_variant_is_400_not_busy(self):
        exc = self._raise("Invalid request: unknown variant `on-failure`, "
                          "expected one of `untrusted`, `on-request`")
        self.assertEqual(exc.status_code, 400)
        self.assertEqual(exc.code, "CX_INVALID_PARAM")

    def test_missing_field_is_400(self):
        exc = self._raise("Invalid request: missing field `threadId`")
        self.assertEqual(exc.status_code, 400)

    def test_genuine_busy_still_409(self):
        """真的忙碌不能被新規則誤殺。"""
        exc = self._raise("thread is busy with another turn")
        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.code, "CX_TURN_IN_FLIGHT")

    def test_thread_lock_still_wins(self):
        """鎖的判定必須排在最前面(既有行為,不能被本次改動破壞)。"""
        err = bridge.CodexAppServerError(
            f"thread {TID} already has an active writer")
        err.code = -32600
        with patch.object(bridge, "_cx_feed_thread_locked", return_value=None):
            with self.assertRaises(bridge.HTTPException) as cm:
                bridge._codex_http_error(err)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(cm.exception.code, "CX_THREAD_LOCKED")


class TestSettingsWrite(unittest.IsolatedAsyncioTestCase):
    async def test_write_updates_cache_immediately(self):
        """廣播是非同步的;寫完立刻讀回不能還是舊值,否則又被當成沒生效。"""
        app = FakeCodexApp()
        p1, p2 = _patched(app)
        with p1, p2:
            await bridge.codex_session_settings(
                TID, FakeRequest({"model": "gpt-5.1-codex-max",
                                  "approvalPolicy": "never"}))
            res = await bridge.codex_session_settings_read(TID, FakeRequest())
        self.assertEqual(res["settings"]["model"], "gpt-5.1-codex-max")
        self.assertEqual(res["settings"]["approvalPolicy"], "never")


if __name__ == "__main__":
    unittest.main()
