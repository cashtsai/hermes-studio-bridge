"""S3:小任務文字生成分離(text_gen)。

釘住三件最容易回歸的事:

1. **未設定時的退路** —— `generate()` 回 `ok=False, reason="not_configured"`,
   呼叫端照舊走規則式/原稿。這是靜默失敗紅線的正面:功能不准因為沒設定
   textgen 而消失。
2. **lane 順序與降級** —— 便宜 API → 本機 Ollama → 放棄;每一段失敗都有
   可辨識的 reason,絕不無聲。
3. **用量留痕** —— 每一次呼叫(含失敗)都餵 usage sink 一筆,成本查得到。

加上 roster 的惰性升級(規則式先頂、背景生成、下輪換上)與 config 端點
的「key 不回傳」契約。
"""
import _isolation  # noqa: F401
import asyncio
import json
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="textgen-s3-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import text_gen  # noqa: E402


# ── httpx 假替身 ────────────────────────────────────────────────────────────
# text_gen 在 `_lane_api` 裡才 `import httpx`(repo 慣例),所以蓋模組屬性
# 就夠 —— 不必動 sys.modules。

_CALLS: list = []
_SCRIPT: list = []          # [(status_code, payload_or_exception), …]


class _FakeResp:
    def __init__(self, code, payload):
        self.status_code = code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    def __init__(self, *a, **kw):
        self.kw = kw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):   # noqa: A002
        _CALLS.append({"url": url, "json": json, "headers": headers or {}})
        code, payload = _SCRIPT.pop(0) if _SCRIPT else (200, _ok_body("x"))
        if isinstance(payload, Exception) and code == 0:
            raise payload
        return _FakeResp(code, payload)


def _ok_body(text, usage=None):
    body = {"choices": [{"message": {"role": "assistant", "content": text}}]}
    if usage is not None:
        body["usage"] = usage
    return body


class _TextGenBase(unittest.TestCase):
    def setUp(self):
        import httpx
        self._httpx = httpx
        self._real_client = httpx.AsyncClient
        httpx.AsyncClient = _FakeClient
        _CALLS.clear()
        _SCRIPT.clear()
        self.sink: list = []
        text_gen.set_usage_sink(self.sink.append)
        text_gen.reset_local_cooldown()
        # 每個測試從「沒設定」起跑,互不污染。
        for k in ("POCKET_TEXTGEN_BASE_URL", "POCKET_TEXTGEN_KEY",
                  "POCKET_TEXTGEN_MODEL"):
            os.environ.pop(k, None)
        try:
            os.remove(text_gen._CONFIG_FILE)
        except OSError:
            pass

    def tearDown(self):
        self._httpx.AsyncClient = self._real_client
        text_gen.set_usage_sink(None)

    def _configure(self, model="cheap-1", key="sk-test",
                   base="https://api.example.test/v1"):
        text_gen.save_config(base_url=base, api_key=key, model=model)


class NotConfiguredTest(_TextGenBase):
    """未設定 → 誠實失敗 + 留痕,**不是**例外、也不是假成功。"""

    def test_generate_reports_not_configured(self):
        res = asyncio.run(text_gen.generate("roster.summary", "做了些事"))
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "not_configured")
        self.assertEqual(res.text, "")
        self.assertFalse(text_gen.configured())
        # 一行都沒打出去 —— 沒設定不該戳任何網路。
        self.assertEqual(_CALLS, [])

    def test_not_configured_still_leaves_usage_trace(self):
        """沒設定也要留一筆 —— 不然「為什麼摘要還是規則式」永遠查不到。"""
        asyncio.run(text_gen.generate("roster.summary", "做了些事"))
        self.assertEqual(len(self.sink), 1)
        rec = self.sink[0]
        self.assertEqual(rec["task"], "roster.summary")
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["reason"], "not_configured")

    def test_empty_prompt_short_circuits(self):
        self._configure()
        res = asyncio.run(text_gen.generate("roster.summary", "   \n  "))
        self.assertEqual(res.reason, "empty_prompt")
        self.assertEqual(_CALLS, [])

    def test_base_url_without_model_is_not_configured(self):
        """只設 base_url 不算設定好 —— 猜一個 model id 只會換來 404。"""
        text_gen.save_config(base_url="https://api.example.test/v1",
                             api_key="k", model="")
        self.assertFalse(text_gen.configured())


class ApiLaneTest(_TextGenBase):
    def test_happy_path_strips_decoration_and_records_usage(self):
        self._configure()
        _SCRIPT.append((200, _ok_body(
            "```\n摘要:「部署腳本改好了」\n```",
            {"prompt_tokens": 120, "completion_tokens": 9,
             "total_tokens": 129})))
        res = asyncio.run(text_gen.generate("roster.summary", "一些里程碑",
                                            session="codex:abc"))
        self.assertTrue(res.ok, res)
        # 圍欄/前綴/引號都拆掉 —— 不准讓反引號出現在 wire 上。
        self.assertEqual(res.text, "部署腳本改好了")
        self.assertEqual(res.usage["total_tokens"], 129)
        self.assertTrue(res.usage["reported"])
        rec = self.sink[-1]
        self.assertEqual(rec["usage_total_tokens"], 129)
        self.assertEqual(rec["session"], "codex:abc")
        self.assertEqual(rec["lanes"], "api")

    def test_request_shape_is_openai_compatible(self):
        self._configure(model="cheap-1")
        _SCRIPT.append((200, _ok_body("ok")))
        asyncio.run(text_gen.generate("session.title", "素材",
                                      system="你是標題機", max_tokens=48))
        call = _CALLS[0]
        self.assertEqual(call["url"],
                         "https://api.example.test/v1/chat/completions")
        self.assertEqual(call["json"]["model"], "cheap-1")
        self.assertEqual(call["json"]["max_tokens"], 48)
        self.assertFalse(call["json"]["stream"])
        self.assertEqual([m["role"] for m in call["json"]["messages"]],
                         ["system", "user"])
        self.assertEqual(call["headers"]["authorization"], "Bearer sk-test")

    def test_local_server_without_key_sends_no_auth_header(self):
        """Ollama / LM Studio 不需要 key —— 不准硬塞一個空 Bearer。"""
        text_gen.save_config(base_url="http://127.0.0.1:1234/v1",
                             api_key="", model="local-small")
        self.assertTrue(text_gen.configured())
        _SCRIPT.append((200, _ok_body("ok")))
        asyncio.run(text_gen.generate("roster.summary", "素材"))
        self.assertNotIn("authorization", _CALLS[0]["headers"])

    def test_http_error_degrades_with_reason(self):
        self._configure()
        _SCRIPT.append((429, {}))
        res = asyncio.run(text_gen.generate("roster.summary", "素材"))
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "http_429")
        self.assertEqual(self.sink[-1]["reason"], "http_429")

    def test_transport_exception_never_raises(self):
        self._configure()
        _SCRIPT.append((0, RuntimeError("connection reset")))
        res = asyncio.run(text_gen.generate("roster.summary", "素材"))
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "transport")

    def test_usage_absent_is_reported_false_not_zero(self):
        """端點不回 usage 時寫 0 會讓對帳說謊 —— 要誠實標 reported=False。"""
        self._configure()
        _SCRIPT.append((200, _ok_body("好了")))
        res = asyncio.run(text_gen.generate("roster.summary", "素材"))
        self.assertTrue(res.ok)
        self.assertFalse(res.usage["reported"])
        self.assertNotIn("total_tokens", res.usage)

    def test_empty_text_records_usage_because_money_was_spent(self):
        self._configure()
        _SCRIPT.append((200, _ok_body("", {"prompt_tokens": 50,
                                           "completion_tokens": 0,
                                           "total_tokens": 50})))
        res = asyncio.run(text_gen.generate("roster.summary", "素材"))
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "empty_text")
        self.assertEqual(res.usage["total_tokens"], 50)


class LocalLaneTest(_TextGenBase):
    """本機 lane 是**既有行為的保存者** —— 便宜 API 沒設定時一定要走得到。"""

    def setUp(self):
        super().setUp()
        os.environ["POCKET_TEXTGEN_LOCAL"] = "1"
        from harness import model as harness_model
        self.hm = harness_model
        self._real_ollama = harness_model.ollama_text

    def tearDown(self):
        self.hm.ollama_text = self._real_ollama
        os.environ["POCKET_TEXTGEN_LOCAL"] = "0"
        super().tearDown()

    def _stub_ollama(self, result):
        async def _fake(prompt, **kw):
            self.last_prompt = prompt
            if isinstance(result, Exception):
                raise result
            return result
        self.hm.ollama_text = _fake

    def test_local_serves_when_api_unconfigured(self):
        self._stub_ollama("清好的稿子")
        res = asyncio.run(text_gen.generate("transcript.cleanup", "原稿",
                                            system="你是校對"))
        self.assertTrue(res.ok, res)
        self.assertEqual(res.text, "清好的稿子")
        self.assertEqual(self.sink[-1]["lanes"], "local")
        # system 當前綴併進 prompt(harness_model 只吃單一 prompt)
        self.assertIn("你是校對", self.last_prompt)
        self.assertIn("原稿", self.last_prompt)

    def test_api_failure_cascades_to_local(self):
        """上游掛了就用本機補 —— 「安靜地沒摘要」比多花 200ms 糟得多。"""
        self._configure()
        _SCRIPT.append((503, {}))
        self._stub_ollama("本機生的")
        res = asyncio.run(text_gen.generate("roster.summary", "素材"))
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "本機生的")
        self.assertEqual(self.sink[-1]["lanes"], "api,local")

    def test_allow_local_false_stays_on_api(self):
        self._configure()
        _SCRIPT.append((503, {}))
        self._stub_ollama("不該被叫到")
        res = asyncio.run(text_gen.generate("roster.summary", "素材",
                                            allow_local=False))
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "http_503")
        self.assertEqual(self.sink[-1]["lanes"], "api")

    def _counting_stub(self, exc):
        calls = []

        async def _fake(prompt, **kw):
            calls.append(prompt)
            raise exc
        self.hm.ollama_text = _fake
        return calls

    def test_transport_failure_sets_cooldown(self):
        """連不上(沒裝/沒開)→ 冷卻,不准每個回合都再撞一次 connection
        refused(log 會被洗版、熱路徑白等)。用「有沒有真的再打一次」驗,
        不看 reason —— reason 會被 not_configured 蓋掉(那是刻意的)。"""
        calls = self._counting_stub(RuntimeError("connect refused"))
        r1 = asyncio.run(text_gen.generate("roster.summary", "素材"))
        self.assertEqual(r1.reason, "local_transport")
        self.assertEqual(len(calls), 1)
        asyncio.run(text_gen.generate("roster.summary", "素材"))
        self.assertEqual(len(calls), 1, "冷卻期內不該再打 Ollama")

    def test_cooldown_can_be_reset_so_a_late_ollama_is_picked_up(self):
        """使用者中途把 Ollama 開起來要能自己恢復 —— 絕不永久放棄。"""
        calls = self._counting_stub(RuntimeError("connect refused"))
        asyncio.run(text_gen.generate("roster.summary", "素材"))
        text_gen.reset_local_cooldown()
        asyncio.run(text_gen.generate("roster.summary", "素材"))
        self.assertEqual(len(calls), 2)

    def test_timeout_does_not_set_cooldown(self):
        """逾時是「在跑但慢」,不是缺席 —— 當成缺席會害慢機器永遠沒本機 lane。"""
        calls = self._counting_stub(asyncio.TimeoutError())
        r1 = asyncio.run(text_gen.generate("roster.summary", "素材"))
        self.assertEqual(r1.reason, "local_timeout")
        asyncio.run(text_gen.generate("roster.summary", "素材"))
        self.assertEqual(len(calls), 2, "逾時不該觸發冷卻")

    def test_disabled_local_is_not_called_and_does_not_mask_not_configured(self):
        """關掉本機 lane = 不准真的去打 Ollama;而且回報的 reason 要是
        `not_configured`(能動作的那個),不是 `local_disabled`(實作細節)。"""
        os.environ["POCKET_TEXTGEN_LOCAL"] = "0"
        called = []

        async def _fake(prompt, **kw):
            called.append(prompt)
            return "不該被叫到"
        self.hm.ollama_text = _fake

        res = asyncio.run(text_gen.generate("roster.summary", "素材"))
        self.assertEqual(called, [])
        self.assertEqual(res.reason, "not_configured")

    def test_local_timeout_outranks_not_configured(self):
        """本機真的跑到模型才失敗是**新情報**(有、但不行),該蓋掉
        not_configured —— 不然使用者會以為自己沒設定。"""
        self._stub_ollama(asyncio.TimeoutError())
        res = asyncio.run(text_gen.generate("roster.summary", "素材"))
        self.assertEqual(res.reason, "local_timeout")


class TaskWrapperTest(_TextGenBase):
    def test_summary_length_is_hard_clamped(self):
        """prompt 只是請求,小模型不一定聽 —— wire 契約(§17 ≤12 字)靠硬閘。"""
        self._configure()
        _SCRIPT.append((200, _ok_body("一二三四五六七八九十十一十二十三")))
        res = asyncio.run(text_gen.summarize_turn("素材", max_chars=12))
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "一二三四五六七八九十十一"[:12] + "…")
        self.assertLessEqual(len(res.text), 13)

    def test_transcript_input_budget_is_bigger_than_default(self):
        """會議逐字稿不能套摘要的 6000 字上限 —— 會安靜吃掉開頭。"""
        self.assertGreater(text_gen.TRANSCRIPT_MAX_INPUT_CHARS,
                           text_gen.MAX_INPUT_CHARS)
        self._configure()
        _SCRIPT.append((200, _ok_body("清好了")))
        long_raw = "甲" * 20000
        asyncio.run(text_gen.clean_transcript(long_raw))
        sent = _CALLS[0]["json"]["messages"][-1]["content"]
        self.assertEqual(len(sent), 20000)      # 沒被 6000 砍掉

    def test_clip_keeps_the_tail(self):
        """超長取尾段 —— 摘要要的是「最近發生什麼」。"""
        clipped = text_gen._clip("頭" * 10 + "尾" * 10, 10)
        self.assertEqual(clipped, "尾" * 10)


class ConfigTest(_TextGenBase):
    def test_merge_write_does_not_wipe_other_fields(self):
        text_gen.save_config(base_url="https://a.test/v1", api_key="k1",
                             model="m1")
        text_gen.save_config(model="m2")
        cfg = text_gen.load_config()
        self.assertEqual(cfg["api_key"], "k1")
        self.assertEqual(cfg["model"], "m2")
        self.assertEqual(cfg["base_url"], "https://a.test/v1")

    def test_env_wins_and_sources_are_independent(self):
        text_gen.save_config(base_url="https://file.test/v1", api_key="fk",
                             model="file-model")
        os.environ["POCKET_TEXTGEN_MODEL"] = "env-model"
        cfg = text_gen.load_config()
        self.assertEqual(cfg["model"], "env-model")
        self.assertEqual(cfg["model_source"], "env")
        # 只設 env model 不該讓 file 的 key 消失
        self.assertEqual(cfg["api_key"], "fk")
        self.assertEqual(cfg["api_key_source"], "file")

    def test_trailing_slash_in_base_url_is_normalised(self):
        text_gen.save_config(base_url="https://a.test/v1/", api_key="k",
                             model="m")
        _SCRIPT.append((200, _ok_body("ok")))
        asyncio.run(text_gen.generate("roster.summary", "素材"))
        self.assertEqual(_CALLS[0]["url"], "https://a.test/v1/chat/completions")

    def test_public_config_never_echoes_the_key(self):
        text_gen.save_config(base_url="https://a.test/v1",
                             api_key="sk-super-secret", model="m")
        pub = text_gen.config_public()
        self.assertTrue(pub["key_set"])
        self.assertNotIn("api_key", pub)
        self.assertNotIn("sk-super-secret", json.dumps(pub, ensure_ascii=False))

    def test_empty_string_clears_a_field(self):
        text_gen.save_config(base_url="https://a.test/v1", api_key="k",
                             model="m")
        text_gen.save_config(base_url="")
        self.assertFalse(text_gen.configured())

    def test_bad_config_file_degrades_to_unconfigured(self):
        with open(text_gen._CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertFalse(text_gen.configured())
        self.assertEqual(text_gen.load_config()["base_url_source"], "none")


class SinkTest(_TextGenBase):
    def test_broken_sink_does_not_break_generation(self):
        """對帳管線壞掉不准拖垮摘要生成。"""
        def _boom(_rec):
            raise RuntimeError("sink down")
        text_gen.set_usage_sink(_boom)
        self._configure()
        _SCRIPT.append((200, _ok_body("好了")))
        res = asyncio.run(text_gen.generate("roster.summary", "素材"))
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "好了")


# ── bridge 側接線 ──────────────────────────────────────────────────────────

import bridge  # noqa: E402


class BridgeWiringTest(unittest.TestCase):
    def setUp(self):
        bridge._TURN_SUMMARY.clear()
        bridge._TURN_SUMMARY_TRIED.clear()
        bridge._TURN_SUMMARY_LOADED = True   # 別去讀真檔

    def test_usage_event_type_is_registered(self):
        """新事件型別必須先登記才可發射(B1 鐵則,event_log 完整性測試會抓)。"""
        self.assertIn("textgen.usage", bridge.EVENT_PERSIST_ONLY)
        self.assertNotIn("textgen.usage", bridge.EVENT_PERSISTED)

    def test_sink_without_session_skips_event_log(self):
        """沒有歸屬的雜務不該在 event_log 開一個假 session 鍵去騙查帳的人。"""
        calls = []
        real = bridge._event_append
        bridge._event_append = lambda *a, **kw: calls.append(a)
        try:
            bridge._textgen_usage_sink({"task": "t", "session": "", "ok": True})
            self.assertEqual(calls, [])
            bridge._textgen_usage_sink({"task": "t", "session": "codex:z",
                                        "ok": True})
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], "codex:z")
            self.assertEqual(calls[0][1], "textgen.usage")
        finally:
            bridge._event_append = real

    def test_cached_summary_requires_matching_material_hash(self):
        """素材變了就不准再用舊摘要(不然列表會顯示上一回合的事)。"""
        raw = "部署腳本改好了,測試全綠"
        bridge._TURN_SUMMARY["codex:a"] = {
            "hash": bridge._turn_summary_key(raw),
            "text": "部署腳本已修好", "ts": 0.0}
        self.assertEqual(bridge._turn_summary_cached("codex:a", raw),
                         "部署腳本已修好")
        self.assertEqual(bridge._turn_summary_cached("codex:a", "別的素材"), "")
        self.assertEqual(bridge._turn_summary_cached("codex:nope", raw), "")

    def test_schedule_outside_event_loop_is_a_no_op(self):
        """沒有事件迴圈時安靜跳過 —— 規則式照走,不准 raise。"""
        bridge._turn_summary_schedule("codex:a", "素材")
        self.assertEqual(bridge._TURN_SUMMARY, {})

    def test_schedule_tries_each_material_once(self):
        """同一筆素材只試一次 —— 否則沒設定的機器每 3 秒丟一個註定失敗的任務。"""
        async def _drive():
            bridge._turn_summary_schedule("codex:a", "素材")
            bridge._turn_summary_schedule("codex:a", "素材")
            await asyncio.sleep(0)
        asyncio.run(_drive())
        self.assertEqual(len(bridge._TURN_SUMMARY_TRIED), 1)

    def test_polish_system_prompt_drops_the_trailing_label(self):
        """走 chat API 時「逐字稿:」標籤會變成沒有下文的殘句。"""
        self.assertTrue(bridge._MEETING_POLISH_PROMPT.rstrip().endswith("逐字稿:"))
        self.assertFalse(bridge._MEETING_POLISH_SYSTEM.rstrip().endswith("逐字稿:"))
        # 專有名詞清單是實測資產,搬家時不准掉
        self.assertIn("Hermes", bridge._MEETING_POLISH_SYSTEM)
        self.assertIn("善彰", bridge._MEETING_POLISH_SYSTEM)


class ConfigEndpointTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(bridge.app)
        self.auth = {"Authorization": "Bearer " + bridge.BRIDGE_TOKEN}
        for k in ("POCKET_TEXTGEN_BASE_URL", "POCKET_TEXTGEN_KEY",
                  "POCKET_TEXTGEN_MODEL"):
            os.environ.pop(k, None)
        try:
            os.remove(text_gen._CONFIG_FILE)
        except OSError:
            pass

    def test_requires_auth(self):
        self.assertIn(self.client.get("/app/v1/textgen/config").status_code,
                      (401, 429))

    def test_get_reports_unconfigured_with_a_fallback_note(self):
        r = self.client.get("/app/v1/textgen/config", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["configured"])
        self.assertFalse(body["key_set"])
        # 未設定 ≠ 功能壞掉。契約要把退路講清楚,app 才不會畫「未啟用」。
        self.assertTrue(body["fallback_note"])
        self.assertTrue(body["suggested_models"])

    def test_put_round_trip_never_echoes_the_key(self):
        r = self.client.put("/app/v1/textgen/config", headers=self.auth,
                            json={"base_url": "https://api.example.test/v1",
                                  "api_key": "sk-secret-xyz",
                                  "model": "cheap-1"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("sk-secret-xyz", r.text)
        self.assertTrue(r.json()["configured"])
        got = self.client.get("/app/v1/textgen/config",
                              headers=self.auth).json()
        self.assertEqual(got["model"], "cheap-1")
        self.assertTrue(got["key_set"])

    def test_put_with_no_known_field_is_400(self):
        r = self.client.put("/app/v1/textgen/config", headers=self.auth,
                            json={"nonsense": 1})
        self.assertEqual(r.status_code, 400)

    def test_put_resets_the_tried_set(self):
        """設好 key 之後不該還要等素材變才看得到效果(體感「設了沒用」)。"""
        bridge._TURN_SUMMARY_TRIED.add(("codex:a", "deadbeef"))
        self.client.put("/app/v1/textgen/config", headers=self.auth,
                        json={"model": "cheap-1"})
        self.assertEqual(bridge._TURN_SUMMARY_TRIED, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
