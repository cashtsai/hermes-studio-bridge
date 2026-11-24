"""LINE transport receipts and strict reply correlation; no provider credentials."""
import asyncio
import hashlib
import sqlite3
import time
from contextlib import contextmanager

PROVIDERS = frozenset({"hermes", "claude_code", "cc2", "codex", "gemini",
                       "grok", "opencode", "openclaw"})


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Receipts:
    """Host-local outbox. Never replay an uncertain input after a process restart."""
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS line_receipts (
                id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, session_id TEXT NOT NULL,
                input_hash TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL,
                created REAL NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0)""")
            columns = {r["name"] for r in db.execute("PRAGMA table_info(line_receipts)")}
            for name, decl in (("attempts", "INTEGER"), ("next_attempt", "REAL")):
                if name not in columns:
                    db.execute(f"ALTER TABLE line_receipts ADD COLUMN {name} {decl} NOT NULL DEFAULT 0")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, client_id, token, sid, text):
        with self.connect() as db:
            db.execute("DELETE FROM line_receipts WHERE created < ? AND delivered=1",
                       (time.time() - 7 * 86400,))
            cur = db.execute("INSERT OR IGNORE INTO line_receipts "
                             "(id,token_hash,session_id,input_hash,status,result,created,delivered) "
                             "VALUES (?,?,?,?,?,?,?,0)",
                             (client_id, digest(token), sid, digest(text), "pending", "", time.time()))
            row = dict(db.execute("SELECT * FROM line_receipts WHERE id=?", (client_id,)).fetchone())
        if (row["token_hash"], row["session_id"], row["input_hash"]) != (digest(token), sid, digest(text)):
            raise ValueError("client_id conflict")
        return cur.rowcount == 1, row

    def finish(self, client_id, status, result):
        with self.connect() as db:
            db.execute("UPDATE line_receipts SET status=?,result=? WHERE id=? AND status='pending'",
                       (status, result[:12000], client_id))

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE line_receipts SET status='interrupted',result=? WHERE status='pending'",
                       ("主機已重啟，無法確認這輪結果；沒有重送指令，請回 Pocket 查看原對話。",))

    def pending_delivery(self):
        with self.connect() as db:
            # LINE retry keys expire after 24 hours. Never retry ambiguously past it.
            db.execute("UPDATE line_receipts SET delivered=1 WHERE created<? AND status!='pending'",
                       (time.time() - 23 * 3600,))
            return [dict(r) for r in db.execute(
                "SELECT * FROM line_receipts WHERE delivered=0 AND status!='pending' "
                "AND next_attempt<=? ORDER BY created LIMIT 100", (time.time(),))]

    def attempted(self, row):
        with self.connect() as db:
            db.execute("UPDATE line_receipts SET attempts=attempts+1,next_attempt=? WHERE id=?",
                       (time.time() + min(300, 10 * 2 ** min(row["attempts"], 5)), row["id"]))

    def delivered(self, client_id):
        with self.connect() as db:
            db.execute("UPDATE line_receipts SET delivered=1 WHERE id=?", (client_id,))


class ReplyTracker:
    """Accept only the acknowledged turn or a native user echo carrying client_id.

    Do not infer a turn from 'the next assistant message': other devices may be
    using the same session, and an input may be waiting behind an older turn.
    """
    def __init__(self, client_id, turn_id="", native_cc=False):
        self.client_id = client_id
        self.turn_id = turn_id
        self.cards = {}
        self.ends = {}
        self.native_cc = native_cc
        self.native_root = ""
        self.first_final_seq = {}
        self.idle_seq = -1

    def feed(self, event):
        data = event.get("data") or {}
        if event.get("type") == "card.upsert":
            card = data.get("card") or {}
            body = card.get("body") or {}
            if (card.get("role") == "user" and body.get("client_id") == self.client_id
                    and card.get("turn_id") and body.get("delivery") != "queued"):
                self.turn_id = card["turn_id"]
            if card.get("role") == "user" and body.get("client_id") == self.client_id:
                self.native_root = body.get("native_message_id") or self.native_root
            if card.get("id"):
                self.cards[card["id"]] = card
                if body.get("native_message_id") and card.get("final"):
                    self.first_final_seq.setdefault(card["id"], event.get("seq", 0))
        elif event.get("type") == "turn" and data.get("state") == "end":
            self.ends[data.get("turn_id")] = data.get("outcome") or "done"
        elif (event.get("type") == "session.status" and data.get("busy") is False
              and data.get("phase") == "idle" and not data.get("prompt")):
            self.idle_seq = event.get("seq", 0)

    def _cc_reply(self):
        # tmux busy/idle turn IDs are not authoritative. Follow native JSONL
        # parentUuid instead, stopping at every intervening human user message.
        nodes = {}
        for c in self.cards.values():
            b = c.get("body") or {}
            if b.get("native_message_id"):
                nodes[b["native_message_id"]] = (b.get("native_parent_id"), c.get("role"))
        def belongs(mid):
            seen = set()
            while mid and mid not in seen:
                if mid == self.native_root:
                    return True
                seen.add(mid)
                parent, role = nodes.get(mid, (None, None))
                if role == "user":
                    return False
                mid = parent
            return False
        found = [c for c in self.cards.values() if c.get("role") == "assistant"
                 and c.get("kind") == "markdown" and c.get("final")
                 and belongs((c.get("body") or {}).get("native_message_id"))]
        if not found or self.idle_seq <= max(self.first_final_seq.get(c["id"], 0) for c in found):
            return None
        return "\n\n".join(dict.fromkeys(c["body"].get("text") or "" for c in found)).strip() or None

    def result(self):
        if self.native_cc:
            return self._cc_reply() if self.native_root else None
        if not self.turn_id or self.turn_id not in self.ends:
            return None
        texts, errors = [], []
        for card in self.cards.values():
            if card.get("turn_id") != self.turn_id or not card.get("final", False):
                continue
            body = card.get("body") or {}
            if card.get("role") == "system" and body.get("error_code"):
                errors.append("執行遇到錯誤，請回 Pocket 查看。")
            if (card.get("role") == "assistant" and card.get("kind") in {"markdown", "text"}
                    and not body.get("reasoning") and not body.get("is_reasoning")
                    and body.get("phase") != "commentary"
                    and card.get("origin") not in {"reasoning", "thinking"}):
                text = body.get("text") or body.get("fallback_text")
                if text and not text.lstrip().startswith(("💭", "🧠")) and text not in texts:
                    texts.append(text)
        if self.ends[self.turn_id] not in {"done", "completed", "success"}:
            errors.append("這輪未正常完成，請回 Pocket 查看執行狀態。")
        return "\n\n".join(texts + errors) or "這輪已結束，沒有文字回覆；請回 Pocket 查看結果。"


async def wait_reply(store, client_id, since_seq, turn_id="", timeout=1800, native_cc=False):
    tracker = ReplyTracker(client_id, turn_id, native_cc=native_cc)
    deadline = time.monotonic() + timeout
    cursor = since_seq
    store.subscribers += 1
    waker = store.attach_waker()
    try:
        while time.monotonic() < deadline:
            waker.clear()
            events = store.since(cursor)
            if events is None:
                return "對話事件已超出追蹤範圍，請回 Pocket 查看；沒有重送指令。"
            for ev in events:
                tracker.feed(ev)
                cursor = ev["seq"]
            result = tracker.result()
            if result is not None:
                return result
            try:
                await asyncio.wait_for(waker.wait(), max(.01, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
        return "尚未取得這輪回覆，工作可能仍在執行或等待審核，請回 Pocket 查看；沒有重送指令。"
    finally:
        store.subscribers -= 1
        store.detach_waker(waker)
