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

import json
import re
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
_CMD_MAX = 500                # 工具 cmd/路徑截斷上限 — 140 會把深路徑攔腰砍斷，
                              # app 的 diff chip 拿殘缺路徑去打 /filediff 就 404（#38 缺口）
_PATCH_MAX = 20_000           # tool_call.patch.text 上限（契約 §2）


def _tool_patch(name: str, inp) -> dict | None:
    """Edit/Write/MultiEdit/NotebookEdit 的 tool_use input → `tool_call.patch`
    （契約 §2）。從事件自身合成——不回讀 worktree，步驟過後再 commit 也能
    回看單步變更，replay 重放產同一份。事件裡沒有整檔上下文，故 hunk 用裸
    `@@` 分隔、無行號。"""
    if not isinstance(inp, dict):
        return None
    path = inp.get("file_path") or inp.get("notebook_path") or ""
    if name == "Edit":
        pairs = [(inp.get("old_string"), inp.get("new_string"))]
    elif name == "MultiEdit":
        pairs = [(e.get("old_string"), e.get("new_string"))
                 for e in inp.get("edits") or [] if isinstance(e, dict)]
    elif name == "Write":
        pairs = [("", inp.get("content"))]
    elif name == "NotebookEdit":
        pairs = [("", inp.get("new_source"))]
    else:
        return None
    if not path:
        return None
    hunks, adds, dels = [], 0, 0
    for old, new in pairs:
        lines = []
        for ln in str(old or "").splitlines() if old else []:
            lines.append("-" + ln)
            dels += 1
        for ln in str(new or "").splitlines() if new else []:
            lines.append("+" + ln)
            adds += 1
        if lines:
            hunks.append("@@\n" + "\n".join(lines))
    if not hunks:
        return None
    text = f"--- {path}\n+++ {path}\n" + "\n".join(hunks)
    if len(text) > _PATCH_MAX:
        text = text[:_PATCH_MAX] + "\n…(截斷)"
    return {"path": path, "text": text, "adds": adds, "dels": dels}


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
                cmd = str(cmd).splitlines()[0][:_CMD_MAX] if cmd else ""
                fb = f"› 🔧 {name}" + (f" `{cmd}`" if cmd else "")
                body = {"tool": name, "summary": cmd, "fallback_text": fb}
                patch = _tool_patch(name, inp)
                if patch:
                    body["patch"] = patch
                cards.append(make_card(cid(i), turn_id, "assistant", "tool_call",
                                       body, ts))
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


# ───────────────────────── S2：codex app-server 事件 → 卡片 ─────────────────


def _cx_user_text(content) -> str:
    """codex userMessage 的 content blocks → 純文字（同 bridge._codex_user_input_text
    的形狀，複製一小份讓本模組自包含）。"""
    parts = []
    for item in content or []:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "text" and item.get("text"):
            parts.append(item["text"])
        elif t == "localImage" and item.get("path"):
            parts.append(f"[圖片: {item['path']}]")
        elif t == "image" and item.get("url"):
            parts.append(f"[圖片: {item['url']}]")
        elif item.get("path"):
            parts.append(f"[{t or 'file'}: {item['path']}]")
    return "\n".join(parts).strip()


def _cx_tool_label(item: dict) -> str:
    """item → status label 用的短工具名（cc_status_label 的 last_tool 位）。"""
    t = item.get("type")
    if t == "commandExecution":
        return "command"
    if t == "fileChange":
        return "fileChange"
    if t == "mcpToolCall":
        return f"{item.get('server', 'mcp')}.{item.get('tool', 'tool')}"
    if t == "dynamicToolCall":
        label = item.get("tool") or "tool"
        ns = item.get("namespace")
        return f"{ns}.{label}" if ns else label
    if t == "webSearch":
        return "webSearch"
    if t == "imageGeneration":
        return "imageGeneration"
    return ""


def codex_item_to_cards(item: dict, turn_id: str = "",
                        phase: str = "completed") -> list[dict]:
    """一個 codex app-server item → 0..n 張卡。

    item id 是 app-server 的穩定識別 → 卡 id 由它衍生；同一 item 的
    started/delta/completed 反覆 upsert 同一張卡（rev 遞增、final 收尾）——
    這是契約 §2 rev/upsert 真串流的首個來源。started 階段 final=False，
    completed 收 final=True。
    """
    if not isinstance(item, dict):
        return []
    iid = item.get("id")
    if not iid:
        return []
    t = item.get("type")
    final = phase == "completed"
    cid = f"card-cx-{iid}"
    cards: list[dict] = []

    if t == "userMessage":
        text = _cx_user_text(item.get("content") or [])
        if text:
            cards.append(make_card(cid, turn_id, "user", "text",
                                   {"text": text, "fallback_text": text}))
        return cards

    if t == "agentMessage":
        text = item.get("text") or ""
        if text:
            cards.append(make_card(cid, turn_id, "assistant", "markdown",
                                   {"text": text, "fallback_text": text},
                                   final=final))
        return cards

    if t == "reasoning":
        summary = "\n".join(item.get("summary") or []).strip()
        if summary:
            think = summary[:_THINKING_MAX]
            cards.append(make_card(cid, turn_id, "assistant", "text",
                                   {"text": f"💭 {think}",
                                    "fallback_text": f"💭 {think}"},
                                   final=final))
        return cards

    if t == "plan":
        text = item.get("text") or ""
        if text:
            cards.append(make_card(cid, turn_id, "assistant", "markdown",
                                   {"text": text, "fallback_text": text},
                                   final=final))
        return cards

    if t == "commandExecution":
        cmd = (item.get("command") or "").strip()
        cmd1 = cmd.splitlines()[0][:_CMD_MAX] if cmd else ""
        fb = f"› 🔧 command" + (f" `{cmd1}`" if cmd1 else "")
        cards.append(make_card(cid, turn_id, "assistant", "tool_call",
                               {"tool": "command", "summary": cmd1,
                                "fallback_text": fb}, final=final))
        out = (item.get("aggregatedOutput") or "").strip()
        if final and out:
            short = out[:_TOOL_RESULT_MAX]
            if len(out) > _TOOL_RESULT_MAX:
                short += "\n…(截斷)"
            cards.append(make_card(f"{cid}-r", turn_id, "assistant",
                                   "tool_result",
                                   {"text": short,
                                    "fallback_text": f"↳ 結果\n{short[:1000]}"}))
        return cards

    if t == "fileChange":
        rows = []
        for c in (item.get("changes") or [])[:20]:
            if not isinstance(c, dict):
                continue
            kind = c.get("kind") or {}
            k = kind.get("type") if isinstance(kind, dict) else str(kind)
            rows.append(f"{k or 'change'} {c.get('path', '')}")
        n = len(item.get("changes") or [])
        summary = rows[0] if len(rows) == 1 else f"{n} 檔變更"
        detail = "\n".join(rows) + (f"\n…共 {n} 檔" if n > 20 else "")
        cards.append(make_card(cid, turn_id, "assistant", "tool_call",
                               {"tool": "fileChange", "summary": summary,
                                "detail": detail,
                                "fallback_text": f"› 📝 {summary}\n{detail}"},
                               final=final))
        return cards

    if t in ("mcpToolCall", "dynamicToolCall", "webSearch", "imageGeneration"):
        label = _cx_tool_label(item)
        summary = str(item.get("query") or "")[:_CMD_MAX] if t == "webSearch" else ""
        body = {"tool": label, "summary": summary,
                "fallback_text": f"› 🔧 {label}" + (f" `{summary}`" if summary else "")}
        err = item.get("error") or {}
        if isinstance(err, dict) and err.get("message"):
            body["detail"] = f"⚠️ {err['message']}"
        cards.append(make_card(cid, turn_id, "assistant", "tool_call", body,
                               final=final))
        return cards

    return []


class ApprovalCardMixin:
    """approval pending/resolved → 卡片（契約 §6），CC(tmux watcher) /
    codex(app-server) / hermes(create 流程) 三 provider 共用同一套
    upsert/resolve 邏輯 —— 不重寫三份（APPROVAL_HUB_SPEC.md §4/§6 A3）。

    宿主只需要提供：
    - `store`（卡片庫）——沒有這個屬性就代表宿主自己就是 store
      （見 `_appr_store`；S1 的 `SessionCardStore` 直接掛這個 mixin，
      不另外包一層 digest 物件）。
    - 類屬性 `_appr_prefix`（卡 id 前綴）、`_appr_source`（卡 body.source）、
      `_appr_default_title`（record 缺 title 時的預設，通常用不到——
      approvals 的統一物件一定帶 title）。
    - 選配 `prompt` 屬性 / `_status()` 方法——有就順手同步「等待核准」
      人話 label（CodexThreadDigest/PersonaDigest 都有）；CC 的
      SessionCardStore 沒有，因為它的 label 已由 `_cc_card_follower`
      依 tmux prompt 另外算好，不需要這條路再插一手。
    """
    _appr_prefix = "card-appr-"
    _appr_source = "hermes"
    _appr_default_title = "需要核准"

    def _appr_store(self):
        return getattr(self, "store", self)

    def handle_approval(self, record: dict) -> None:
        """pending approval record（bridge 的統一 approval 物件，A1 wire
        shape）→ approval 卡（契約 §6）＋（有 prompt/_status 才做的）
        「等待核准」status。"""
        if not record or not record.get("id"):
            return
        title = record.get("title") or self._appr_default_title
        store = self._appr_store()
        if hasattr(self, "prompt"):
            self.prompt = title
        body = {"approval_id": record["id"], "title": title,
                "detail": record.get("detail") or "",
                # 選項改由發起方宣告(record["options"]);沒宣告才退回二元預設。
                # 不再把「允許/拒絕」寫死在這裡——見 CHOICE_GATEWAY_CONTRACT §1。
                "options": record.get("options") or [
                    {"key": "approve", "label": "允許", "style": "primary"},
                    {"key": "deny", "label": "拒絕", "style": "danger"}],
                "source": self._appr_source,
                "fallback_text": f"🔐 {title}"}
        if record.get("kind") in ("question", "notice"):
            # A3:加值欄位 —— app 靠它做 question 直選/notice 單鍵;
            # 不認得就忽略(fallback 原則)。
            body["kind"] = record["kind"]
        store.upsert_card(make_card(
            f"{self._appr_prefix}{record['id']}", store.turn_id, "system",
            "approval", body, final=False))
        if hasattr(self, "_status"):
            self._status()

    def resolve_approval(self, record: dict, status: str) -> None:
        """核准已決/失效 → 同卡收尾（options 清空、resolved 註記）。"""
        if not record or not record.get("id"):
            return
        title = record.get("title") or self._appr_default_title
        store = self._appr_store()
        if hasattr(self, "prompt"):
            self.prompt = None
        store.upsert_card(make_card(
            f"{self._appr_prefix}{record['id']}", store.turn_id, "system",
            "approval",
            {"approval_id": record["id"], "title": title,
             "options": [], "resolved": status, "source": self._appr_source,
             "fallback_text": f"🔐 {title} — {status}"}))
        if hasattr(self, "_status"):
            self._status()


class CodexThreadDigest(ApprovalCardMixin):
    """S2：一個 codex thread 的事件驅動 digest（無輪詢——status/turn/卡片
    全由 app-server 通知推進）。冷載 seed 走 thread/turns/list（舊→新），
    item id 穩定 → seed 與 live 事件 upsert 同一批卡 id，重疊只是 rev 遞增。
    """
    _appr_prefix = "card-cx-appr-"
    _appr_source = "codex"
    _appr_default_title = "Codex approval"

    def __init__(self):
        self.store = SessionCardStore()
        self.agent_text: dict[str, str] = {}   # itemId → delta 累積文字
        self.busy = False
        self.prompt = None                     # pending approval title（label 素材）
        self.seeded = False

    def _status(self):
        self.store.set_status({
            "busy": self.busy, "mode": None, "prompt": self.prompt,
            "phase": "run" if self.busy else "idle",
            "label": cc_status_label(self.busy, self.prompt,
                                     self.store.last_tool, self.store.saw_output),
        })

    def seed_turns(self, turns: list):
        """thread/turns/list 的 data（呼叫端先 reverse 成舊→新）→ 卡片庫。"""
        for turn in turns or []:
            tid = str(turn.get("id") or "")
            for item in (turn.get("items") or []):
                for card in codex_item_to_cards(item, turn_id=tid):
                    self.store.upsert_card(card)

    def handle(self, method: str, params: dict):
        """一則 app-server 通知 → 卡片/turn/status 事件。"""
        if method == "turn/started":
            turn = params.get("turn") or {}
            self.store.turn_id = str(turn.get("id") or "")
            self.busy = True
            self.store.saw_output = False
            self.store.last_tool = ""
            self.store.push_turn("begin", self.store.turn_id)
            self._status()
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            err = turn.get("error") if isinstance(turn, dict) else None
            self.busy = False
            self.prompt = None
            self.store.push_turn("end", self.store.turn_id or str(turn.get("id") or ""))
            if err:
                msg = str(err.get("message", err))
                self.store.upsert_card(make_card(
                    f"card-cx-err-{self.store.seq}", self.store.turn_id, "system",
                    "text", {"text": f"⚠️ {msg}", "fallback_text": f"⚠️ {msg}"}))
            self.store.turn_id = ""
            self.store.last_tool = ""
            self._status()
        elif method == "item/agentMessage/delta":
            iid = params.get("itemId")
            delta = params.get("delta") or ""
            if not iid or not delta:
                return
            text = self.agent_text.get(iid, "") + delta
            self.agent_text[iid] = text
            self.store.saw_output = True
            self.store.last_tool = ""
            self.store.upsert_card(make_card(
                f"card-cx-{iid}", self.store.turn_id, "assistant", "markdown",
                {"text": text, "fallback_text": text}, final=False))
            self._status()
        elif method in ("item/started", "item/completed"):
            item = params.get("item") or {}
            phase = "started" if method == "item/started" else "completed"
            if item.get("type") == "agentMessage":
                self.agent_text.pop(item.get("id"), None)
                self.store.saw_output = True
            else:
                label = _cx_tool_label(item)
                if label:
                    self.store.last_tool = label if phase == "started" else ""
            for card in codex_item_to_cards(item, self.store.turn_id, phase=phase):
                self.store.upsert_card(card)
            self._status()

    # handle_approval / resolve_approval：見 ApprovalCardMixin（A3，三 provider 共用）。


class SessionCardStore(ApprovalCardMixin):
    """契約 §1/§3 的 per-session 卡片庫 + 事件 ring buffer。

    只在 asyncio 事件圈裡存取（bridge 全程單圈），不需要鎖。
    seq per-session 嚴格遞增；ring 滿了丟最舊；`since()` 補洞、超範圍回
    None（呼叫端回 410 → app 改走 snapshot 冷載）。ping 不進 ring、不佔 seq。

    掛 `ApprovalCardMixin`：CC(S1) 沒有獨立 digest 物件，approval 卡
    （`_cc_approval_watcher` 建立/過期時）直接對 store 本身 handle_approval/
    resolve_approval（`_appr_store()` 偵測不到 `self.store` → 回傳 self）；
    CC 沒有 `prompt`/`_status()`，mixin 用 hasattr 探測就會跳過那段，
    label 仍由 `_cc_card_follower` 依 tmux prompt 另外算，不衝突。
    """
    _appr_prefix = "card-cc-appr-"
    _appr_source = "claude_code"
    _appr_default_title = "Claude Code 等待核准"

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


# ───────────────── B3:studio-card 圍欄協定(persona 結構化輸出)──────────────
# persona/工具在回覆裡嵌 ```studio-card 圍欄 JSON(晨報表格、持倉 kv…),digest
# 抽成獨立結構化卡,原文位置換成 fallback_text——手機原生渲染表格,舊 client
# 看純文字,兩邊都不壞。這是 Hermes 拓撲下「persona 工具的正確呈現形態」。
#
# 圍欄格式:
#   ```studio-card
#   {"kind": "table", "title": "持倉", "columns": [...], "rows": [[...]],
#    "fallback_text": "..."}
#   ```
# kind ∈ {table, kv, text, markdown};fallback_text 缺了由 digest 代生。
# 壞 JSON / 不認得的 kind → 圍欄原文保留(不吞內容,fallback 鐵律)。

_SC_FENCE_RE = re.compile(r"```studio-card[ \t]*\n(.*?)\n?```", re.DOTALL)
# choices = 選擇閘道公版(docs/CHOICE_GATEWAY_CONTRACT.md):agent 吐一個
# ```studio-card {kind:"choices", options:[…]}``` → App 渲染成原生按鈕。
# body(含 options/rows)原樣過(extract 用 dict(payload)),不需另寫欄位。
_SC_KINDS = ("table", "kv", "text", "markdown", "choices")


def _studio_card_fallback(payload: dict) -> str:
    """缺 fallback_text 時的純文字代生。"""
    title = str(payload.get("title") or "").strip()
    kind = payload.get("kind")
    if kind == "table":
        lines = [title] if title else []
        cols = payload.get("columns") or []
        if cols:
            lines.append(" | ".join(str(c) for c in cols))
        rows = payload.get("rows") or []
        for r in rows[:12]:
            cells = r if isinstance(r, list) else [r]
            lines.append(" | ".join(str(x) for x in cells))
        if len(rows) > 12:
            lines.append(f"…共 {len(rows)} 列")
        return "\n".join(lines).strip() or "studio-card"
    if kind == "kv":
        items = payload.get("items")
        if isinstance(items, dict):
            pairs = list(items.items())
        elif isinstance(items, list):
            pairs = [(d.get("key"), d.get("value"))
                     for d in items if isinstance(d, dict)]
        else:
            pairs = []
        lines = ([title] if title else []) + \
            [f"{k}: {v}" for k, v in pairs[:20]]
        return "\n".join(lines).strip() or "studio-card"
    if kind == "choices":
        # 純文字降級(TG / 不認得 choices 的 client 照樣看得懂,= 契約的「文字備援」)。
        lines = [title] if title else []
        for o in payload.get("options") or []:
            if not isinstance(o, dict):
                continue
            label = str(o.get("label") or "").strip()
            if not label:
                continue
            url = str(o.get("url") or "").strip()
            send = str(o.get("send") or "").strip()
            if url:
                lines.append(f"- {label}:{url}")
            elif send:
                lines.append(f"- {label}(送「{send}」)")
            else:
                lines.append(f"- {label}")
        return "\n".join(lines).strip() or "studio-card"
    text = str(payload.get("text") or "")
    return (f"{title}\n{text}" if title else text).strip() or "studio-card"


def extract_studio_cards(text: str) -> tuple:
    """text → (圍欄換成 fallback_text 的乾淨文字, 結構化卡 body 清單)。
    body 含 kind(呼叫端提出來當卡 kind)與 fallback_text(必附)。"""
    if not text or "```studio-card" not in text:
        return text, []
    bodies: list[dict] = []

    def _sub(m):
        try:
            payload = json.loads(m.group(1))
        except Exception:  # noqa: BLE001
            return m.group(0)
        if not isinstance(payload, dict) or payload.get("kind") not in _SC_KINDS:
            return m.group(0)
        fb = str(payload.get("fallback_text") or "").strip() \
            or _studio_card_fallback(payload)
        body = dict(payload)
        body["fallback_text"] = fb
        bodies.append(body)
        return fb

    return _SC_FENCE_RE.sub(_sub, text), bodies


# ───────────────────────── S3:persona 事件 → 卡片 ───────────────────────────


class PersonaDigest(ApprovalCardMixin):
    """S3:一個 persona 的卡片 digest。三個來源,一個卡片庫:

    - seed:canonical messages(mid 穩定 → 卡 id,重放同 id)。
    - live turn:bridge 發起的 persona 回合(POST /app/v1/messages 的
      run_turn 掛鉤)——delta 累積同一張卡 rev 遞增(真串流),status 人話
      label 原樣透傳,turn begin/end。
    - canonical follower:寫入版本喚醒後補掃(其他路徑寫入的訊息;
      known_mids 去重,live turn 已出過卡的 reply 不會再出一張)。

    限制(v0,與 v1 messages/events 相同):只看 bridge canonical 寫入;
    TG 端直跑的 persona 回合要等該輪訊息落 canonical 才會出卡。

    A3:approval 卡走 `ApprovalCardMixin`(hermes create 流程掛鉤,見
    bridge._hp_cards_feed_approval);`self.prompt` 兼作「有 pending
    待審」旗標,`_status()` 沿用 CC/codex 同款「有 prompt → 等待核准」
    人話 label 優先序。
    """
    _appr_prefix = "card-hp-appr-"
    _appr_source = "hermes"
    _appr_default_title = "需要核准"

    def __init__(self):
        self.store = SessionCardStore()
        self.known_mids: set = set()
        self.turn_text: dict[str, str] = {}   # 進行中 turn cid → 累積文字
        self.busy = False
        self.prompt = None                    # pending approval title(A3)
        self.seeded = False

    def _status(self, label: str = ""):
        self.store.set_status({
            "busy": self.busy, "mode": None, "prompt": self.prompt,
            "phase": "run" if self.busy else "idle",
            "label": label or ("等待核准" if self.prompt else
                               ("回覆中" if self.busy else "待命")),
        })

    def _emit_studio_cards(self, base_cid: str, bodies: list, ts=None):
        """B3:抽出的 studio-card bodies → 結構化卡(id 錨在母卡上,重放穩定)。"""
        for i, body in enumerate(bodies):
            body = dict(body)
            kind = body.pop("kind")
            self.store.upsert_card(make_card(f"{base_cid}-sc{i}", self.store.turn_id,
                                             "assistant", kind, body, ts=ts))

    def message_card(self, m: dict):
        """canonical message dict(_canon_messages 形狀)→ 卡;known 去重。
        assistant 內文先過 B3 studio-card 抽取(圍欄→結構化卡+fallback 文字)。"""
        mid = str(m.get("id") or "")
        if not mid or mid in self.known_mids:
            return
        self.known_mids.add(mid)
        text = m.get("content") or ""
        if not text:
            return
        role = "user" if m.get("role") == "user" else "assistant"
        kind = "text" if role == "user" else "markdown"
        sc_bodies = []
        if role == "assistant":
            text, sc_bodies = extract_studio_cards(text)
        body = {"text": text, "fallback_text": text}
        atts = m.get("attachments") or []
        if atts:
            body["attachments"] = [{"kind": a.get("kind"),
                                    "filename": a.get("filename")}
                                   for a in atts if isinstance(a, dict)]
        ts = _epoch(m.get("ts"))
        self.store.upsert_card(make_card(f"card-hp-{mid}", "", role, kind,
                                         body, ts=ts))
        self._emit_studio_cards(f"card-hp-{mid}", sc_bodies, ts=ts)

    def seed_messages(self, msgs: list):
        for m in msgs or []:
            self.message_card(m)

    # ── live turn 掛鉤(bridge 發起的回合)──

    def turn_begin(self, cid: str, label: str = ""):
        self.busy = True
        self.turn_text[cid] = ""
        self.store.turn_id = f"turn-{cid}"
        self.store.push_turn("begin", self.store.turn_id)
        self._status(label or "已送達 Hermes，等待回覆。")

    def turn_delta(self, cid: str, delta: str):
        if not delta:
            return
        text = self.turn_text.get(cid, "") + delta
        self.turn_text[cid] = text
        self.store.upsert_card(make_card(
            f"card-hp-turn-{cid}", self.store.turn_id, "assistant", "markdown",
            {"text": text, "fallback_text": text}, final=False))
        self._status("回覆中")

    def turn_status(self, label: str):
        if label:
            self._status(label)

    def turn_end(self, cid: str, full_text: str, reply_mid: str = "",
                 error: str = ""):
        """回合收尾:同卡 final=True 全文覆蓋;reply 的 canonical mid 註記
        known,follower 補掃時不會再出第二張。"""
        self.busy = False
        self.turn_text.pop(cid, None)
        if reply_mid:
            self.known_mids.add(str(reply_mid))
        if full_text:
            clean, sc_bodies = extract_studio_cards(full_text)
            self.store.upsert_card(make_card(
                f"card-hp-turn-{cid}", self.store.turn_id, "assistant",
                "markdown", {"text": clean, "fallback_text": clean}))
            self._emit_studio_cards(f"card-hp-turn-{cid}", sc_bodies)
        if error:
            self.store.upsert_card(make_card(
                f"card-hp-turn-{cid}-err", self.store.turn_id, "system", "text",
                {"text": f"⚠️ {error}", "fallback_text": f"⚠️ {error}"}))
        self.store.push_turn("end", self.store.turn_id)
        self.store.turn_id = ""
        self._status()
