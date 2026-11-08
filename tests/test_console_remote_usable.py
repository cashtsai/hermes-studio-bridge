"""console-remote 這條路**真的能做事** —— 不只是「擋得住」。

為什麼要獨立一檔:`test_console_remote_scope.py` 的 12 條幾乎都在驗「拒絕」,
唯一的正向測試打的是 `/health` —— 那條**任何 token 都過**,所以它證明不了
遠端瀏覽器能不能操作 agent。

少了這一檔,整組 scope 測試可以靠「**全部拒絕**」通過。那不是安全,是功能壞掉
而測試看不出來 —— 而付費賣的就是「從任意電腦開控制台」這件事,壞了等於賣空氣。

釘的是使用者實際會走的那幾步,每一步都必須**不是 403**:
  列 session → 讀卡片流 → 送輸入 → 核准。

同時釘住反面:真正危險的端點仍然擋著。選的三個都有具體後果 ——
  · `/app/v1/relay/*`:能開關公網曝光。借來的電腦不該決定機主的曝光面。
  · `/clientlog`:寫入端點,能灌垃圾進機主的診斷日誌。
  · `/pair/*`:能發新的配對憑證,等於自我提權。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import sys
import tempfile
import time
import unittest
import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

_TMP = tempfile.mkdtemp(prefix="console-remote-usable-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

class ConsoleRemoteUsableTest(unittest.TestCase):
    TOKEN = "pdev-console-remote-usable"

    def setUp(self):
        self.http = TestClient(bridge.app)
        self.addCleanup(self.http.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.wd = self.repo / "shared"
        self.wd.mkdir()
        self.sid = "claude_code:demo"
        self.base = "/app/v2/sessions/" + self.sid
        self.a = {"id":"aid-demo", "session_id":self.sid, "kind":"permission", "status":"pending",
                  "options":[{"key":"1","label":"Yes"}, {"key":"2","label":"Always allow"}]}
        self.enterContext(patch.object(bridge, "PERSONAS", {}))
        self.enterContext(patch.object(bridge, "_CONSOLE_GRANTS", {}))
        self.enterContext(patch.object(bridge, "_CC_APPROVAL_ACTIVE", {"demo":{"aid":"aid-demo"}}))
        self.enterContext(patch.object(bridge, "CONSOLE_REMOTE_FILE_ROOTS", [str(self.wd.resolve())]))
        for name, val in [("_delegation_v2_sessions", []), ("_delegated_codex_thread_ids", set()),
                          ("_codex_v2_visible_threads", []), ("_hermes_pending_by_session", {}),
                          ("_cc_conf_rows", [("demo",str(self.wd),"1")]),
                          ("_v2_cc_state", ("waiting_approval", {"options":self.a["options"]})),
                          ("_approval_get_row", self.a), ("_registry_call_safe", None)]:
            self.enterContext(patch.object(bridge, name, return_value=val))
        for m,k in [(bridge.GEMINI,"configured"),(bridge.OPENCLAW,"configured"),(bridge.cc_sdk,"enabled")]:
            self.enterContext(patch.object(m,k,return_value=False))
        with bridge._PAIR_LOCK:
            bridge._DEVICE_TOKENS[self.TOKEN] = {
                "name": "borrowed laptop", "platform": "web",
                "scope": bridge.CONSOLE_REMOTE_SCOPE,
                "expires": time.time() + 600,
                "created": time.time(), "last_seen": time.time(),
            }
        self.listed = self.http.get("/app/v2/sessions", headers=self._hdr())
        self.assertEqual(self.listed.status_code, 200)

    def tearDown(self):
        # 同 test_console_remote_scope:不用 `with` —— 踩到鎖重入時這裡會卡死,
        # 整套測試掛掉且看不到任何失敗訊息。
        if bridge._PAIR_LOCK.acquire(timeout=5):
            try:
                bridge._DEVICE_TOKENS.pop(self.TOKEN, None)
            finally:
                bridge._PAIR_LOCK.release()
        else:
            bridge._DEVICE_TOKENS.pop(self.TOKEN, None)
            print("⚠️ tearDown 拿不到 _PAIR_LOCK(5s)")

    def _hdr(self):
        return {"Authorization": "Bearer " + self.TOKEN}

    # ── 正向:使用者實際會走的那幾步 ───────────────────────────────

    def test_can_list_sessions(self):
        """第一步:開了控制台要看得到有哪些 agent 在跑。"""
        row, = self.listed.json()["sessions"]
        self.assertEqual(row["id"], self.sid)
        self.assertIn("changes", row["capabilities"])
        self.assertNotIn("keys", row["capabilities"])
        self.assertEqual([o["key"] for o in row["meta"]["console_options"]], ["1"])

    def test_can_read_a_session_card_stream(self):
        """第二步:點進去要看得到內容。"""
        with patch.object(bridge, "_v2_card_store", return_value=bridge.carddigest.SessionCardStore()):
            r = self.http.get(self.base + "/cards", headers=self._hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["cards"], [])

    def test_can_send_input(self):
        """第三步:**這是整個產品的重點** —— 遠端要能真的指揮 agent。

        被 scope 擋掉的話,控制台就只是個唯讀觀景窗,付費賣的東西不存在。
        """
        with patch.object(bridge, "_cc_capture_pane_fresh", return_value="> "), \
             patch.object(bridge, "_cc_input_core", return_value={"ok":True}) as provider:
            r = self.http.post(self.base + "/input", headers=self._hdr(), json={"text":"hello"})
        self.assertEqual(r.status_code, 200, r.text)
        provider.assert_awaited_once_with("demo", {"text":"hello"})

    def test_can_reach_approvals(self):
        """第四步:agent 要權限時要能在遠端按同意。"""
        with patch.object(bridge, "_approval_decide_core", return_value={"status":"approved"}) as dispatch:
            for body in [{"key":"1"}, {"approval_id":"other","key":"1"}, {"approval_id":"aid-demo","key":"2"},
                         {"approval_id":"aid-demo","approve":True,"remember":True}]:
                self.assertEqual(self.http.post(self.base + "/approve",headers=self._hdr(),json=body).status_code,403)
            body = {"approval_id":"aid-demo","key":"1"}
            self.assertEqual(self.http.post(self.base + "/approve",headers=self._hdr(),json=body).status_code,200)
            dispatch.assert_awaited_once_with("aid-demo",body)
            self.a["session_id"] = "claude_code:other"
            self.assertEqual(self.http.post(self.base + "/approve",headers=self._hdr(),json=body).status_code,403)

    def test_method_path_and_session_guesses_fail_closed(self):
        for method, path in [
                ("POST",self.base+"/rename"), ("POST",self.base+"/archive"), ("POST",self.base+"/config"),
                ("POST",self.base+"/key"), ("POST",self.base+"/cards"), ("GET",self.base+"/input"),
                ("GET",self.base+"/config"), ("GET",self.base+"/future"), ("GET","/app/v2/events"),
                ("GET","/files"), ("GET","/app/v1/approvals"), ("POST","/ccsessions"), ("DELETE","/codexsessions/x"),
                ("POST","/sessions/x"), ("GET","/app/v2/sessions?archived=true"),
                ("GET","/app/v2/sessions/claude_code:other/cards"), ("GET","/app/v2/sessions/demo/cards")]:
            with self.subTest(path=path,method=method):
                r = self.http.request(method,path,headers=self._hdr())
                self.assertEqual((r.status_code,r.json()["error"]["code"]), (403,"SCOPE_FORBIDDEN"))
        r = self.http.get(self.base+"/cards",headers={"Authorization":"Bearer made-up"})
        self.assertEqual(r.status_code,401)
        with patch.object(bridge, "_CONSOLE_GRANT_TTL", -1):
            self.assertEqual(self.http.get(self.base+"/cards",headers=self._hdr()).status_code,403)

    def test_scoped_changes_and_diff_cannot_escape_subdirectory(self):
        subprocess.run(["git","init","-q",str(self.repo)], check=True)
        (self.repo / "private.txt").write_text("secret")
        (self.wd / "[x].txt").write_text("visible")
        (self.wd / "x.txt").write_text("other")
        (self.wd / "escape").symlink_to(self.repo / "private.txt")
        r = self.http.get(self.base+"/changes",headers=self._hdr())
        self.assertEqual(r.status_code,200)
        self.assertEqual({f["path"] for f in r.json()["files"]},{"[x].txt","x.txt","escape"})
        def diff(path):
            return self.http.get(self.base+"/diff",params={"path":path},headers=self._hdr())
        self.assertIn("+visible",diff("[x].txt").json()["diff"])
        self.assertNotIn("+other",diff("[x].txt").json()["diff"])
        for path in ("../private.txt","escape",str(self.repo / "private.txt")):
            self.assertEqual(diff(path).status_code,400)
        with patch.object(bridge,"CONSOLE_REMOTE_FILE_ROOTS",[]):
            self.assertEqual(self.http.get(self.base+"/changes",headers=self._hdr()).status_code,403)
            self.assertEqual(diff("x.txt").status_code,403)

    def test_attachment_reference_shell_and_persistent_options_are_not_inputs(self):
        with patch.object(bridge, "_cc_input_core") as provider:
            for body in [{"content":"test","attachments":[{"path":"/tmp/stored"}]},
                         {"text":"!ls"},{"text":"/permissions"},{"text":"a","config":{}}]:
                self.assertEqual(self.http.post(self.base+"/input",headers=self._hdr(),json=body).status_code,403)
            provider.assert_not_awaited()

    def test_cc_foreground_check_rejects_background_claude_and_shell(self):
        with patch.object(bridge,"_tmux_run",return_value=(0,"%4 /dev/ttys000","")), \
             patch.object(bridge,"_capture_stdout",return_value=(0,"4 4 zsh\n5 4 claude")):
            with self.assertRaises(bridge.HTTPException):
                asyncio.run(bridge._cc_console_pane("demo"))
        with patch.object(bridge,"_tmux_run",return_value=(0,"%4 /dev/ttys000","")), \
             patch.object(bridge,"_capture_stdout",return_value=(0,"4 5 zsh\n5 5 claude")):
            self.assertEqual(asyncio.run(bridge._cc_console_pane("demo")), "%4")

    def test_grants_are_not_shared_between_browsers_or_retained_after_refresh(self):
        with patch.dict(bridge._DEVICE_TOKENS, {"other-browser":dict(bridge._DEVICE_TOKENS[self.TOKEN])}):
            r = self.http.post(self.base+"/interrupt",headers={"Authorization":"Bearer other-browser"})
            self.assertEqual(r.status_code,403)
        with patch.object(bridge,"_cc_conf_rows",return_value=[]):
            self.assertEqual(self.http.get("/app/v2/sessions",headers=self._hdr()).json()["sessions"],[])
        self.assertEqual(self.http.post(self.base+"/input",headers=self._hdr(),json={"text":"late"}).status_code,403)

    def test_codex_input_reaches_original_thread_with_one_provider_dispatch(self):
        client = Mock()
        client.start_turn = AsyncMock()
        for m in ("pending_approval_for_thread","thread_lock_info","is_active","is_thread_locked"):
            getattr(client,m).return_value = False
        summary = {"thread_id":"qa-original", "name":"Original", "workdir":str(self.wd)}
        with patch.object(bridge,"_codex_v2_visible_threads",return_value=[{}]), \
             patch.object(bridge,"_codex_enrich_summary",return_value=summary), \
             patch.object(bridge,"_cx_client_for_thread",return_value=client), \
             patch.object(bridge,"_CODEX_V2_STALE",False), patch.object(bridge,"_cx_feed_input_accepted"):
            res = self.http.get("/app/v2/sessions",headers=self._hdr())
            sid = next(s["id"] for s in res.json()["sessions"] if s["provider"] == "codex")
            payload = {"content":"continue QA","client_id":"scope-fixture-1"}
            for _ in range(2):
                ack = self.http.post("/app/v2/sessions/"+sid+"/input",headers=self._hdr(),json=payload)
                self.assertEqual(ack.status_code,200,ack.text)
                self.assertEqual(ack.json()["session_id"],"codex:qa-original")
            client.start_turn.assert_awaited_once()
            self.assertEqual(client.start_turn.await_args.args[0],"qa-original")

    def test_can_load_the_console_itself(self):
        r = self.http.get("/console/manifest.json", headers=self._hdr())
        self.assertIn(r.status_code,(200,404))  # Console feature flag may be off in CI

    # ── 反面:危險的仍然擋著(每條都有具體後果)─────────────────

    def test_cannot_toggle_relay_exposure(self):
        """借來的電腦不該能決定機主的公網曝光面。"""
        r = self.http.post("/app/v1/relay/enable", headers=self._hdr(),
                           json={"enabled": True})
        self.assertEqual(r.status_code, 403, r.text[:200])
        self.assertIn("SCOPE_FORBIDDEN", r.text)

    def test_cannot_write_client_log(self):
        """寫入端點 —— 能灌垃圾進機主的診斷日誌。"""
        r = self.http.post("/clientlog", headers=self._hdr(),
                           json={"msg": "junk"})
        self.assertEqual(r.status_code, 403, r.text[:200])

    def test_cannot_mint_pairing_credentials(self):
        """能發新配對憑證 = 自我提權,這條最嚴重。"""
        r = self.http.get("/pair/devices", headers=self._hdr())
        self.assertEqual(r.status_code, 403, r.text[:200])


if __name__ == "__main__":
    unittest.main()
