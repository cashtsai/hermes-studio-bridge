"""agent_context 跨 session 上下文互讀 —— 遮罩/權限分級/快取/留痕/封頂鐵律。

涵蓋:
- **遮罩**:key 形狀的字串在 recent/search/summary 三條路都活不下來,
  連「餵給本機模型的素材」也已遮罩(model 一律 mock,不打真的 LLM)。
- **權限分級**:母子邊放行、陌生人 default DENY、context_targets 明示放行、
  summary 比 recent/search 寬(且可用 env 調級)。
- **旗標**:AGENT_CONTEXT 未開 → 404。
- **快取**:同一個 (session, 內容 seq) 重讀免費;新卡片進來即失效;
  audit 卡自己**不算**新內容(否則快取永遠 miss)。
- **封頂**:回應字元硬上限 + truncated 旗標;recent limit 上限。
- **留痕**:每次讀取在被讀 session 的卡片流落一張 👁 卡。
- **速率**:每 caller 每分鐘上限 → 429。
- **搜尋範圍即權限**:讀不到的 session 連命中都看不到。
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_TMP = tempfile.mkdtemp(prefix="agent-context-test-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("POCKET_REGISTRY_DB",
                      os.path.join(_TMP, "bridge-registry.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_context  # noqa: E402
import agent_registry  # noqa: E402
import bridge  # noqa: E402
import carddigest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

CALLER = "claude_code:tirith"
CHILD = "claude_code:kid"          # CALLER 的子 session(母子邊)
FRIEND = "codex:friend"            # 靠政策放行
STRANGER = "codex:stranger"        # 誰都不放行

_KNOWN = {CALLER: ("cc", "tirith", "/tmp"), CHILD: ("cc", "kid", "/tmp"),
          FRIEND: ("cx", "friend"), STRANGER: ("cx", "stranger")}

SECRETS = [
    "sk-ant-api03-AbCdEf1234567890abcdefGHIJKLMNOP",
    "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    "github_pat_11ABCDEFG0abcdefghijklmnop",
    "AKIAIOSFODNN7EXAMPLE",
    "AIzaSyD-1234567890abcdefghijklmnopqrstuv",
    "xoxb-1234567890-abcdefghijkl",
    "123456789:AAF-abcdefghijklmnopqrstuvwxyz0123456",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
    "Authorization: Bearer abcdef1234567890ABCDEF",
    "ANTHROPIC_API_KEY=sk-ant-secretvalue-0123456789",
    "password: hunter2hunter2",
]


def _write_policy(rules) -> str:
    path = tempfile.mktemp(suffix=".json", dir=_TMP)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "rules": rules}, f, ensure_ascii=False)
    return path


def _fresh_registry() -> agent_registry.AgentRegistry:
    return agent_registry.AgentRegistry(
        tempfile.mktemp(suffix=".db", dir=_TMP),
        task_ttl=100.0, ephemeral_ttl=50.0, max_children=3,
        task_cap=12, max_depth=2, idle_secs=10.0)


class FakeRequest:
    headers = {"authorization": "Bearer test-unit-token"}
    client = None

    def __init__(self, body=None):
        self._body = body or {}

    async def json(self):
        return self._body


def _fake_card_source(sid):
    if sid in _KNOWN:
        return _KNOWN[sid]
    raise bridge.http_err(404, "SESSION_NOT_FOUND", "unknown session")


class _Env:
    """agent_context 端點測試的標準 patch 組。"""

    def __init__(self, rules=None, extra_env=None, summary="摘要:在做 X。"):
        self.reg = _fresh_registry()
        # 戶口:CHILD 掛在 CALLER 名下 → 母子邊;FRIEND/STRANGER 是陌生人。
        self.reg.register(CALLER, provider="claude_code", purpose="主線開發",
                          enforce_quota=False)
        self.reg.register(CHILD, provider="claude_code", purpose="子任務:測試",
                          parent=CALLER, enforce_quota=False)
        self.reg.register(FRIEND, provider="codex", purpose="友軍 thread",
                          enforce_quota=False)
        self.reg.register(STRANGER, provider="codex", purpose="不相干 thread",
                          enforce_quota=False)
        policy = _write_policy(rules if rules is not None else [])
        env = {"AGENT_CONTEXT": "1", "AGENT_CALL_POLICY": policy}
        env.update(extra_env or {})
        self.stores = {}
        self.summarize = AsyncMock(return_value=summary)

        async def _store_for(sid):
            _fake_card_source(sid)          # 未知 session 照樣 404
            if sid not in self.stores:
                self.stores[sid] = carddigest.SessionCardStore()
            return self.stores[sid]

        self._patches = [
            patch.dict(os.environ, env),
            patch.object(bridge, "REGISTRY", self.reg),
            patch.object(bridge, "_v2_card_source",
                         side_effect=_fake_card_source),
            patch.object(bridge, "_v2_card_store", side_effect=_store_for),
            patch.object(bridge, "_registry_ensure_personas", MagicMock()),
            patch.object(bridge, "_registry_is_busy",
                         AsyncMock(return_value=False)),
            patch.object(bridge, "_registry_card_store",
                         side_effect=lambda sid: self.stores.get(sid)),
            patch.object(bridge, "_agent_context_summarize", self.summarize),
        ]

    def __enter__(self):
        bridge._AGENT_CONTEXT_SUMMARY_CACHE.clear()
        bridge._AGENT_CONTEXT_HITS.clear()
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *a):
        for p in reversed(self._patches):
            p.stop()

    def seed(self, sid, texts, role="assistant"):
        store = self.stores.setdefault(sid, carddigest.SessionCardStore())
        for i, t in enumerate(texts):
            store.upsert_card(carddigest.make_card(
                f"card-{sid}-{store.seq}-{i}", "", role, "text",
                {"text": t, "fallback_text": t}))
        return store

    def cards(self, sid):
        store = self.stores.get(sid)
        return list(store.cards.values()) if store else []


def _read(body):
    return asyncio.run(bridge.v2_agent_context(FakeRequest(body)))


def _targets(caller, mode=""):
    return asyncio.run(bridge.v2_agent_context_targets(
        FakeRequest(), caller=caller, mode=mode))


# ───────────────────────── 遮罩(純函式)─────────────────────────

class TestRedaction(unittest.TestCase):
    def test_every_secret_shape_dies(self):
        for s in SECRETS:
            out = agent_context.redact_text(f"前面 {s} 後面")
            self.assertIn(agent_context.REDACTED, out, s)
            # 原字串的「有辨識度的那一段」不得倖存
            core = s.split("=")[-1].split(": ")[-1].strip()
            self.assertNotIn(core, out, f"{s} 沒被遮乾淨:{out}")

    def test_key_value_keeps_key_name_drops_value(self):
        out = agent_context.redact_text("BRIDGE_TOKEN=abcd1234efgh5678")
        self.assertNotIn("abcd1234efgh5678", out)
        self.assertIn("BRIDGE_TOKEN", out)

    def test_pem_block(self):
        pem = ("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\nxyz\n"
               "-----END RSA PRIVATE KEY-----")
        self.assertNotIn("MIIabc", agent_context.redact_text(pem))

    def test_normal_text_untouched(self):
        s = "改了 bridge.py 第 300 行,跑 tests/test_agent_call.py 全過"
        self.assertEqual(agent_context.redact_text(s), s)

    def test_card_line_and_snippet_redact(self):
        line = agent_context.card_line("assistant", f"key={SECRETS[0]}", 0)
        self.assertNotIn("sk-ant", line)
        frag = agent_context.match_snippet(f"前文 {SECRETS[1]} 後文", "前文")
        self.assertNotIn("ghp_", frag)

    def test_clip_marks_truncation(self):
        text, trunc = agent_context.clip("x" * 100, 10)
        self.assertTrue(trunc)
        self.assertTrue(text.startswith("x" * 10))
        text, trunc = agent_context.clip("short", 10)
        self.assertFalse(trunc)


# ───────────────────────── 政策分級(純邏輯)─────────────────────────

class TestPolicyTiering(unittest.TestCase):
    def test_default_deny(self):
        pol = agent_context.load_policy(os.path.join(_TMP, "nope.json"))
        for mode in agent_context.MODES:
            ok, why = agent_context.decide(pol, CALLER, STRANGER, mode)
            self.assertFalse(ok)
            self.assertIn("default DENY", why)

    def test_family_edge_all_modes(self):
        pol = agent_context.load_policy(_write_policy([]))
        for mode in agent_context.MODES:
            self.assertTrue(agent_context.decide(pol, CALLER, CHILD, mode,
                                                 family=True)[0])

    def test_family_modes_configurable(self):
        pol = agent_context.load_policy(_write_policy([]))
        with patch.dict(os.environ, {"AGENT_CONTEXT_FAMILY_MODES": "summary"}):
            self.assertTrue(agent_context.decide(pol, CALLER, CHILD, "summary",
                                                 family=True)[0])
            self.assertFalse(agent_context.decide(pol, CALLER, CHILD, "recent",
                                                  family=True)[0])

    def test_context_rule_defaults_to_summary_only(self):
        pol = agent_context.load_policy(_write_policy(
            [{"caller": CALLER, "context_targets": ["codex:*"]}]))
        self.assertTrue(agent_context.decide(pol, CALLER, FRIEND, "summary")[0])
        self.assertFalse(agent_context.decide(pol, CALLER, FRIEND, "recent")[0])
        self.assertFalse(agent_context.decide(pol, CALLER, FRIEND, "search")[0])

    def test_context_rule_explicit_modes(self):
        pol = agent_context.load_policy(_write_policy(
            [{"caller": CALLER, "context_targets": [FRIEND],
              "modes": ["summary", "recent", "search"]}]))
        for mode in agent_context.MODES:
            self.assertTrue(agent_context.decide(pol, CALLER, FRIEND, mode)[0])
        # 規則只涵蓋 FRIEND,別人照樣拒
        self.assertFalse(agent_context.decide(pol, CALLER, STRANGER,
                                              "summary")[0])

    def test_agent_call_grant_implies_summary_only(self):
        pol = agent_context.load_policy(_write_policy(
            [{"caller": CALLER, "targets": ["codex:*"]}]))
        ok, basis = agent_context.decide(pol, CALLER, FRIEND, "summary")
        self.assertTrue(ok)
        self.assertEqual(basis, "agent_call")
        self.assertFalse(agent_context.decide(pol, CALLER, FRIEND, "recent")[0])
        with patch.dict(os.environ, {"AGENT_CONTEXT_CALL_MODES": ""}):
            self.assertFalse(agent_context.decide(pol, CALLER, FRIEND,
                                                  "summary")[0])

    def test_self_read_denied_and_bad_mode(self):
        pol = agent_context.load_policy(_write_policy(
            [{"caller": "*", "context_targets": ["*"],
              "modes": ["summary", "recent", "search"]}]))
        self.assertFalse(agent_context.decide(pol, CALLER, CALLER, "summary",
                                              family=True)[0])
        self.assertFalse(agent_context.decide(pol, CALLER, FRIEND, "bogus")[0])

    def test_empty_modes_list_grants_nothing(self):
        pol = agent_context.load_policy(_write_policy(
            [{"caller": CALLER, "context_targets": ["*"], "modes": []}]))
        self.assertFalse(agent_context.decide(pol, CALLER, FRIEND, "summary")[0])

    def test_policy_file_shared_with_agent_call(self):
        """同一份檔案、同一個 rule 物件可以同時帶調用權與讀取權。"""
        path = _write_policy([{"caller": CALLER, "targets": ["codex:*"],
                               "context_targets": [CHILD],
                               "modes": ["recent"]}])
        pol = agent_context.load_policy(path)
        self.assertEqual(len(pol["context_rules"]), 1)
        self.assertEqual(len(pol["call_rules"]), 1)
        self.assertTrue(agent_context.decide(pol, CALLER, CHILD, "recent")[0])

    def test_is_family_edge_both_directions(self):
        parent_row = {"parent": None}
        child_row = {"parent": CALLER}
        self.assertTrue(agent_context.is_family_edge(
            parent_row, child_row, CALLER, CHILD))
        self.assertTrue(agent_context.is_family_edge(
            child_row, parent_row, CHILD, CALLER))
        self.assertFalse(agent_context.is_family_edge(
            {"parent": None}, {"parent": "other"}, CALLER, FRIEND))


# ───────────────────────── 旗標 ─────────────────────────

class TestFlag(unittest.TestCase):
    def test_disabled_returns_404(self):
        with _Env(extra_env={"AGENT_CONTEXT": "0"}):
            for body in ({"caller": CALLER, "target": CHILD},
                         {"caller": CALLER, "mode": "search", "query": "x"}):
                with self.assertRaises(HTTPException) as cm:
                    _read(body)
                self.assertEqual(cm.exception.status_code, 404)
            with self.assertRaises(HTTPException) as cm:
                _targets(CALLER)
            self.assertEqual(cm.exception.status_code, 404)

    def test_enabled_by_default_is_off(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_CONTEXT", None)
            self.assertFalse(bridge._agent_context_enabled())


# ───────────────────────── 端點:權限 ─────────────────────────

class TestEndpointPermission(unittest.TestCase):
    def test_family_edge_allowed(self):
        with _Env() as env:
            env.seed(CHILD, ["跑完了 pytest,3 個檔案改動"])
            res = _read({"caller": CALLER, "target": CHILD, "mode": "recent"})
            self.assertEqual(res["basis"], "family")
            self.assertIn("pytest", res["content"])
            self.assertEqual(res["purpose"], "子任務:測試")
            self.assertEqual(res["provider"], "claude_code")
            self.assertFalse(res["busy"])
            self.assertIsNotNone(res["last_active_ts"])

    def test_stranger_denied(self):
        with _Env() as env:
            env.seed(STRANGER, ["機密內容"])
            with self.assertRaises(HTTPException) as cm:
                _read({"caller": CALLER, "target": STRANGER, "mode": "summary"})
            self.assertEqual(cm.exception.status_code, 403)

    def test_allowlist_grant_and_summary_vs_recent_tiering(self):
        rules = [{"caller": CALLER, "context_targets": [FRIEND]}]
        with _Env(rules=rules) as env:
            env.seed(FRIEND, ["友軍在改 bridge.py"])
            res = _read({"caller": CALLER, "target": FRIEND})
            self.assertEqual(res["basis"], "context_rule")
            self.assertEqual(res["mode"], "summary")
            # 同一條規則不給原文
            with self.assertRaises(HTTPException) as cm:
                _read({"caller": CALLER, "target": FRIEND, "mode": "recent"})
            self.assertEqual(cm.exception.status_code, 403)

    def test_denied_read_leaks_nothing_and_leaves_trail(self):
        with _Env() as env:
            env.seed(STRANGER, ["機密內容"])
            with self.assertRaises(HTTPException):
                _read({"caller": CALLER, "target": STRANGER, "mode": "recent"})
            trail = [c for c in env.cards(STRANGER)
                     if c["id"].startswith("card-agentctx-")]
            self.assertEqual(len(trail), 1)
            self.assertIn("已被政策拒絕", trail[0]["body"]["text"])

    def test_bad_requests(self):
        with _Env():
            for body, status in (
                    ({"caller": CALLER, "target": CHILD, "mode": "bogus"}, 400),
                    ({"caller": "", "target": CHILD}, 400),
                    ({"caller": CALLER}, 400),                    # 缺 target
                    ({"caller": CALLER, "mode": "search"}, 400),  # 缺 query
                    ({"caller": CALLER, "target": CALLER}, 400),  # 自讀
                    ({"caller": "claude_code:nobody", "target": CHILD}, 400),
                    ({"caller": CALLER, "target": "codex:ghost"}, 404)):
                with self.assertRaises(HTTPException) as cm:
                    _read(body)
                self.assertEqual(cm.exception.status_code, status, body)

    def test_targets_endpoint_lists_modes(self):
        rules = [{"caller": CALLER, "context_targets": [FRIEND]}]
        with _Env(rules=rules):
            out = _targets(CALLER)
            by_id = {t["id"]: t for t in out["targets"]}
            self.assertEqual(by_id[CHILD]["modes"],
                             ["summary", "recent", "search"])
            self.assertTrue(by_id[CHILD]["family"])
            self.assertEqual(by_id[FRIEND]["modes"], ["summary"])
            self.assertNotIn(STRANGER, by_id)
            self.assertEqual(out["tiering"]["context_rule_default"],
                             ["summary"])


# ───────────────────────── 端點:內容/遮罩/封頂 ─────────────────────────

class TestContentSafety(unittest.TestCase):
    def test_recent_redacts_secrets(self):
        with _Env() as env:
            env.seed(CHILD, [f"我把金鑰寫進 .env 了:{s}" for s in SECRETS])
            res = _read({"caller": CALLER, "target": CHILD, "mode": "recent"})
            for s in SECRETS:
                core = s.split("=")[-1].split(": ")[-1].strip()
                self.assertNotIn(core, res["content"])
            self.assertIn(agent_context.REDACTED, res["content"])

    def test_summary_path_redacts_material_and_model_output(self):
        leaked = f"模型不小心複誦:{SECRETS[0]} 與 {SECRETS[1]}"
        with _Env(summary=leaked) as env:
            env.seed(CHILD, [f"export ANTHROPIC_API_KEY={SECRETS[0]}"])
            res = _read({"caller": CALLER, "target": CHILD, "mode": "summary"})
            # (a) 模型輸出裡的 key 不得離開 bridge
            self.assertNotIn("sk-ant", res["content"])
            self.assertNotIn("ghp_", res["content"])
            # (b) 餵給模型的素材本身也已遮罩
            material = env.summarize.call_args[0][0]
            self.assertNotIn("sk-ant", material)
            self.assertIn(agent_context.REDACTED, material)

    def test_summary_falls_back_when_model_unavailable(self):
        with _Env(summary="") as env:
            env.seed(CHILD, ["做到一半"])
            res = _read({"caller": CALLER, "target": CHILD, "mode": "summary"})
            self.assertIn("未經蒸餾", res["content"])
            self.assertIn("做到一半", res["content"])
            # fail-soft 不進快取:下次模型活了要拿得到真摘要
            self.assertFalse(bridge._AGENT_CONTEXT_SUMMARY_CACHE)

    def test_response_char_cap(self):
        with _Env(extra_env={"AGENT_CONTEXT_MAX_CHARS": "200"}) as env:
            env.seed(CHILD, ["長" * 500 for _ in range(5)])
            res = _read({"caller": CALLER, "target": CHILD, "mode": "recent"})
            self.assertTrue(res["truncated"])
            self.assertLessEqual(
                len(res["content"]), 200 + len(agent_context.TRUNC_MARK))
            self.assertIn("已截斷", res["content"])

    def test_recent_limit_capped(self):
        with _Env(extra_env={"AGENT_CONTEXT_RECENT_MAX": "5",
                             "AGENT_CONTEXT_MAX_CHARS": "100000"}) as env:
            env.seed(CHILD, [f"第 {i} 句" for i in range(30)])
            res = _read({"caller": CALLER, "target": CHILD, "mode": "recent",
                         "limit": 999})
            self.assertEqual(len(res["content"].splitlines()), 5)
            self.assertIn("第 29 句", res["content"])       # 取最新的
            self.assertNotIn("第 20 句", res["content"])

    def test_empty_stream_is_not_an_error(self):
        with _Env() as env:
            env.seed(CHILD, [])
            res = _read({"caller": CALLER, "target": CHILD, "mode": "recent"})
            self.assertIn("空", res["content"])
            self.assertFalse(res["truncated"])


# ───────────────────────── 快取 ─────────────────────────

class TestSummaryCache(unittest.TestCase):
    def test_hit_then_invalidate_on_new_activity(self):
        with _Env() as env:
            env.seed(CHILD, ["第一輪"])
            a = _read({"caller": CALLER, "target": CHILD, "mode": "summary"})
            self.assertFalse(a["cached"])
            b = _read({"caller": CALLER, "target": CHILD, "mode": "summary"})
            self.assertTrue(b["cached"])
            self.assertEqual(a["content"], b["content"])
            self.assertEqual(env.summarize.call_count, 1)
            self.assertEqual(a["source_seq"], b["source_seq"])
            # 新卡片 → 內容 seq 前進 → 失效
            env.seed(CHILD, ["第二輪"])
            c = _read({"caller": CALLER, "target": CHILD, "mode": "summary"})
            self.assertFalse(c["cached"])
            self.assertGreater(c["source_seq"], b["source_seq"])
            self.assertEqual(env.summarize.call_count, 2)

    def test_audit_card_does_not_invalidate_cache(self):
        """讀取自己會落 audit 卡;若拿 store.seq 當快取鍵,summary 就永遠 miss。"""
        with _Env() as env:
            store = env.seed(CHILD, ["內容"])
            _read({"caller": CALLER, "target": CHILD, "mode": "summary"})
            seq_after_audit = store.seq
            res = _read({"caller": CALLER, "target": CHILD, "mode": "summary"})
            self.assertTrue(res["cached"])
            self.assertLess(res["source_seq"], seq_after_audit)
            self.assertEqual(env.summarize.call_count, 1)

    def test_cache_is_per_session(self):
        rules = [{"caller": CALLER, "context_targets": [FRIEND]}]
        with _Env(rules=rules) as env:
            env.seed(CHILD, ["A"])
            env.seed(FRIEND, ["B"])
            _read({"caller": CALLER, "target": CHILD, "mode": "summary"})
            _read({"caller": CALLER, "target": FRIEND, "mode": "summary"})
            self.assertEqual(env.summarize.call_count, 2)
            self.assertEqual(set(bridge._AGENT_CONTEXT_SUMMARY_CACHE),
                             {CHILD, FRIEND})


# ───────────────────────── 留痕 ─────────────────────────

class TestAuditTrail(unittest.TestCase):
    def test_audit_card_lands_in_target_stream(self):
        with _Env() as env:
            env.seed(CHILD, ["內容"])
            res = _read({"caller": CALLER, "target": CHILD, "mode": "recent"})
            cards = [c for c in env.cards(CHILD)
                     if c["id"].startswith("card-agentctx-")]
            self.assertEqual(len(cards), 1)
            body = cards[0]["body"]
            self.assertIn("👁", body["text"])
            self.assertIn(CALLER, body["text"])
            self.assertIn("recent", body["text"])
            self.assertEqual(body["fallback_text"], body["text"])
            self.assertEqual(body["origin"], "agent_context")
            self.assertEqual(body["read_id"], res["read_id"])
            # caller 自己的流不落卡(讀取是自己的事,別洗自己的版)
            self.assertEqual(env.cards(CALLER), [])

    def test_audit_card_not_collected_as_agent_call_reply(self):
        """👁 卡是 assistant text 卡,不能被 agent_call 收割成「對方的回覆」。"""
        with _Env() as env:
            store = env.seed(CHILD, [])
            since = store.seq
            _read({"caller": CALLER, "target": CHILD, "mode": "recent"})
            self.assertEqual(
                bridge._agent_call_collect_reply(store, since), "")

    def test_audit_card_not_reread_as_content(self):
        with _Env() as env:
            env.seed(CHILD, ["真內容"])
            _read({"caller": CALLER, "target": CHILD, "mode": "recent"})
            res = _read({"caller": CALLER, "target": CHILD, "mode": "recent"})
            self.assertNotIn("👁", res["content"])
            self.assertIn("真內容", res["content"])


# ───────────────────────── 速率上限 ─────────────────────────

class TestRateLimit(unittest.TestCase):
    def test_per_caller_limit(self):
        with _Env(extra_env={"AGENT_CONTEXT_RATE": "3"}) as env:
            env.seed(CHILD, ["內容"])
            for _ in range(3):
                _read({"caller": CALLER, "target": CHILD, "mode": "summary"})
            with self.assertRaises(HTTPException) as cm:
                _read({"caller": CALLER, "target": CHILD, "mode": "summary"})
            self.assertEqual(cm.exception.status_code, 429)

    def test_denied_reads_burn_quota(self):
        """不然探測政策邊界是免費的。"""
        with _Env(extra_env={"AGENT_CONTEXT_RATE": "2"}) as env:
            env.seed(STRANGER, ["x"])
            for _ in range(2):
                with self.assertRaises(HTTPException) as cm:
                    _read({"caller": CALLER, "target": STRANGER})
                self.assertEqual(cm.exception.status_code, 403)
            with self.assertRaises(HTTPException) as cm:
                _read({"caller": CALLER, "target": CHILD})
            self.assertEqual(cm.exception.status_code, 429)


# ───────────────────────── 搜尋 ─────────────────────────

class TestSearch(unittest.TestCase):
    def test_scope_is_permission(self):
        rules = [{"caller": CALLER, "context_targets": [FRIEND],
                  "modes": ["search"]}]
        with _Env(rules=rules) as env:
            env.seed(CHILD, ["child 摸過 bridge.py 的 agent_context"])
            env.seed(FRIEND, ["friend 也提到 agent_context 這個字"])
            env.seed(STRANGER, ["stranger 的機密 agent_context 內容"])
            res = _read({"caller": CALLER, "mode": "search",
                         "query": "agent_context"})
            sessions = {h["session"] for h in res["hits"]}
            self.assertEqual(sessions, {CHILD, FRIEND})
            self.assertNotIn("stranger 的機密", res["content"])
            self.assertEqual(res["target"], "*")
            self.assertNotIn(STRANGER, res["scope"])

    def test_search_single_target_denied(self):
        with _Env() as env:
            env.seed(STRANGER, ["機密 needle"])
            with self.assertRaises(HTTPException) as cm:
                _read({"caller": CALLER, "target": STRANGER,
                       "mode": "search", "query": "needle"})
            self.assertEqual(cm.exception.status_code, 403)

    def test_search_snippets_redacted_and_capped(self):
        with _Env(extra_env={"AGENT_CONTEXT_SEARCH_MAX": "2"}) as env:
            env.seed(CHILD, [f"needle {SECRETS[0]}" for _ in range(10)])
            res = _read({"caller": CALLER, "mode": "search", "query": "needle"})
            self.assertEqual(len(res["hits"]), 2)
            self.assertNotIn("sk-ant", res["content"])

    def test_search_audit_only_on_hit_sessions(self):
        with _Env() as env:
            env.seed(CHILD, ["有 needle"])
            env.seed(FRIEND, ["沒有那個字"])
            _read({"caller": CALLER, "mode": "search", "query": "needle"})
            self.assertTrue([c for c in env.cards(CHILD)
                             if c["id"].startswith("card-agentctx-")])
            self.assertFalse([c for c in env.cards(FRIEND)
                              if c["id"].startswith("card-agentctx-")])

    def test_search_no_hits_is_not_an_error(self):
        with _Env() as env:
            env.seed(CHILD, ["完全無關"])
            res = _read({"caller": CALLER, "mode": "search",
                         "query": "找不到的東西"})
            self.assertEqual(res["hits"], [])
            self.assertIn("找不到", res["content"])

    def test_search_only_scans_warm_stores(self):
        """不為了搜尋把整台機器的 session 冷載入(那會拖垮一個回合)。"""
        with _Env() as env:
            env.seed(CHILD, ["needle 在這"])
            before = set(env.stores)
            _read({"caller": CALLER, "mode": "search", "query": "needle"})
            self.assertEqual(set(env.stores) - before, set())


if __name__ == "__main__":
    unittest.main()
