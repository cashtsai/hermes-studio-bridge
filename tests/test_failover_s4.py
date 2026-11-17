"""S4:額度耗盡自動切 provider(usage_limits + bridge 接線)。

釘住的事:

1. **順序選擇** —— 照使用者設定的備援順序挑第一個能用的。
2. **caps / availability 過濾** —— 沒宣告 caps、沒安裝、明確未登入、
   沒有交棒落地實作的,一律不選(宣告了卻不能落地就是死鈕)。
3. **suggest vs auto** —— 預設 suggest(出按鈕問);auto 會切,但卡片
   **一定明示**換成誰、為什麼、失去什麼(自動切不得靜默)。
4. **交棒內容** —— 切換走既有 ContextTransfer v1(新 session + milestone
   摘要),不是假裝同一條對話。
5. **沒有備援時也要說話** —— blocked 卡片列出考慮過誰、為什麼都不行。
"""
import _isolation  # noqa: F401
import asyncio
import json
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="failover-s4-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carddigest  # noqa: E402
import usage_limits  # noqa: E402

#: 測試用的 caps 表(形狀照 bridge._provider_caps_payload():caps 旗標 + ops)
CAPS = {
    "claude_code": {"interrupt": True, "queued_messages": True,
                    "approvals": True, "streaming": "live",
                    "ops": ["history", "rename"]},
    "cc2": {"interrupt": True, "queued_messages": True, "approvals": True,
            "streaming": "live",
            "ops": ["archive", "history", "rename"]},
    "codex": {"interrupt": True, "queued_messages": True, "approvals": True,
              "streaming": "live",
              "ops": ["archive", "config_read", "config_write", "history",
                      "rename"]},
    "gemini": {"interrupt": False, "queued_messages": True, "approvals": True,
               "streaming": "live", "ops": ["history"]},
    "grok": {"interrupt": True, "queued_messages": True, "approvals": True,
             "streaming": "poll", "ops": []},
    "opencode": {"interrupt": True, "queued_messages": True,
                 "approvals": True, "streaming": "live",
                 "ops": ["config_read", "config_write"]},
}

ALL_UP = {p: {"installed": True, "logged_in": True} for p in CAPS}


def _limited(reset_at=None):
    """E3 的輸出形狀 —— 用真的 classifier/overlay 產,不手刻 dict。"""
    return carddigest.apply_usage_limited(
        {"phase": "idle"}, {"error_kind": carddigest.USAGE_LIMIT_KIND,
                            "reset_at": reset_at})


class LimitDetectionTest(unittest.TestCase):
    """限額判定是 **E3 的輸出**,這裡只負責讀 —— 不重做字串比對。"""

    def test_reads_phase_not_error_text(self):
        import time
        st = _limited(time.time() + 3600)
        self.assertEqual(st["phase"], "usage_limited")
        self.assertTrue(usage_limits.is_usage_limited(st))
        self.assertIsNotNone(usage_limits.reset_at_of(st))

    def test_ordinary_status_is_not_limited(self):
        for st in ({}, None, {"phase": "idle"}, {"phase": "error"},
                   {"phase": "running"}):
            self.assertFalse(usage_limits.is_usage_limited(st), st)

    def test_unparseable_reset_stays_none(self):
        st = _limited(None)
        self.assertIsNone(usage_limits.reset_at_of(st))
        # 不准編一個時間出來
        self.assertIn("不明", usage_limits._fmt_reset(None))


class OrderSelectionTest(unittest.TestCase):
    def test_picks_first_usable_in_order(self):
        plan = usage_limits.choose_target(
            "claude_code", availability=ALL_UP, caps=CAPS,
            order=["codex", "cc2"])
        self.assertEqual(plan["provider"], "codex")

    def test_order_is_respected_not_alphabetical(self):
        plan = usage_limits.choose_target(
            "claude_code", availability=ALL_UP, caps=CAPS,
            order=["cc2", "codex"])
        self.assertEqual(plan["provider"], "cc2")

    def test_never_picks_itself(self):
        plan = usage_limits.choose_target(
            "codex", availability=ALL_UP, caps=CAPS,
            order=["codex", "cc2"])
        self.assertEqual(plan["provider"], "cc2")
        self.assertIn({"provider": "codex", "why": "same_provider"},
                      plan["rejected"])

    def test_empty_order_yields_no_candidate(self):
        plan = usage_limits.choose_target(
            "codex", availability=ALL_UP, caps=CAPS, order=[])
        self.assertIsNone(plan["provider"])
        self.assertEqual(plan["reason"], "empty_order")

    def test_parse_order_accepts_string_and_dedupes(self):
        self.assertEqual(usage_limits.parse_order("cc2, codex ,cc2"),
                         ["cc2", "codex"])
        self.assertEqual(usage_limits.parse_order(["a", "a", "b"]), ["a", "b"])
        self.assertEqual(usage_limits.parse_order(None), [])


class FilteringTest(unittest.TestCase):
    def test_uninstalled_is_rejected(self):
        avail = dict(ALL_UP, codex={"installed": False, "logged_in": True})
        plan = usage_limits.choose_target(
            "claude_code", availability=avail, caps=CAPS,
            order=["codex", "cc2"])
        self.assertEqual(plan["provider"], "cc2")
        self.assertIn({"provider": "codex", "why": "unavailable"},
                      plan["rejected"])

    def test_explicitly_logged_out_is_rejected(self):
        avail = dict(ALL_UP, codex={"installed": True, "logged_in": False})
        plan = usage_limits.choose_target(
            "claude_code", availability=avail, caps=CAPS,
            order=["codex", "cc2"])
        self.assertEqual(plan["provider"], "cc2")

    def test_unknown_login_is_treated_as_usable(self):
        """`logged_in=None` = 還在探(冷啟要 10s+)。當成沒登入會讓剛開機的
        機器一律沒備援;真的沒登入時 spawn 會失敗並回報,比安靜地沒備援好查。"""
        avail = dict(ALL_UP, codex={"installed": True, "logged_in": None})
        plan = usage_limits.choose_target(
            "claude_code", availability=avail, caps=CAPS, order=["codex"])
        self.assertEqual(plan["provider"], "codex")

    def test_provider_without_declared_caps_is_rejected(self):
        """宣告 caps 是加入這張表的門票 —— 沒宣告就不是備援候選。"""
        plan = usage_limits.choose_target(
            "claude_code",
            availability=dict(ALL_UP, mystery={"installed": True,
                                               "logged_in": True}),
            caps=CAPS, order=["mystery", "cc2"])
        self.assertEqual(plan["provider"], "cc2")
        self.assertIn({"provider": "mystery", "why": "no_caps_declared"},
                      plan["rejected"])

    def test_providers_without_handoff_landing_are_rejected(self):
        """宣告了卻不能落地就是死鈕 —— 沒有 spawn helper 的不准選。"""
        plan = usage_limits.choose_target(
            "claude_code", availability=ALL_UP, caps=CAPS,
            order=["gemini", "grok", "codex"],
            targets=("codex", "cc2"))
        self.assertEqual(plan["provider"], "codex")
        whys = {r["provider"]: r["why"] for r in plan["rejected"]}
        self.assertEqual(whys["gemini"], "no_handoff_target")
        self.assertEqual(whys["grok"], "no_handoff_target")

    def test_available_providers_helper(self):
        avail = {"a": {"installed": True, "logged_in": True},
                 "b": {"installed": True, "logged_in": None},
                 "c": {"installed": True, "logged_in": False},
                 "d": {"installed": False, "logged_in": True},
                 "e": "not a dict"}
        self.assertEqual(sorted(usage_limits.available_providers(avail)),
                         ["a", "b"])


class CapsGapTest(unittest.TestCase):
    """誠實邊界:切過去會少什麼,要在卡片上講明。"""

    def test_lost_capability_flag_is_reported(self):
        lost = usage_limits.caps_gap("claude_code", "gemini", CAPS)
        self.assertIn("中斷回合", lost)

    def test_lost_ops_are_reported_in_human_words(self):
        lost = usage_limits.caps_gap("codex", "gemini", CAPS)
        self.assertIn("改名", lost)
        self.assertIn("封存", lost)
        self.assertIn("讀設定", lost)

    def test_gained_capability_is_not_advertised(self):
        """B 多出來的能力不報 —— 卡片講的是邊界,不是推銷。"""
        lost = usage_limits.caps_gap("claude_code", "codex", CAPS)
        self.assertEqual(lost, [])

    def test_streaming_downgrade_is_reported(self):
        lost = usage_limits.caps_gap("codex", "grok", CAPS)
        self.assertTrue(any("輪詢" in x for x in lost), lost)

    def test_unknown_provider_does_not_explode(self):
        self.assertEqual(usage_limits.caps_gap("nope", "alsonope", CAPS), [])


class ConfigTest(unittest.TestCase):
    def setUp(self):
        for k in ("POCKET_FAILOVER_MODE", "POCKET_FAILOVER_ORDER"):
            os.environ.pop(k, None)
        try:
            os.remove(usage_limits._CONFIG_FILE)
        except OSError:
            pass

    def test_default_is_suggest_not_auto(self):
        """house precedent(orchestrator.py):搬動工作要明確批准。"""
        cfg = usage_limits.load_config()
        self.assertEqual(cfg["mode"], "suggest")
        self.assertEqual(cfg["mode_source"], "default")
        self.assertEqual(cfg["order"], list(usage_limits.DEFAULT_ORDER))

    def test_merge_write_keeps_the_other_field(self):
        usage_limits.save_config(mode="auto", order=["codex"])
        usage_limits.save_config(order=["cc2"])
        cfg = usage_limits.load_config()
        self.assertEqual(cfg["mode"], "auto")
        self.assertEqual(cfg["order"], ["cc2"])

    def test_env_wins(self):
        usage_limits.save_config(mode="auto", order=["codex"])
        os.environ["POCKET_FAILOVER_MODE"] = "suggest"
        os.environ["POCKET_FAILOVER_ORDER"] = "cc2,grok"
        cfg = usage_limits.load_config()
        self.assertEqual((cfg["mode"], cfg["mode_source"]),
                         ("suggest", "env"))
        self.assertEqual((cfg["order"], cfg["order_source"]),
                         (["cc2", "grok"], "env"))

    def test_bad_mode_in_file_degrades_to_suggest(self):
        """讀取端寬鬆,但 fail-safe 的方向:打錯字不該變成「自動切」。"""
        with open(usage_limits._CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"mode": "AUTOMATIC"}, f)
        self.assertEqual(usage_limits.load_config()["mode"], "suggest")

    def test_bad_mode_on_write_raises(self):
        """寫入端嚴格:默默改掉會讓使用者以為自己開了 auto。"""
        with self.assertRaises(ValueError):
            usage_limits.save_config(mode="automatic")

    def test_mode_is_case_insensitive(self):
        usage_limits.save_config(mode="AUTO")
        self.assertEqual(usage_limits.load_config()["mode"], "auto")


class CardTest(unittest.TestCase):
    def setUp(self):
        self.plan = usage_limits.choose_target(
            "claude_code", availability=ALL_UP, caps=CAPS,
            order=["gemini", "codex"], targets=("codex", "cc2"))

    def test_suggest_card_draws_no_dead_buttons(self):
        """bridge 沒有斜線指令解析層 —— 做成 `send` 按鈕按下去只會把
        `/failover codex` 送進一條正卡在限額上的 session,什麼都不會發生。
        所以卡片只告知,控制面在端點上(§19.2)。"""
        body = usage_limits.suggest_card_body("claude_code", self.plan,
                                              thread_id="t1", reset_at=None)
        self.assertEqual(body["kind"], "choices")
        self.assertEqual(body["options"], [])
        # 但要講清楚「怎麼切」,不是只說「額度用完了」
        self.assertIn("codex", body["detail"])
        self.assertIn("failover", body["detail"])
        self.assertIn("t1", body["detail"])
        # Telegram 上要能退化成純文字
        self.assertTrue(body["fallback_text"])

    def test_no_card_body_promises_a_slash_command(self):
        """守門:只要有人把 `send:` 按鈕加回來,又沒接指令解析層,這條會紅。"""
        plan = self.plan
        bodies = [
            usage_limits.suggest_card_body("claude_code", plan, thread_id="t1"),
            usage_limits.auto_card_body("claude_code", plan, thread_id="t1",
                                        new_session_id="codex:n"),
            usage_limits.blocked_card_body("claude_code", plan, thread_id="t1"),
        ]
        for body in bodies:
            for opt in body["options"]:
                self.assertNotIn("send", opt,
                                 f"{body['kind']} 畫了沒有落地實作的按鈕")

    def test_suggest_card_says_it_is_a_new_session(self):
        """不是假裝同一條對話 —— 交棒語意要寫在卡片上。"""
        body = usage_limits.suggest_card_body("claude_code", self.plan,
                                              thread_id="t1")
        self.assertIn("新的", body["detail"])
        self.assertIn("不是同一條對話", body["detail"])

    def test_auto_card_states_the_switch_and_the_reason(self):
        """自動切不得靜默:換成誰、為什麼、新 session 是哪條,全要明示。"""
        body = usage_limits.auto_card_body(
            "claude_code", self.plan, thread_id="t1",
            new_session_id="codex:new1")
        self.assertIn("已自動切換", body["detail"])
        self.assertIn("claude_code", body["title"])
        self.assertIn("codex", body["title"])
        self.assertIn("額度用完", body["title"])
        self.assertIn("codex:new1", body["detail"])
        # 已成事實 → 不給假按鈕
        self.assertEqual(body["options"], [])

    def test_auto_card_mentions_lost_capability(self):
        plan = usage_limits.choose_target(
            "codex", availability=ALL_UP, caps=CAPS,
            order=["cc2"], targets=("codex", "cc2"))
        body = usage_limits.auto_card_body("codex", plan, thread_id="t1")
        self.assertIn("會少", body["detail"])
        self.assertIn("讀設定", body["detail"])

    def test_blocked_card_explains_who_was_considered(self):
        """沒有它,使用者會以為 failover 壞了或根本沒裝。"""
        avail = {p: {"installed": False, "logged_in": True} for p in CAPS}
        plan = usage_limits.choose_target(
            "claude_code", availability=avail, caps=CAPS,
            order=["codex", "cc2"], targets=("codex", "cc2"))
        self.assertIsNone(plan["provider"])
        body = usage_limits.blocked_card_body("claude_code", plan,
                                              thread_id="t1")
        self.assertIn("沒有可用的備援", body["detail"])
        self.assertIn("codex", body["detail"])
        self.assertIn("未安裝或未登入", body["detail"])
        self.assertEqual(body["options"], [])


class PlanForTest(unittest.TestCase):
    def setUp(self):
        for k in ("POCKET_FAILOVER_MODE", "POCKET_FAILOVER_ORDER"):
            os.environ.pop(k, None)
        try:
            os.remove(usage_limits._CONFIG_FILE)
        except OSError:
            pass

    def _plan(self, status, **kw):
        kw.setdefault("availability", ALL_UP)
        kw.setdefault("caps", CAPS)
        kw.setdefault("targets", ("codex", "cc2"))
        return usage_limits.plan_for("claude_code", status, **kw)

    def test_not_limited_is_action_none(self):
        out = self._plan({"phase": "idle"})
        self.assertFalse(out["limited"])
        self.assertEqual(out["action"], "none")

    def test_limited_defaults_to_suggest(self):
        out = self._plan(_limited(), order=["codex"])
        self.assertTrue(out["limited"])
        self.assertEqual(out["action"], "suggest")
        self.assertEqual(out["plan"]["provider"], "codex")

    def test_auto_mode_is_honoured(self):
        usage_limits.save_config(mode="auto")
        out = self._plan(_limited(), order=["codex"])
        self.assertEqual(out["action"], "auto")

    def test_no_candidate_is_blocked_even_in_auto_mode(self):
        """auto 也不能無中生有一個 provider。"""
        usage_limits.save_config(mode="auto")
        out = self._plan(_limited(), order=[])
        self.assertEqual(out["action"], "blocked")


# ── bridge 側接線 ──────────────────────────────────────────────────────────

import bridge  # noqa: E402


class BridgeAvailabilityTest(unittest.TestCase):
    def test_shape_is_what_usage_limits_expects(self):
        avail = bridge._failover_availability()
        for prov, st in avail.items():
            self.assertIn("installed", st, prov)
            self.assertIn("logged_in", st, prov)
            self.assertIsInstance(st["installed"], bool, prov)

    def test_every_caps_provider_has_an_availability_entry(self):
        """漏一家 = 那家永遠不會被選為備援,而且不會有任何錯誤訊息。"""
        avail = bridge._failover_availability()
        for prov in bridge._PROVIDER_CAPS:
            self.assertIn(prov, avail, f"{prov} 沒有 availability 判準")

    def test_opencode_has_no_login_wall(self):
        """報 False 會憑空造出一道登入牆,把一個本來能用的備援關掉。"""
        self.assertIs(bridge._failover_availability()["opencode"]["logged_in"],
                      True)

    def test_handoff_targets_all_declare_caps(self):
        for prov in bridge._TRANSFER_TARGETS:
            self.assertIn(prov, bridge._PROVIDER_CAPS)


class BridgeEndpointTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(bridge.app)
        self.auth = {"Authorization": "Bearer " + bridge.BRIDGE_TOKEN}
        for k in ("POCKET_FAILOVER_MODE", "POCKET_FAILOVER_ORDER"):
            os.environ.pop(k, None)
        try:
            os.remove(usage_limits._CONFIG_FILE)
        except OSError:
            pass

    def test_config_requires_auth(self):
        self.assertIn(self.client.get("/app/v1/failover/config").status_code,
                      (401, 429))

    def test_config_get_answers_why_a_provider_was_not_picked(self):
        r = self.client.get("/app/v1/failover/config", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["mode"], "suggest")
        self.assertEqual(body["modes"], ["suggest", "auto"])
        # 設定面板要能當場答「我順序裡那家為什麼沒被選」
        self.assertIn("availability", body)
        self.assertIn("available", body)
        self.assertEqual(body["handoff_targets"],
                         list(bridge._TRANSFER_TARGETS))

    def test_config_put_round_trip(self):
        r = self.client.put("/app/v1/failover/config", headers=self.auth,
                            json={"mode": "auto", "order": ["codex", "cc2"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mode"], "auto")
        got = self.client.get("/app/v1/failover/config",
                              headers=self.auth).json()
        self.assertEqual(got["order"], ["codex", "cc2"])

    def test_config_put_rejects_bad_mode(self):
        r = self.client.put("/app/v1/failover/config", headers=self.auth,
                            json={"mode": "automatic"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "FAILOVER_BAD_MODE")

    def test_config_put_rejects_empty_body(self):
        r = self.client.put("/app/v1/failover/config", headers=self.auth,
                            json={})
        self.assertEqual(r.status_code, 400)

    def test_preview_unknown_thread_is_404(self):
        r = self.client.get("/app/v2/threads/nope-no-such/failover",
                            headers=self.auth)
        self.assertEqual(r.status_code, 404)

    def test_execute_unknown_thread_is_404(self):
        r = self.client.post("/app/v2/threads/nope-no-such/failover",
                             headers=self.auth, json={})
        self.assertEqual(r.status_code, 404)


class BridgePlanAndExecuteTest(unittest.TestCase):
    """用真的 app thread + binding 跑預覽與切換,交棒管線 monkeypatch 掉。"""

    def setUp(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(bridge.app)
        self.auth = {"Authorization": "Bearer " + bridge.BRIDGE_TOKEN}
        import t3code_runtime
        self.t3 = t3code_runtime
        t3code_runtime.ensure_schema(bridge.CANON_DB)
        uniq = os.urandom(6).hex()
        self.tid = "t-failover-" + uniq
        # provider_thread_id 存的就是完整的 bridge session id(帶 provider
        # 前綴)—— 照 §app_thread 的相容紅線,不是裸名字。
        self.src = "claude_code:Ops-" + uniq
        # initial_binding 必填 —— failover 判的是「當前 binding 的 provider
        # 撞限額了沒」,沒有 binding 就沒有來源可交接(端點回 404)。
        t3code_runtime.create_app_thread(
            bridge.CANON_DB, "test-owner", "Ops", self.tid,
            initial_binding=("claude_code", self.src))
        for k in ("POCKET_FAILOVER_MODE", "POCKET_FAILOVER_ORDER"):
            os.environ.pop(k, None)
        try:
            os.remove(usage_limits._CONFIG_FILE)
        except OSError:
            pass
        # 狀態與 availability 都注入,測試不依賴這台機器裝了什麼。
        self._real_status = bridge._failover_status_for
        self._real_avail = bridge._failover_availability
        self.status = {"phase": "idle"}
        bridge._failover_status_for = self._fake_status
        bridge._failover_availability = lambda: {
            p: {"installed": True, "logged_in": True}
            for p in bridge._PROVIDER_CAPS}

    async def _fake_status(self, _sid):
        return self.status

    def tearDown(self):
        bridge._failover_status_for = self._real_status
        bridge._failover_availability = self._real_avail

    def test_preview_reports_none_when_not_limited(self):
        r = self.client.get(f"/app/v2/threads/{self.tid}/failover",
                            headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["limited"])
        self.assertEqual(body["action"], "none")
        self.assertEqual(body["current"], "claude_code")

    def test_preview_suggests_with_a_card_when_limited(self):
        self.status = _limited()
        r = self.client.get(f"/app/v2/threads/{self.tid}/failover",
                            headers=self.auth)
        body = r.json()
        self.assertTrue(body["limited"])
        self.assertEqual(body["action"], "suggest")
        self.assertIn(body["plan"]["provider"], bridge._TRANSFER_TARGETS)
        self.assertEqual(body["card"]["kind"], "choices")
        # app 畫按鈕用結構化的 plan,不是卡片上的假按鈕(§19.4)
        self.assertEqual(body["card"]["options"], [])
        self.assertTrue(body["card"]["detail"])

    def test_preview_does_not_switch_anything(self):
        self.status = _limited()
        self.client.get(f"/app/v2/threads/{self.tid}/failover",
                        headers=self.auth)
        binding = self.t3.current_binding(bridge.CANON_DB, self.tid)
        self.assertEqual(binding["provider"], "claude_code")

    def test_execute_refuses_when_not_limited(self):
        """這支是「額度耗盡」的出口,不是通用搬家鍵(那是 /transfer)。"""
        r = self.client.post(f"/app/v2/threads/{self.tid}/failover",
                             headers=self.auth, json={})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "FAILOVER_NOT_LIMITED")

    def test_execute_rejects_a_provider_without_handoff_landing(self):
        self.status = _limited()
        r = self.client.post(f"/app/v2/threads/{self.tid}/failover",
                             headers=self.auth, json={"to_provider": "gemini"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "FAILOVER_BAD_PROVIDER")

    def test_execute_hands_off_via_context_transfer(self):
        """切換 = 新 session + milestone 摘要交棒,**復用**既有管線。"""
        self.status = _limited()
        seen = {}

        async def _fake_spawn(request, prompt, cwd):
            seen["prompt"] = prompt
            return "codex:handed-off"
        real = bridge._transfer_spawn_codex
        bridge._transfer_spawn_codex = _fake_spawn
        try:
            r = self.client.post(f"/app/v2/threads/{self.tid}/failover",
                                 headers=self.auth,
                                 json={"to_provider": "codex"})
        finally:
            bridge._transfer_spawn_codex = real

        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["new_session_id"], "codex:handed-off")
        self.assertEqual(body["failover"]["from"], "claude_code")
        self.assertEqual(body["failover"]["to"], "codex")
        self.assertEqual(body["failover"]["mode"], "suggest")
        self.assertTrue(body["failover"]["limited"])
        # 交棒 prompt 真的帶了接手指示(ContextTransfer v1 的產物)
        self.assertIn("接手", seen["prompt"])
        # auto 卡片:自動切不得靜默
        self.assertIn("codex:handed-off", body["card"]["detail"])
        # binding 真的換過去了
        binding = self.t3.current_binding(bridge.CANON_DB, self.tid)
        self.assertEqual(binding["provider"], "codex")
        self.assertEqual(binding["provider_thread_id"], "codex:handed-off")

    def test_execute_uses_the_policy_target_when_none_given(self):
        self.status = _limited()
        os.environ["POCKET_FAILOVER_ORDER"] = "cc2,codex"
        calls = {}

        async def _fake_spawn(request, prompt, workdir, name):
            calls["name"] = name
            return f"cc2:{name}"
        real = bridge._transfer_spawn_cc2
        bridge._transfer_spawn_cc2 = _fake_spawn
        try:
            r = self.client.post(f"/app/v2/threads/{self.tid}/failover",
                                 headers=self.auth, json={})
        finally:
            bridge._transfer_spawn_cc2 = real
            os.environ.pop("POCKET_FAILOVER_ORDER", None)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["failover"]["to"], "cc2")

    def test_force_allows_switching_without_a_limit(self):
        async def _fake_spawn(request, prompt, cwd):
            return "codex:forced"
        real = bridge._transfer_spawn_codex
        bridge._transfer_spawn_codex = _fake_spawn
        try:
            r = self.client.post(f"/app/v2/threads/{self.tid}/failover",
                                 headers=self.auth,
                                 json={"to_provider": "codex", "force": True})
        finally:
            bridge._transfer_spawn_codex = real
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["failover"]["forced"])
        self.assertFalse(r.json()["failover"]["limited"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
