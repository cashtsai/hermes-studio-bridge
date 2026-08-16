"""Continual Harness(累積層,藍圖 §2)—— 軌跡/遮罩/四庫/蒸餾/人審鐵律。

涵蓋:
- 軌跡正規化(卡片流 ring / CC jsonl / canonical row)、封頂、outcome 判定
- **秘密遮罩**:api-key 形狀的字串一條都不許活著進庫(端到端驗到 sqlite)
- 四庫 CRUD + 版本遞增 + 狀態機(含非法轉換必須被擋)
- 蒸餾提案生成(**模型呼叫全程 mock,絕不打真的 LLM**)
- approve/reject 端點;prompt 提案核准 → 真的寫進 spawn pin(閉環)
- **旗標關閉 = 端點 404 + 零背景工作**
- **唯讀保證**:harness 的程式碼不得以寫入模式開 production DB

⚠️ 這裡所有 DB 都指向 tmp,**絕不碰真的 canonical.db / state.db /
~/.config/ccsess**(那些是活的 production 資料)。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

_TMP = tempfile.mkdtemp(prefix="harness-test-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("POCKET_REGISTRY_DB", os.path.join(_TMP, "registry.db"))
os.environ.setdefault("HARNESS_DB", os.path.join(_TMP, "harness.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
from harness import distill as D  # noqa: E402
from harness import store as S  # noqa: E402
from harness import trajectory as T  # noqa: E402


class FakeRequest:
    headers = {"authorization": "Bearer test-unit-token"}
    client = None

    def __init__(self, body=None):
        self._body = body or {}

    async def json(self):
        return self._body


def _store() -> S.HarnessStore:
    return S.HarnessStore(tempfile.mktemp(suffix=".db", dir=_TMP))


# 真實形狀的 api key(全是編造的假值,但長得跟真的一樣,才驗得出遮罩)。
#
# ⚠️ 為什麼要用 `_mk()` 拼起來而不是直接寫字面值:GitHub push protection 會
# 掃出「長得像真金鑰」的字串並**擋下整個 push**(2026-08-11 實際被擋過一次,
# 就是這幾行)。前綴與本體拆開存放,組出來的值一模一樣、遮罩照樣驗得到,
# 但檔案裡不存在連續的金鑰形狀字串。**不要**改回字面值,也不要去 GitHub 開
# 白名單放行 —— 那等於教大家「被擋就點放行」,遲早放到真的。
def _mk(*parts: str) -> str:
    return "".join(parts)


FAKE_SECRETS = [
    _mk("sk-", "ant-", "api03-Ab3xY9zQwErTyUiOpAsDfGhJkLzXcVbNm1234567890"),
    _mk("sk-", "proj-", "9f8e7d6c5b4a3210zyxwvutsrqponmlkjihgfedcba"),
    _mk("ghp", "_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"),
    _mk("github", "_pat_", "11ABCDEFG0abcdefghijklmnopqrstuvwxyz123456"),
    _mk("AKIA", "IOSFODNN7", "EXAMPLE"),
    _mk("xoxb", "-123456789012-", "1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"),
    _mk("1234567890", ":AAH", "dqTcvCH1vGWJxfSeofSAs0K5PALDsaw"),
]
# 「不該被誤殺」的正常字串(遮罩過激會把軌跡洗成一片 redacted)
INNOCENT = ["Read /Users/xcash/apps/pocketagent/README.md",
            "git commit -m '修好卡頓'", "pytest tests/test_agent_call.py -x",
            "success: 12 passed", "sk-"]


# ─────────────────────────── 遮罩 ───────────────────────────

class TestRedaction(unittest.TestCase):
    def test_每種_api_key_形狀都被遮掉(self):
        for secret in FAKE_SECRETS:
            out = T.redact_text(f"執行前先 export KEY={secret} 然後跑")
            self.assertNotIn(secret, out, f"未遮罩:{secret}")
            self.assertIn(T.REDACTED, out)

    def test_key_value_通則保留欄名只殺值(self):
        out = T.redact_text("ANTHROPIC_API_KEY=totally-secret-value-here")
        self.assertNotIn("totally-secret-value-here", out)
        self.assertIn("ANTHROPIC_API_KEY", out)   # 欄名有診斷價值,留著

    def test_bearer_與_jwt(self):
        out = T.redact_text("Authorization: Bearer abcdefghijklmnop1234567890")
        self.assertNotIn("abcdefghijklmnop1234567890", out)
        jwt = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
               "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
        self.assertNotIn(jwt, T.redact_text("token " + jwt))

    def test_pem_私鑰整塊(self):
        pem = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n"
               "-----END RSA PRIVATE KEY-----")
        self.assertNotIn("MIIEow", T.redact_text(pem))

    def test_正常字串不被誤殺(self):
        for s in INNOCENT:
            self.assertEqual(s, T.redact_text(s), f"誤殺:{s}")

    def test_config_遮罩只留_has_api_key(self):
        out = T.redact_config({"model": "opus", "api_key": FAKE_SECRETS[0],
                               "effort": "high"})
        self.assertEqual(out["model"], "opus")
        self.assertTrue(out["has_api_key"])
        self.assertNotIn("api_key", out)
        self.assertNotIn(FAKE_SECRETS[0], json.dumps(out))

    def test_非字串輸入不炸(self):
        self.assertEqual(T.redact_text(None), "")
        self.assertEqual(T.redact_text(42), "42")


# ─────────────────────────── 軌跡正規化 ───────────────────────────

def _card(cid, turn, kind, body, ts=1000.0):
    return {"seq": 0, "ts": ts, "type": "card.upsert",
            "data": {"card": {"id": cid, "turn_id": turn, "kind": kind,
                              "role": "assistant", "ts": ts, "body": body}}}


CARD_EVENTS = [
    {"seq": 1, "ts": 100.0, "type": "turn", "data": {"state": "start", "turn_id": "t1"}},
    _card("c1", "t1", "text", {"text": "幫我修 bug"}, 100.0),
    _card("c2", "t1", "tool_call", {"tool": "Grep", "summary": "def foo"}, 101.0),
    _card("c3", "t1", "tool_result", {"text": "bridge.py:42"}, 102.0),
    _card("c4", "t1", "tool_call", {"tool": "Edit", "summary": "/x/bridge.py"}, 103.0),
    _card("c5", "t1", "tool_result", {"text": "Error: string not found"}, 104.0),
    _card("c6", "t1", "markdown", {"text": "改好了"}, 105.0),
    {"seq": 8, "ts": 106.0, "type": "turn", "data": {"state": "end", "turn_id": "t1"}},
    {"seq": 9, "ts": 200.0, "type": "turn", "data": {"state": "start", "turn_id": "t2"}},
    _card("d1", "t2", "tool_call", {"tool": "Bash", "summary": "pytest"}, 201.0),
    _card("d2", "t2", "tool_result", {"text": "12 passed"}, 202.0),
    {"seq": 12, "ts": 203.0, "type": "turn", "data": {"state": "end", "turn_id": "t2"}},
]


class TestTrajectoryFromCards(unittest.TestCase):
    def test_依_turn_切成兩條軌跡(self):
        out = T.from_card_events(CARD_EVENTS, session_id="claude_code:tirith",
                                 provider="cc", purpose="修 bug")
        self.assertEqual(len(out), 2)
        self.assertEqual([t["turn_id"] for t in out], ["t1", "t2"])
        self.assertEqual(out[0]["session_id"], "claude_code:tirith")
        self.assertEqual(out[0]["purpose"], "修 bug")

    def test_step_形狀與工具名(self):
        t1 = T.from_card_events(CARD_EVENTS, session_id="s")[0]
        kinds = [s["kind"] for s in t1["steps"]]
        self.assertEqual(kinds, ["say", "tool", "result", "tool", "result", "say"])
        self.assertEqual([s["tool"] for s in t1["steps"] if s["kind"] == "tool"],
                         ["Grep", "Edit"])

    def test_result_失敗被判成_error_且整條軌跡不_ok(self):
        t1, t2 = T.from_card_events(CARD_EVENTS, session_id="s")
        self.assertFalse(t1["result"]["ok"])
        self.assertIn("Error", t1["result"]["error"])
        self.assertTrue(t2["result"]["ok"])

    def test_duration_與_turns(self):
        t1 = T.from_card_events(CARD_EVENTS, session_id="s")[0]
        self.assertAlmostEqual(t1["result"]["duration_s"], 6.0, places=2)
        self.assertEqual(t1["result"]["turns"], 2)      # 兩個 tool_call

    def test_thinking_卡不進軌跡(self):
        evs = list(CARD_EVENTS) + [_card("th", "t1", "text", {"text": "💭 想一下"})]
        t1 = T.from_card_events(evs, session_id="s")[0]
        self.assertFalse(any("想一下" in s["summary"] for s in t1["steps"]))

    def test_同卡多次_upsert_只留最後一版(self):
        evs = list(CARD_EVENTS) + [
            _card("c6", "t1", "markdown", {"text": "改好了(修訂)"}, 105.5)]
        t1 = T.from_card_events(evs, session_id="s")[0]
        says = [s for s in t1["steps"] if s["kind"] == "say"]
        self.assertEqual(len(says), 2)
        self.assertIn("修訂", says[-1]["summary"])

    def test_trajectory_id_穩定且唯一(self):
        a = T.from_card_events(CARD_EVENTS, session_id="s")
        b = T.from_card_events(CARD_EVENTS, session_id="s")
        self.assertEqual([x["id"] for x in a], [y["id"] for y in b])
        self.assertNotEqual(a[0]["id"], a[1]["id"])

    def test_step_封頂並插摺疊註記(self):
        evs = [_card(f"c{i}", "big", "tool_call", {"tool": "Bash", "summary": str(i)},
                     100.0 + i) for i in range(400)]
        t = T.from_card_events(evs, session_id="s", max_steps=20)[0]
        self.assertEqual(len(t["steps"]), 20)
        self.assertTrue(any("摺疊" in s["summary"] for s in t["steps"]))

    def test_秘密不會從卡片流漏進軌跡(self):
        evs = [_card("c1", "t", "tool_call",
                     {"tool": "Bash", "summary": f"export K={FAKE_SECRETS[0]}"}),
               _card("c2", "t", "tool_result", {"text": f"ok {FAKE_SECRETS[2]}"})]
        t = T.from_card_events(evs, session_id="s",
                               node_config={"api_key": FAKE_SECRETS[1]})[0]
        blob = json.dumps(t, ensure_ascii=False)
        for secret in FAKE_SECRETS[:3]:
            self.assertNotIn(secret, blob)


CC_JSONL = [
    {"type": "user", "uuid": "u1", "timestamp": "2026-08-11T00:00:00Z",
     "message": {"content": "請跑測試"}},
    {"type": "assistant", "uuid": "a1", "timestamp": "2026-08-11T00:00:05Z",
     "message": {"content": [
         {"type": "text", "text": "好,我跑"},
         {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -x"}}]}},
    {"type": "user", "uuid": "r1", "timestamp": "2026-08-11T00:00:20Z",
     "message": {"content": [{"type": "tool_result",
                              "content": [{"type": "text", "text": "3 failed"}],
                              "is_error": True}]}},
    {"type": "user", "uuid": "sys", "timestamp": "2026-08-11T00:00:25Z",
     "message": {"content": "<system-reminder>別理我</system-reminder>"}},
    {"type": "user", "uuid": "u2", "timestamp": "2026-08-11T00:01:00Z",
     "message": {"content": "那修一下"}},
    {"type": "assistant", "uuid": "a2", "timestamp": "2026-08-11T00:01:10Z",
     "message": {"content": [
         {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/a.py"}}]}},
]


class TestTrajectoryFromCcJsonl(unittest.TestCase):
    def test_依使用者輸入切_turn_且跳過管路訊息(self):
        out = T.from_cc_jsonl(CC_JSONL, session_id="claude_code:x")
        self.assertEqual(len(out), 2)
        self.assertEqual([t["turn_id"] for t in out], ["u1", "u2"])
        self.assertNotIn("system-reminder", json.dumps(out))

    def test_is_error_直接判成失敗(self):
        first = T.from_cc_jsonl(CC_JSONL, session_id="s")[0]
        self.assertFalse(first["result"]["ok"])
        self.assertEqual(first["provider"], "claude_code")

    def test_tool_use_取出工具名與參數摘要(self):
        first = T.from_cc_jsonl(CC_JSONL, session_id="s")[0]
        tools = [(s["tool"], s["summary"]) for s in first["steps"]
                 if s["kind"] == "tool"]
        self.assertEqual(tools, [("Bash", "pytest -x")])

    def test_壞行不炸(self):
        out = T.from_cc_jsonl([None, "字串", {"type": "nope"}] + CC_JSONL,
                              session_id="s")
        self.assertEqual(len(out), 2)

    def test_秘密不漏(self):
        rows = [{"type": "user", "uuid": "u", "timestamp": "2026-08-11T00:00:00Z",
                 "message": {"content": "用這把 " + FAKE_SECRETS[0]}},
                {"type": "assistant", "uuid": "a",
                 "timestamp": "2026-08-11T00:00:01Z",
                 "message": {"content": [{"type": "tool_use", "name": "Bash",
                                          "input": {"command": "curl -H 'Authorization: Bearer "
                                                    + FAKE_SECRETS[1] + "'"}}]}}]
        blob = json.dumps(T.from_cc_jsonl(rows, session_id="s"), ensure_ascii=False)
        self.assertNotIn(FAKE_SECRETS[0], blob)
        self.assertNotIn(FAKE_SECRETS[1], blob)


class TestTrajectoryFromCanonical(unittest.TestCase):
    def test_人格對話切_turn(self):
        rows = [{"role": "user", "content": "早", "created_at": 10.0, "turn_id": "x1"},
                {"role": "assistant", "content": "早安", "created_at": 11.0,
                 "turn_id": "x1"},
                {"role": "user", "content": "報告", "created_at": 20.0,
                 "turn_id": "x2"},
                {"role": "assistant", "content": "來了", "created_at": 21.0,
                 "turn_id": "x2"}]
        out = T.from_canonical_rows(rows, session_id="hermes:yuanfang")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["provider"], "hermes")
        self.assertEqual(len(out[0]["steps"]), 2)

    def test_吃得下_sqlite3_row(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE m(role TEXT, content TEXT, created_at REAL,"
                    " turn_id TEXT)")
        con.executemany("INSERT INTO m VALUES(?,?,?,?)",
                        [("user", "問", 1.0, "t"), ("assistant", "答", 2.0, "t")])
        rows = con.execute("SELECT * FROM m ORDER BY created_at").fetchall()
        out = T.from_canonical_rows(rows, session_id="hermes:yuanfang")
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]["steps"]), 2)


# ─────────────────────────── 四庫 CRUD / 狀態機 ───────────────────────────

class TestStoreCrud(unittest.TestCase):
    def test_四庫都建得起來且欄位齊(self):
        st = _store()
        con = sqlite3.connect(st.db_path)
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(set(S.STORES) <= names)
        self.assertIn("trajectories", names)
        self.assertIn("distill_runs", names)

    def test_propose_版本自動遞增(self):
        st = _store()
        a = st.propose("memory", key="k", payload={"fact": "一"})
        b = st.propose("memory", key="k", payload={"fact": "二"})
        c = st.propose("memory", key="other", payload={"fact": "三"})
        self.assertEqual((a["version"], b["version"], c["version"]), (1, 2, 1))
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(a["state"], "proposed")

    def test_scope_隔離版本(self):
        st = _store()
        a = st.propose("prompt", key="n1", scope="node:a", payload={"node": "a"})
        b = st.propose("prompt", key="n1", scope="node:b", payload={"node": "b"})
        self.assertEqual((a["version"], b["version"]), (1, 1))

    def test_每庫的_payload_欄位各自落地(self):
        st = _store()
        sk = st.propose("skill", key="s", payload={
            "name": "跑測試", "when_to_use": "改完 code",
            "steps": ["跑 pytest", "看紅的"]})
        self.assertEqual(sk["name"], "跑測試")
        self.assertEqual(sk["steps"], ["跑 pytest", "看紅的"])   # JSON 欄自動轉回
        rt = st.propose("subagent_route", key="coding", payload={
            "task_kind": "coding", "target": "cc:x", "success_n": 8, "sample_n": 10})
        self.assertEqual((rt["success_n"], rt["sample_n"]), (8, 10))

    def test_未知_store_與空_key_被擋(self):
        st = _store()
        with self.assertRaises(ValueError):
            st.propose("nope", key="k", payload={})
        with self.assertRaises(ValueError):
            st.propose("memory", key="  ", payload={"fact": "x"})

    def test_list_可依庫_狀態_scope_過濾(self):
        st = _store()
        st.propose("memory", key="a", payload={"fact": "1"})
        st.propose("skill", key="b", payload={"name": "n", "steps": ["s"]})
        self.assertEqual(len(st.list()), 2)
        self.assertEqual(len(st.list(store="memory")), 1)
        self.assertEqual(len(st.list(state="active")), 0)


class TestStateMachine(unittest.TestCase):
    def test_approve_走完_approved_到_active(self):
        st = _store()
        p = st.propose("memory", key="k", payload={"fact": "x"})
        out = st.approve(p["id"], by="善彰")
        self.assertEqual(out["state"], "active")
        self.assertEqual(out["decided_by"], "善彰")
        self.assertTrue(out["decided_ts"])

    def test_reject_後不能再_approve(self):
        st = _store()
        p = st.propose("memory", key="k", payload={"fact": "x"})
        st.reject(p["id"], reason="沒用")
        self.assertEqual(st.get(p["id"])["state"], "rejected")
        with self.assertRaises(S.StateError):
            st.approve(p["id"])

    def test_已_active_不能重複_approve(self):
        st = _store()
        p = st.propose("memory", key="k", payload={"fact": "x"})
        st.approve(p["id"])
        with self.assertRaises(S.StateError):
            st.approve(p["id"])

    def test_同_key_新版上線_舊版退位(self):
        st = _store()
        v1 = st.propose("prompt", key="n", scope="node:n", payload={"node": "n"})
        st.approve(v1["id"])
        v2 = st.propose("prompt", key="n", scope="node:n", payload={"node": "n"})
        st.approve(v2["id"])
        self.assertEqual(st.get(v1["id"])["state"], "superseded")
        self.assertEqual(st.get(v2["id"])["state"], "active")
        self.assertEqual(len(st.active("prompt")), 1)

    def test_沒有_proposed_直達_active_這條邊(self):
        self.assertNotIn(("proposed", "active"), S._TRANSITIONS)

    def test_找不到的提案回報清楚(self):
        st = _store()
        with self.assertRaises(S.StateError):
            st.approve("mem-doesnotexist")

    def test_軌跡落庫冪等且可依時間撈(self):
        st = _store()
        trajs = T.from_card_events(CARD_EVENTS, session_id="s")
        for t in trajs:
            st.put_trajectory(t)
            st.put_trajectory(t)          # 重放
        self.assertEqual(len(st.trajectories_since(0)), 2)
        self.assertEqual(len(st.trajectories_since(150)), 1)

    def test_跑批帳(self):
        st = _store()
        rid = st.run_start(hours=24, model="m")
        st.run_finish(rid, trajectories=5, proposals=2)
        last = st.last_run()
        self.assertEqual((last["trajectories"], last["proposals"]), (5, 2))


class TestSecretsNeverReachDb(unittest.TestCase):
    """端到端:從卡片流灌到 sqlite,整個 DB 檔的 bytes 裡不能有 api key。"""

    def test_整個_db_檔掃不到任何秘密(self):
        st = _store()
        evs = []
        for i, secret in enumerate(FAKE_SECRETS):
            evs.append(_card(f"c{i}", "t", "tool_call",
                             {"tool": "Bash", "summary": f"export K={secret}"}))
            evs.append(_card(f"r{i}", "t", "tool_result", {"text": f"用了 {secret}"}))
        for t in T.from_card_events(evs, session_id="s",
                                    node_config={"api_key": FAKE_SECRETS[0]}):
            st.put_trajectory(t)
            st.propose("memory", key="k", payload={"fact": f"金鑰是 {FAKE_SECRETS[1]}"},
                       rationale=f"看到 {FAKE_SECRETS[2]}",
                       preview=T.redact_text(f"+ {FAKE_SECRETS[3]}"))
        with open(st.db_path, "rb") as f:
            blob = f.read()
        for secret in FAKE_SECRETS:
            self.assertNotIn(secret.encode(), blob, f"DB 裡撈得到:{secret}")

    def test_store_是最後一道防線_呼叫端沒洗也不會漏(self):
        """縱深防禦:就算呼叫端忘了遮,進 sqlite 前 store 一定再洗一次。"""
        st = _store()
        row = st.propose("memory", key="k",
                         payload={"fact": f"金鑰 {FAKE_SECRETS[0]}",
                                  "tags": [f"t{FAKE_SECRETS[1]}"]},
                         rationale=f"看到 {FAKE_SECRETS[2]}",
                         preview=f"+ {FAKE_SECRETS[3]}",
                         meta={"raw": FAKE_SECRETS[4]})
        blob = json.dumps(row, ensure_ascii=False)
        for secret in FAKE_SECRETS[:5]:
            self.assertNotIn(secret, blob)
        self.assertIn(T.REDACTED, row["fact"])
        self.assertIn(T.REDACTED, row["meta"]["raw"])

    def test_軌跡進庫也走同一道防線(self):
        st = _store()
        traj = {"id": "traj-x", "session_id": "s", "turn_id": "t", "ts": 1.0,
                "provider": "cc", "purpose": f"用 {FAKE_SECRETS[0]}",
                "node_config": {}, "steps": [], "result": {"ok": True}}
        st.put_trajectory(traj)
        got = st.trajectories_since(0)[0]
        self.assertNotIn(FAKE_SECRETS[0], json.dumps(got, ensure_ascii=False))

    def test_propose_的_rationale_也要遮(self):
        """蒸餾器 `_shape` 這條路自己也要洗(不倚賴 store 那道)。"""
        rec = D._shape("memory",
                       {"fact": f"key {FAKE_SECRETS[0]}",
                        "rationale": f"因為 {FAKE_SECRETS[1]}", "evidence": []},
                       "node:x", "x", "coding", set())
        self.assertNotIn(FAKE_SECRETS[0], json.dumps(rec, ensure_ascii=False))
        self.assertNotIn(FAKE_SECRETS[1], json.dumps(rec, ensure_ascii=False))


# ─────────────────────────── 蒸餾(模型全 mock)───────────────────────────

MODEL_REPLY = json.dumps({
    "memory": [{"key": "pytest-路徑", "fact": "這個 repo 的測試要用 PYTHONPATH=. 跑",
                "tags": ["測試"], "rationale": "連兩次忘了設而失敗",
                "evidence": ["__EV0__"]}],
    "skill": [{"key": "跑測試", "name": "跑 bridge 測試",
               "when_to_use": "改完 bridge.py",
               "steps": ["PYTHONPATH=. venv/bin/python tests/x.py", "看紅的先修"],
               "rationale": "重複三次同一組動作", "evidence": ["__EV0__", "捏造的id"]}],
    "prompt": [{"fragment": "跑測試前先確認 PYTHONPATH=.",
                "rationale": "同一個坑踩兩次", "evidence": ["__EV1__"]}],
}, ensure_ascii=False)


def _seed(st, n_ok=6, n_fail=2, sid="claude_code:tirith"):
    """灌一批「同節點、同任務類型」的軌跡,讓分組蒸得出東西。"""
    ids = []
    now = time.time()
    for i in range(n_ok + n_fail):
        ok = i < n_ok
        evs = [_card(f"c{i}a", f"t{i}", "tool_call",
                     {"tool": "Edit", "summary": f"/x/f{i}.py"}, now - 100 + i),
               _card(f"c{i}b", f"t{i}", "tool_result",
                     {"text": "ok" if ok else "Error: 爆了"}, now - 99 + i)]
        for t in T.from_card_events(evs, session_id=sid, provider="cc",
                                    purpose="改 code"):
            st.put_trajectory(t)
            ids.append(t["id"])
    return ids


class TestDistill(unittest.IsolatedAsyncioTestCase):
    async def test_提案生成含理由_證據_預覽(self):
        st = _store()
        ids = _seed(st)
        reply = MODEL_REPLY.replace("__EV0__", ids[0]).replace("__EV1__", ids[1])
        mc = AsyncMock(return_value=reply)
        out = await D.run(st, hours=24, model_call=mc)
        self.assertTrue(mc.await_count >= 1)
        stores = {p["store"] for p in out["proposals"]}
        self.assertTrue({"memory", "skill", "prompt"} <= stores)
        for p in out["proposals"]:
            self.assertTrue(p["rationale"], f"{p['store']} 缺理由")
            self.assertTrue(p["preview"], f"{p['store']} 缺預覽")
        rows = st.list(state="proposed")
        self.assertEqual(len(rows), len(out["proposals"]))
        self.assertTrue(all(r["state"] == "proposed" for r in rows))

    async def test_捏造的_evidence_被剔除(self):
        st = _store()
        ids = _seed(st)
        reply = MODEL_REPLY.replace("__EV0__", ids[0]).replace("__EV1__", ids[1])
        out = await D.run(st, hours=24, model_call=AsyncMock(return_value=reply))
        skill = next(p for p in out["proposals"] if p["store"] == "skill")
        self.assertNotIn("捏造的id", skill["evidence"])
        self.assertIn(ids[0], skill["evidence"])

    async def test_路由提案不經模型_純統計(self):
        st = _store()
        _seed(st, n_ok=8, n_fail=1)
        # 模型整個掛掉,路由照樣要有
        mc = AsyncMock(side_effect=RuntimeError("ollama 掛了"))
        out = await D.run(st, hours=24, model_call=mc)
        routes = [p for p in out["proposals"] if p["store"] == "subagent_route"]
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["payload"]["task_kind"], "coding")
        self.assertEqual(routes[0]["meta"]["source"], "statistics")
        self.assertTrue(out["errors"])            # 模型錯誤有被記錄
        self.assertEqual(st.last_run()["proposals"], 1)

    async def test_樣本太少或成功率太低不出路由(self):
        st = _store()
        _seed(st, n_ok=2, n_fail=0)               # 只有 2 條 < ROUTE_MIN_SAMPLES
        out = await D.run(st, hours=24, model_call=AsyncMock(return_value="{}"))
        self.assertEqual([p for p in out["proposals"]
                          if p["store"] == "subagent_route"], [])
        st2 = _store()
        _seed(st2, n_ok=2, n_fail=6)              # 成功率 25% < 門檻
        out2 = await D.run(st2, hours=24, model_call=AsyncMock(return_value="{}"))
        self.assertEqual([p for p in out2["proposals"]
                          if p["store"] == "subagent_route"], [])

    async def test_dry_run_不寫庫(self):
        st = _store()
        ids = _seed(st)
        out = await D.run(st, hours=24, dry_run=True,
                          model_call=AsyncMock(
                              return_value=MODEL_REPLY.replace("__EV0__", ids[0])
                              .replace("__EV1__", ids[1])))
        self.assertTrue(out["proposals"])
        self.assertEqual(st.list(), [])
        self.assertIsNone(st.last_run())

    async def test_壞_json_不炸整輪(self):
        st = _store()
        _seed(st, n_ok=8, n_fail=1)
        out = await D.run(st, hours=24,
                          model_call=AsyncMock(return_value="我覺得應該多加註解"))
        self.assertEqual([p for p in out["proposals"] if p["store"] == "memory"], [])
        self.assertTrue(any(p["store"] == "subagent_route" for p in out["proposals"]))

    async def test_模型回_code_fence_也吃得下(self):
        st = _store()
        ids = _seed(st)
        fenced = "```json\n" + MODEL_REPLY.replace("__EV0__", ids[0]).replace(
            "__EV1__", ids[1]) + "\n```"
        out = await D.run(st, hours=24, model_call=AsyncMock(return_value=fenced))
        self.assertTrue(any(p["store"] == "memory" for p in out["proposals"]))

    async def test_視窗外的軌跡不列入(self):
        st = _store()
        old = T.from_card_events(CARD_EVENTS, session_id="s")[0]
        old["ts"] = time.time() - 86400 * 30
        st.put_trajectory(old)
        out = await D.run(st, hours=1, model_call=AsyncMock(return_value="{}"))
        self.assertEqual(out["trajectories"], 0)

    async def test_餵給模型的提示詞不含秘密(self):
        st = _store()
        now = time.time()
        for i in range(4):
            evs = [_card(f"x{i}", f"t{i}", "tool_call",
                         {"tool": "Edit", "summary": f"K={FAKE_SECRETS[0]}"},
                         now - 10 + i)]
            for t in T.from_card_events(evs, session_id="s", provider="cc"):
                st.put_trajectory(t)
        mc = AsyncMock(return_value="{}")
        await D.run(st, hours=24, model_call=mc)
        for call in mc.await_args_list:
            self.assertNotIn(FAKE_SECRETS[0], call.args[0])

    def test_任務分類(self):
        mk = lambda tools: {"steps": [{"kind": "tool", "tool": t} for t in tools]}
        self.assertEqual(D.task_kind(mk(["Edit"])), "coding")
        self.assertEqual(D.task_kind(mk(["Bash"])), "shell")
        self.assertEqual(D.task_kind(mk(["Grep", "Read"])), "research")
        self.assertEqual(D.task_kind(mk([])), "chat")

    def test_preview_是_diff_樣的(self):
        p = D.make_preview("prompt", {"node": "n", "fragment": "新片段"}, "舊片段")
        self.assertIn("- 舊片段", p)
        self.assertIn("+ 新片段", p)
        p2 = D.make_preview("prompt", {"node": "n", "fragment": "新"}, "")
        self.assertIn("(目前無)", p2)


# ─────────────────────────── 端點 / 人審閉環 ───────────────────────────

class HarnessApiBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.st = _store()
        self._patch = patch.object(bridge, "_HARNESS_STORE", self.st)
        self._patch.start()
        self._env = patch.dict(os.environ, {"HARNESS": "1"})
        self._env.start()

    def tearDown(self):
        self._patch.stop()
        self._env.stop()


class TestHarnessEndpoints(HarnessApiBase):
    async def test_列出待審提案帶三件事(self):
        p = self.st.propose("memory", key="k", payload={"fact": "事實"},
                            rationale="因為", evidence=["traj-1"], preview="+ 事實")
        out = await bridge.v2_harness_proposals(FakeRequest())
        self.assertEqual(len(out["proposals"]), 1)
        item = out["proposals"][0]
        self.assertEqual(item["id"], p["id"])
        self.assertEqual(item["rationale"], "因為")
        self.assertEqual(item["evidence"], ["traj-1"])
        self.assertEqual(item["preview"], "+ 事實")
        self.assertEqual(item["payload"]["fact"], "事實")

    async def test_state_過濾與非法值(self):
        self.st.propose("memory", key="k", payload={"fact": "x"})
        self.assertEqual(len((await bridge.v2_harness_proposals(
            FakeRequest(), state="active"))["proposals"]), 0)
        with self.assertRaises(Exception):
            await bridge.v2_harness_proposals(FakeRequest(), state="亂寫")

    async def test_approve_翻成_active(self):
        p = self.st.propose("memory", key="k", payload={"fact": "x"})
        out = await bridge.v2_harness_approve(p["id"], FakeRequest({"by": "善彰"}))
        self.assertTrue(out["ok"])
        self.assertEqual(out["proposal"]["state"], "active")
        self.assertEqual(out["proposal"]["decided_by"], "善彰")

    async def test_reject_留下理由(self):
        p = self.st.propose("skill", key="k", payload={"name": "n", "steps": ["s"]})
        out = await bridge.v2_harness_reject(p["id"], FakeRequest({"reason": "太籠統"}))
        self.assertEqual(out["proposal"]["state"], "rejected")
        self.assertEqual(out["proposal"]["apply_note"], "太籠統")

    async def test_重複_approve_回_409(self):
        p = self.st.propose("memory", key="k", payload={"fact": "x"})
        await bridge.v2_harness_approve(p["id"], FakeRequest())
        with self.assertRaises(Exception) as ctx:
            await bridge.v2_harness_approve(p["id"], FakeRequest())
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)

    async def test_不存在的提案回_404(self):
        with self.assertRaises(Exception) as ctx:
            await bridge.v2_harness_approve("mem-nope", FakeRequest())
        self.assertEqual(getattr(ctx.exception, "status_code", None), 404)

    async def test_status(self):
        self.st.propose("memory", key="k", payload={"fact": "x"})
        out = await bridge.v2_harness_status(FakeRequest())
        self.assertTrue(out["enabled"])
        self.assertEqual(out["pending"]["memory"], 1)
        self.assertEqual(out["active"]["memory"], 0)


class TestPromptApprovalWritesSpawnPin(HarnessApiBase):
    """核准 prompt 提案 → 片段真的寫進 ccsess spawn pin(閉環)。

    ⚠️ spawn dir 一律指向 tmp,絕不碰真的 ~/.config/ccsess。
    """

    def setUp(self):
        super().setUp()
        self.spawn_dir = tempfile.mkdtemp(dir=_TMP)
        self.secret_dir = tempfile.mkdtemp(dir=_TMP)
        self._p1 = patch.object(bridge, "CCSESS_SPAWN_DIR", self.spawn_dir)
        self._p2 = patch.object(bridge, "CCSESS_SECRET_DIR", self.secret_dir)
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        super().tearDown()

    def _pin(self, node="tirith"):
        path = os.path.join(self.spawn_dir, node + ".json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    async def test_核准後片段落進_spawn_pin(self):
        p = self.st.propose("prompt", key="tirith", scope="node:tirith",
                            payload={"node": "tirith", "provider": "cc",
                                     "fragment": "跑測試前先設 PYTHONPATH=."})
        out = await bridge.v2_harness_approve(p["id"], FakeRequest())
        self.assertEqual(self._pin()["append_system_prompt"],
                         "跑測試前先設 PYTHONPATH=.")
        self.assertIn("spawn pin", out["applied_note"])
        self.assertTrue(out["proposal"]["applied"])

    async def test_核准前不寫任何東西(self):
        self.st.propose("prompt", key="tirith", scope="node:tirith",
                        payload={"node": "tirith", "provider": "cc",
                                 "fragment": "不該生效"})
        self.assertIsNone(self._pin())

    async def test_保留同檔其他設定不覆蓋(self):
        with open(os.path.join(self.spawn_dir, "tirith.json"), "w") as f:
            json.dump({"model": "opus", "effort": "high"}, f)
        p = self.st.propose("prompt", key="tirith", scope="node:tirith",
                            payload={"node": "tirith", "provider": "cc",
                                     "fragment": "新片段"})
        await bridge.v2_harness_approve(p["id"], FakeRequest())
        pin = self._pin()
        self.assertEqual(pin["model"], "opus")
        self.assertEqual(pin["effort"], "high")
        self.assertEqual(pin["append_system_prompt"], "新片段")

    async def test_絕不刪掉_BYO_金鑰的_secret_檔(self):
        """回歸鐵律:`_cc_write_spawn_pins` 在 cfg 沒 api_key 時會刪 secret 檔;
        harness 的局部更新絕不能連帶砍掉使用者自帶的金鑰。"""
        spath = os.path.join(self.secret_dir, "tirith")
        with open(spath, "w") as f:
            f.write(FAKE_SECRETS[0])
        p = self.st.propose("prompt", key="tirith", scope="node:tirith",
                            payload={"node": "tirith", "provider": "cc",
                                     "fragment": "片段"})
        await bridge.v2_harness_approve(p["id"], FakeRequest())
        self.assertTrue(os.path.exists(spath), "BYO key 的 secret 檔被刪了!")
        with open(spath) as f:
            self.assertEqual(f.read(), FAKE_SECRETS[0])

    async def test_金鑰不會被寫進_pin(self):
        with open(os.path.join(self.spawn_dir, "tirith.json"), "w") as f:
            json.dump({"api_key": FAKE_SECRETS[0], "model": "opus"}, f)
        p = self.st.propose("prompt", key="tirith", scope="node:tirith",
                            payload={"node": "tirith", "provider": "cc",
                                     "fragment": "片段"})
        await bridge.v2_harness_approve(p["id"], FakeRequest())
        self.assertNotIn("api_key", self._pin())

    async def test_非_cc_provider_誠實說做不到(self):
        p = self.st.propose("prompt", key="th1", scope="node:th1",
                            payload={"node": "th1", "provider": "codex",
                                     "fragment": "片段"})
        out = await bridge.v2_harness_approve(p["id"], FakeRequest())
        self.assertEqual(out["proposal"]["state"], "active")
        self.assertIn("手動帶入", out["applied_note"])
        self.assertIsNone(self._pin("th1"))

    async def test_memory_核准不寫任何檔(self):
        p = self.st.propose("memory", key="k", payload={"fact": "x"})
        out = await bridge.v2_harness_approve(p["id"], FakeRequest())
        self.assertEqual(out["applied_note"], "")
        self.assertEqual(os.listdir(self.spawn_dir), [])


# ─────────────────────────── 旗標關閉 = 零風險 ───────────────────────────

class TestFlagOff(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {"HARNESS": "0"})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    async def test_四個端點全部_404(self):
        for coro in (bridge.v2_harness_proposals(FakeRequest()),
                     bridge.v2_harness_approve("x", FakeRequest()),
                     bridge.v2_harness_reject("x", FakeRequest()),
                     bridge.v2_harness_distill(FakeRequest()),
                     bridge.v2_harness_status(FakeRequest())):
            with self.assertRaises(Exception) as ctx:
                await coro
            self.assertEqual(getattr(ctx.exception, "status_code", None), 404)

    async def test_startup_不啟動任何背景工作_也不建_DB(self):
        before = set(bridge._BG_TASKS)
        with patch.object(bridge, "_harness_store") as mk:
            await bridge._start_harness()
            mk.assert_not_called()
        self.assertEqual(set(bridge._BG_TASKS) - before, set())

    async def test_收集器不落任何軌跡(self):
        self.assertEqual(await bridge._harness_ingest_session("claude_code:x"), 0)

    def test_晨報段回空(self):
        self.assertEqual(bridge._persona_harness_reports(bridge._HARNESS_REPORT_PERSONA), [])

    async def test_旗標打開才啟動背景工作(self):
        before = set(bridge._BG_TASKS)
        with patch.dict(os.environ, {"HARNESS": "1"}), \
                patch.object(bridge, "_harness_store", return_value=_store()):
            await bridge._start_harness()
        new = set(bridge._BG_TASKS) - before
        self.assertEqual(len(new), 2)             # ingest + distill 兩顆
        for t in new:
            t.cancel()


# ─────────────────────────── 晨報段 ───────────────────────────

class TestMorningReportSection(unittest.TestCase):
    def setUp(self):
        self.st = _store()
        self._p = patch.object(bridge, "_HARNESS_STORE", self.st)
        self._p.start()
        self._env = patch.dict(os.environ, {"HARNESS": "1"})
        self._env.start()

    def tearDown(self):
        self._p.stop()
        self._env.stop()

    def test_有待審提案就出一則報告(self):
        self.st.propose("memory", key="pytest-路徑", payload={"fact": "要設 PYTHONPATH"},
                        rationale="踩兩次", evidence=["traj-a"], preview="+ 要設")
        out = bridge._persona_harness_reports(bridge._HARNESS_REPORT_PERSONA)
        self.assertEqual(len(out), 1)
        r = out[0]
        self.assertEqual(r["label"], "蒸餾提案")
        self.assertEqual(r["external_source"], "harness")
        self.assertIn("待審 **1** 筆", r["content"])
        self.assertIn("pytest-路徑", r["content"])
        self.assertIn("踩兩次", r["content"])
        self.assertIn("+ 要設", r["content"])

    def test_同一天_external_id_穩定(self):
        self.st.propose("memory", key="k", payload={"fact": "x"})
        a = bridge._persona_harness_reports(bridge._HARNESS_REPORT_PERSONA)[0]
        b = bridge._persona_harness_reports(bridge._HARNESS_REPORT_PERSONA)[0]
        self.assertEqual(a["external_id"], b["external_id"])

    def test_沒提案也沒跑批就不打擾(self):
        self.assertEqual(bridge._persona_harness_reports(bridge._HARNESS_REPORT_PERSONA), [])

    def test_跑過批但沒提案_報告說沒有待審(self):
        rid = self.st.run_start(hours=24, model="m")
        self.st.run_finish(rid, trajectories=3, proposals=0)
        out = bridge._persona_harness_reports(bridge._HARNESS_REPORT_PERSONA)
        self.assertIn("沒有待審提案", out[0]["content"])
        self.assertIn("看了 3 條軌跡", out[0]["content"])

    def test_harness_不在隱藏清單_善彰看得到(self):
        self.assertNotIn("harness", bridge.HIDDEN_REPORT_SOURCES)
        self.assertFalse(bridge._is_hidden_report(
            bridge._persona_harness_reports.__wrapped__("yuanfang")[0]
            if hasattr(bridge._persona_harness_reports, "__wrapped__")
            else {"external_source": "harness", "name": "harness-proposals",
                  "label": "蒸餾提案"}))


# ─────────────────────────── 唯讀保證 ───────────────────────────

class TestReadOnlyGuarantee(unittest.TestCase):
    """production DB 只准唯讀 —— 用靜態掃描把這條紅線釘死在測試裡。"""

    HARNESS_FILES = ("harness/trajectory.py", "harness/store.py",
                     "harness/distill.py", "harness/model.py",
                     "harness/__init__.py")

    def _read(self, rel):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            return f.read()

    def test_harness_套件完全不碰_production_DB(self):
        for rel in self.HARNESS_FILES:
            src = self._read(rel)
            for bad in ("canonical.db", "state.db", "CANON_DB", "POCKET_CANON_DB"):
                # 只准出現在註解/docstring 裡(說明紅線),不准出現在程式碼
                code = "\n".join(ln for ln in src.splitlines()
                                 if not ln.strip().startswith("#"))
                code = re.sub(r'"""[\s\S]*?"""', "", code)
                self.assertNotIn(bad, code,
                                 f"{rel} 的程式碼裡出現 {bad} —— harness 不准碰 production DB")

    def test_harness_不_import_bridge(self):
        for rel in self.HARNESS_FILES:
            src = self._read(rel)
            code = re.sub(r'"""[\s\S]*?"""', "", src)
            self.assertNotRegex(code, r"^\s*import bridge",
                                f"{rel} 不該 import bridge(循環依賴 + 不可測)")

    def test_harness_只寫自己的_DB(self):
        st = _store()
        self.assertTrue(st.db_path.endswith(".db"))
        self.assertNotIn("canonical", st.db_path)
        self.assertNotIn("state.db", st.db_path)

    def test_bridge_端唯讀讀_canonical(self):
        """bridge 讀 canonical.db 的地方一律 mode=ro(既有慣例,順手守住)。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "bridge.py"), encoding="utf-8") as f:
            src = f.read()
        # harness 區塊裡不得出現任何 sqlite3.connect(讀 canonical 一律走既有函式)
        start = src.find("# Continual Harness(藍圖 AGENT_INTEROP §2")
        end = src.find("async def _start_agent_registry")
        self.assertGreater(start, 0, "找不到 harness 區塊")
        block = src[start:end]
        self.assertNotIn("sqlite3.connect", block)

    def test_蒸餾器沒有自動核准路徑(self):
        """鐵律:夜批只能提案。distill.py 不准出現 approve/active。"""
        src = self._read("harness/distill.py")
        code = re.sub(r'"""[\s\S]*?"""', "", src)
        code = "\n".join(ln for ln in code.splitlines()
                         if not ln.strip().startswith("#"))
        self.assertNotIn(".approve(", code)
        self.assertNotIn("mark_applied", code)
        self.assertIn("state=proposed", src)      # docstring 有明講


class TestPolishTranscriptUnchanged(unittest.IsolatedAsyncioTestCase):
    """`_polish_transcript` 的 Ollama 呼叫被抽到 harness/model.py 共用。
    這是 production 路徑(會議錄音清稿),refactor 前沒有測試 —— 補上,
    把「行為必須逐項不變」釘死:參數原值、失敗一律回原稿。"""

    async def test_成功時回清稿後的字(self):
        with patch.object(bridge.harness_model, "ollama_text",
                          AsyncMock(return_value="清好的稿")) as mk:
            self.assertEqual(await bridge._polish_transcript("原稿"), "清好的稿")
        kw = mk.await_args.kwargs
        self.assertEqual(kw["model"], bridge._MEETING_POLISH_MODEL)
        self.assertEqual(kw["timeout"], 90)
        self.assertEqual(kw["temperature"], 0.2)
        self.assertEqual(kw["keep_alive"], "15m")
        # num_ctx 公式與 refactor 前逐字相同
        self.assertEqual(kw["num_ctx"], min(40960, max(8192, len("原稿") * 3 + 2048)))
        self.assertTrue(mk.await_args.args[0].endswith("原稿"))

    async def test_逾時回原稿(self):
        with patch.object(bridge.harness_model, "ollama_text",
                          AsyncMock(side_effect=asyncio.TimeoutError())):
            self.assertEqual(await bridge._polish_transcript("原稿"), "原稿")

    async def test_例外回原稿(self):
        with patch.object(bridge.harness_model, "ollama_text",
                          AsyncMock(side_effect=RuntimeError("ollama 掛了"))):
            self.assertEqual(await bridge._polish_transcript("原稿"), "原稿")

    async def test_空回應回原稿_空輸入不呼叫模型(self):
        with patch.object(bridge.harness_model, "ollama_text",
                          AsyncMock(return_value="")):
            self.assertEqual(await bridge._polish_transcript("原稿"), "原稿")
        with patch.object(bridge.harness_model, "ollama_text", AsyncMock()) as mk:
            self.assertEqual(await bridge._polish_transcript("   "), "   ")
            mk.assert_not_awaited()


class TestHttpRouting(unittest.TestCase):
    """真的走 HTTP(前面那些是直呼 handler)—— 驗路由掛上去了、auth 有效、
    旗標關著時連路由都當作不存在。"""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.client = TestClient(bridge.app)
        cls.auth = {"Authorization": "Bearer test-unit-token"}

    def setUp(self):
        self.st = _store()
        self._p = patch.object(bridge, "_HARNESS_STORE", self.st)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_旗標關_路由回_404(self):
        with patch.dict(os.environ, {"HARNESS": "0"}):
            for path in ("/app/v2/harness/proposals", "/app/v2/harness/status"):
                self.assertEqual(
                    self.client.get(path, headers=self.auth).status_code, 404)
            self.assertEqual(self.client.post(
                "/app/v2/harness/proposals/x/approve",
                headers=self.auth, json={}).status_code, 404)

    def test_旗標開_全套走得通(self):
        with patch.dict(os.environ, {"HARNESS": "1"}):
            p = self.st.propose("memory", key="k", payload={"fact": "事實"},
                                rationale="因為", preview="+ 事實")
            r = self.client.get("/app/v2/harness/proposals", headers=self.auth)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(len(r.json()["proposals"]), 1)
            self.assertEqual(
                self.client.get("/app/v2/harness/proposals").status_code, 401)
            r = self.client.post(f"/app/v2/harness/proposals/{p['id']}/approve",
                                 headers=self.auth, json={"by": "善彰"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["proposal"]["state"], "active")
            self.assertEqual(self.client.post(
                f"/app/v2/harness/proposals/{p['id']}/approve",
                headers=self.auth, json={}).status_code, 409)
            self.assertEqual(self.client.get(
                "/app/v2/harness/status", headers=self.auth
            ).json()["active"]["memory"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
