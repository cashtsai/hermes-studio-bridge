"""Phase 0 §2 — 伺服器端卡片 digest（Terminal Gateway 契約的核心模組）。

一份 parser、伺服器端、所有終端共享（手機 / ESP32 / e-paper 吃同一套）：
provider 原始事件 → 卡片 schema v1 + session 事件信封 {seq, ts, type, data}。

S1 = Claude Code transcript jsonl（本檔 `cc_event_to_cards`）。
S2（codex app-server 事件）/ S3（persona stream）之後各自加一個
`*_to_cards`，共用同一個 `make_card` / `SessionCardStore`。

契約權威文件：studio-os/docs/PHASE0_TERMINAL_GATEWAY_CONTRACT.md（改契約先改文件）。
鐵律：每張卡的 body 必附 `fallback_text` —— 不認得 kind 的 client 一律渲染
它，舊 client 永不壞。
"""

import time


def _epoch(ts) -> float:
    """CC jsonl timestamp（ISO8601 或 epoch）→ epoch float；解不動就當下。"""
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str) and ts:
        try:
            from datetime import datetime
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


def make_card(cid: str, turn_id: str, role: str, kind: str, body: dict,
              ts: float | None = None, rev: int = 1, final: bool = True) -> dict:
    body = dict(body or {})
    body.setdefault("fallback_text", body.get("text") or "")
    return {"id": cid, "turn_id": turn_id, "role": role, "kind": kind,
            "rev": rev, "final": final, "ts": ts if ts is not None else time.time(),
            "body": body}


# 與 bridge._fmt_cc_event 同一份「不是善彰打的」判定 — harness/系統管線不出卡。
PLUMBING_TAGS = ("<task-notification>", "<system-reminder>", "[Internal",
                 "<command-name>", "<local-command")

_TOOL_RESULT_MAX = 2000       # tool_result 卡上限（fallback 再截一半）
_THINKING_MAX = 2000


def _blocks_text(content) -> str:
    """tool_result 的 content（str 或 blocks）→ 純文字。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                parts.append(b["text"])
        return "\n".join(parts)
    return ""


def cc_event_to_cards(d: dict, uid: str, turn_id: str = "") -> list[dict]:
    """一行 CC transcript jsonl 事件 → 0..n 張卡。

    uid = 該事件的穩定識別（jsonl 的 'uuid'；缺了用檔案行號 fallback）——
    卡 id 由它衍生，重放/補洞時同一事件永遠產同一批 id。
    CC jsonl 每行是完整事件（無部分修訂），故卡一律 rev=1, final=True；
    rev/upsert 機制留給 S2/S3 的真串流來源用。
    """
    t = d.get("type")
    msg = d.get("message") or {}
    ts = _epoch(d.get("timestamp"))
    cards: list[dict] = []

    def cid(i: int) -> str:
        return f"card-cc-{uid}-{i}"

    if t == "user":
        content = msg.get("content")
        if isinstance(content, str):
            head = content.lstrip()[:80]
            if any(tag in head for tag in PLUMBING_TAGS):
                return []
            cards.append(make_card(cid(0), turn_id, "user", "text",
                                   {"text": content, "fallback_text": content}, ts))
        elif isinstance(content, list):
            for i, b in enumerate(content):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    txt = _blocks_text(b.get("content"))
                    if txt:
                        short = txt[:_TOOL_RESULT_MAX]
                        if len(txt) > _TOOL_RESULT_MAX:
                            short += "\n…(截斷)"
                        cards.append(make_card(
                            cid(i), turn_id, "assistant", "tool_result",
                            {"text": short,
                             "fallback_text": f"↳ 結果\n{short[:1000]}"}, ts))
        return cards

    if t == "assistant":
        content = msg.get("content")
        if not isinstance(content, list):
            return []
        for i, b in enumerate(content):
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text" and b.get("text"):
                cards.append(make_card(cid(i), turn_id, "assistant", "markdown",
                                       {"text": b["text"], "fallback_text": b["text"]}, ts))
            elif bt == "thinking" and b.get("thinking"):
                think = b["thinking"][:_THINKING_MAX]
                cards.append(make_card(cid(i), turn_id, "assistant", "text",
                                       {"text": f"💭 {think}",
                                        "fallback_text": f"💭 {think}"}, ts))
            elif bt == "tool_use":
                name = b.get("name", "tool")
                inp = b.get("input") or {}
                cmd = (inp.get("command") or inp.get("file_path") or inp.get("path")
                       or inp.get("pattern") or "")
                if not cmd and isinstance(inp, dict):
                    cmd = next((str(v) for v in inp.values()
                                if isinstance(v, (str, int))), "")
                cmd = str(cmd).splitlines()[0][:140] if cmd else ""
                fb = f"› 🔧 {name}" + (f" `{cmd}`" if cmd else "")
                cards.append(make_card(cid(i), turn_id, "assistant", "tool_call",
                                       {"tool": name, "summary": cmd,
                                        "fallback_text": fb}, ts))
        return cards

    return []


def cc_status_label(busy: bool, prompt, last_tool: str = "",
                    saw_output: bool = False) -> str:
    """契約 §1 session.status 的人話 label —— UI 原樣顯示，不再自己猜。"""
    if prompt:
        return "等待核准"
    if not busy:
        return "待命"
    if last_tool:
        return f"執行工具:{last_tool}"
    return "回覆中" if saw_output else "思考中"


class SessionCardStore:
    """契約 §1/§3 的 per-session 卡片庫 + 事件 ring buffer。

    只在 asyncio 事件圈裡存取（bridge 全程單圈），不需要鎖。
    seq per-session 嚴格遞增；ring 滿了丟最舊；`since()` 補洞、超範圍回
    None（呼叫端回 410 → app 改走 snapshot 冷載）。ping 不進 ring、不佔 seq。
    """

    def __init__(self, ring_max: int = 2000, cards_max: int = 600):
        self.seq = 0
        self.events: list[dict] = []      # 事件信封 {seq, ts, type, data}
        self.cards: dict[str, dict] = {}  # card id → 最新 rev 的卡
        self.order: list[str] = []        # card id 到達順序（snapshot 排序）
        self.card_seq: dict[str, int] = {}  # card id → 最後 upsert 的 seq（before_seq 分頁）
        self.ring_max = ring_max
        self.cards_max = cards_max
        self.status: dict = {}            # 最後一筆 session.status data
        self.turn_id = ""                 # 進行中 turn 的 id（"" = 無）
        self.subscribers = 0              # 活躍 SSE 連線數（follower 決定要不要巡 status）
        # 人話 label 的素材（digest 時順手更新）
        self.last_tool = ""               # 本 turn 最後一個 tool_call 的工具名
        self.saw_output = False           # 本 turn 是否已出現助手文字
        # 檔案 tail 游標（S1 CC jsonl 來源用；seed 與 follower 共享，避免重複 digest）
        self.seeded = False
        self.tail_file = ""
        self.tail_pos = 0
        self.tail_lineno = 0

    def _push(self, etype: str, data: dict) -> dict:
        self.seq += 1
        ev = {"seq": self.seq, "ts": time.time(), "type": etype, "data": data}
        self.events.append(ev)
        if len(self.events) > self.ring_max:
            del self.events[:len(self.events) - self.ring_max]
        return ev

    def upsert_card(self, card: dict) -> dict:
        prev = self.cards.get(card["id"])
        if prev:
            # 重放同一事件 → rev 遞增，app 以最高 rev 原位替換。
            card = dict(card)
            card["rev"] = max(card.get("rev", 1), prev.get("rev", 1) + 1)
        else:
            self.order.append(card["id"])
            if len(self.order) > self.cards_max:
                drop = self.order[:len(self.order) - self.cards_max]
                del self.order[:len(self.order) - self.cards_max]
                for cid in drop:
                    self.cards.pop(cid, None)
                    self.card_seq.pop(cid, None)
        self.cards[card["id"]] = card
        ev = self._push("card.upsert", {"card": card})
        self.card_seq[card["id"]] = ev["seq"]
        return ev

    def set_status(self, status: dict):
        """有變才發事件（status 巡邏是輪詢，不能每 tick 都灌 ring）。"""
        if status != self.status:
            self.status = status
            return self._push("session.status", status)
        return None

    def push_turn(self, state: str, turn_id: str = "") -> dict:
        return self._push("turn", {"state": state, "turn_id": turn_id})

    def since(self, since_seq: int):
        """since_seq 之後的事件；洞（已被 ring 擠掉）或 client 領先（bridge
        重啟過）→ None，呼叫端回 410。"""
        if since_seq > self.seq:
            return None
        if self.events and since_seq + 1 < self.events[0]["seq"]:
            return None
        if not self.events and since_seq < self.seq:
            return None
        return [e for e in self.events if e["seq"] > since_seq]

    def snapshot(self, limit: int = 100, before_seq: int | None = None) -> dict:
        ids = self.order
        if before_seq is not None:
            ids = [i for i in ids if self.card_seq.get(i, 0) < before_seq]
        ids = ids[-max(1, limit):]
        return {"cards": [self.cards[i] for i in ids if i in self.cards],
                "latest_seq": self.seq}

    def ping(self) -> dict:
        """keepalive 信封 — 不進 ring、不佔 seq。"""
        return {"seq": self.seq, "ts": time.time(), "type": "ping", "data": {}}
