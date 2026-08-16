"""CC provider v2(`cc2:`)— 用 Claude Agent SDK 驅動 Claude Code,與 tmux TUI
路徑並行(旗標 `CC_SDK_PROVIDER=1`,預設 OFF = 零行為改變)。

為什麼要有這條路(docs/CINDY_VS_POCKET_ARCH_20260816.md §三):tmux 路徑的
pane-diff 仿串流會被 TUI 版面漂移打壞、審批靠 pane 擷取猜選單、中斷靠
send-keys Esc。Agent SDK 給的是結構化管道:
- include_partial_messages → 真 token delta,直接餵現成的 card.upsert rev++
- can_use_tool → 審批變成結構化回呼,接進 Pocket 審核中心(fail-closed)
- resume + 「No conversation found」自癒重建 → 續傳不再依賴 tmux 活著
- interrupt() → 停止不再是按鍵模擬

設計鐵律:
- 本模組**絕不**在 import 時載入 claude_agent_sdk(lazy import 於 `_sdk()`)
  —— SDK 缺席/壞掉不准影響 bridge 啟動(旗標關著時連 import 都不會發生)。
- 不 import bridge(避免循環);bridge 用 `configure()` 注入 log 與 approvals
  DB 掛鉤,掛鉤全部 fail-safe —— provider 例外絕不外洩打斷 dispatch 路徑。
- 卡片流契約與 S1/S2/S3/S4 完全同款(SessionCardStore:card.upsert rev++、
  turn begin/end、session.status busy/idle、ApprovalCardMixin 審批卡),
  Pocket app 零改動即可渲染。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import carddigest

# ── bridge 注入面(configure)────────────────────────────────────────────────
# 預設全 no-op:單測可以不接 bridge 直接驅動本模組。
LOG = None                 # bridge._log_event 同形:LOG(event, **fields)
APPROVAL_DB_UPSERT = None  # callable(record) — pending 落 canonical approvals 表
APPROVAL_DB_MARK = None    # callable(aid, status) — 決議/逾時收尾 DB 列


def configure(log=None, approval_db_upsert=None, approval_db_mark=None) -> None:
    """bridge 開機時接線。掛鉤保存在模組層,session 物件不各自持有。"""
    global LOG, APPROVAL_DB_UPSERT, APPROVAL_DB_MARK
    if log is not None:
        LOG = log
    if approval_db_upsert is not None:
        APPROVAL_DB_UPSERT = approval_db_upsert
    if approval_db_mark is not None:
        APPROVAL_DB_MARK = approval_db_mark


def _log(event: str, **fields) -> None:
    """觀測掛鉤 fail-safe:log 本身壞掉也不准打斷 provider 流程。"""
    try:
        if LOG is not None:
            LOG(event, **fields)
    except Exception:  # noqa: BLE001
        pass


def _db_hook(hook, *args) -> None:
    """approvals DB 掛鉤 fail-safe:DB 掛了審批照走(卡片流+記憶體等待者是
    真相;DB 只是審核中心的鏡像)。"""
    try:
        if hook is not None:
            hook(*args)
    except Exception as e:  # noqa: BLE001
        _log("cc2_approval_db_hook_failed", error=type(e).__name__,
             error_message=str(e)[:200])


# ── 旗標 / env 旋鈕(全部 per-call 讀,照 _agent_call_enabled 慣例)──────────

def enabled() -> bool:
    """旗標每次呼叫讀 env:預設 OFF,merge 零風險;plist 加 CC_SDK_PROVIDER=1
    後重啟啟用(同 AGENT_CALL / CC_TOKEN_STREAM 的開關慣例)。"""
    return str(os.environ.get("CC_SDK_PROVIDER", "")).strip().lower() in (
        "1", "true", "yes", "on")


def state_path() -> str:
    """session 註冊表落地路徑。測試**必須**設 CC_SDK_STATE 指到 temp,
    不准碰 production 的 ~/.pocket。"""
    return os.path.expanduser(
        os.environ.get("CC_SDK_STATE", "") or "~/.pocket/cc-sdk-sessions.json")


def approval_timeout() -> float:
    """can_use_tool 等決議的上限秒數。110s < CLI 端 stdio 審批的預設逾時,
    先於上游放棄 → 我們能保證送回明確 deny(fail-closed,鏡像 Cindy)。"""
    try:
        return float(os.environ.get("CC_SDK_APPROVAL_TIMEOUT", "") or 110.0)
    except ValueError:
        return 110.0


def history_seed_limit() -> int:
    """歷史種子上限(0 = 關閉):session 重建時從 SDK transcript jsonl 撈
    最後 N 則 user/assistant 文字訊息餵進卡片庫,先於一切 live 事件。"""
    try:
        return int(os.environ.get("CC_SDK_HISTORY_SEED", "") or 40)
    except ValueError:
        return 40


def _claude_projects_root() -> str:
    """SDK transcript 落地根目錄(claude CLI 慣例 ~/.claude/projects)。
    測試以 CC_SDK_CLAUDE_PROJECTS 指到 temp fixture,不碰真 ~/.claude。"""
    return os.path.expanduser(
        os.environ.get("CC_SDK_CLAUDE_PROJECTS", "") or "~/.claude/projects")


# ── SDK lazy import ─────────────────────────────────────────────────────────

_sdk_mod = None            # 測試以假模組覆蓋這顆(cc_sdk._sdk_mod = fake)


def _sdk():
    """claude_agent_sdk 的唯一入口:lazy、可注入。任何 SDK 類別/函式都經由
    這裡取用,不在模組層 import —— SDK 壞掉只會壞 cc2 呼叫,不會壞 bridge。"""
    global _sdk_mod
    if _sdk_mod is None:
        import claude_agent_sdk  # noqa: PLC0415
        _sdk_mod = claude_agent_sdk
    return _sdk_mod


PERMISSION_MODES = ("acceptEdits", "bypassPermissions", "default", "plan")


def _clamp_mode(mode: str) -> str:
    return mode if mode in PERMISSION_MODES else "acceptEdits"


def _tool_summary(tool: str, tool_input: dict) -> str:
    """tool_use 的一行摘要(工具卡 / 審批 detail 共用)。Bash 顯示指令、檔案
    工具顯示路徑,其他壓成 compact JSON —— 手機上一行看得懂就好。"""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool == "Bash" and tool_input.get("command"):
        txt = str(tool_input["command"]).splitlines()[0]
    elif tool_input.get("file_path"):
        txt = str(tool_input["file_path"])
    elif tool_input.get("path"):
        txt = str(tool_input["path"])
    else:
        try:
            txt = json.dumps(tool_input, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            txt = str(tool_input)
    txt = " ".join(txt.split())
    return txt[:117] + "…" if len(txt) > 120 else txt


# ── 歷史種子(transcript jsonl → 卡片)────────────────────────────────────────
# SDK 每條 session 都落 ~/.claude/projects/<munged-workdir>/<sid>.jsonl
# (munge 慣例同 bridge._cc_project_dir:'/'→'-')。cc2 card store 是 live-only
# in-memory —— bridge 重啟後 app 打開會是一片空白,所以 session 重建時把最近
# 的對話種回去。解析自包含(不 import bridge),過濾清單鏡像 _fmt_cc_event。

_SEED_SKIP_TAGS = ("<task-notification>", "<system-reminder>", "[Internal",
                   "<command-name>", "<local-command", "[Your previous response")


def _seed_row(d):
    """一行 transcript jsonl → (role, text, ts) 或 None(工具水管/系統訊息/
    非文字內容一律略過 —— 種子只還原「人打的字」與「模型的回覆」)。"""
    if not isinstance(d, dict):
        return None
    t = d.get("type")
    msg = d.get("message") if isinstance(d.get("message"), dict) else {}
    ts = carddigest._epoch(d.get("timestamp"))
    if t == "user":
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            return None            # list = tool_result 水管,不是打字輸入
        if any(tag in content.lstrip()[:80] for tag in _SEED_SKIP_TAGS):
            return None
        return ("user", content, ts)
    if t == "assistant":
        content = msg.get("content")
        if not isinstance(content, list):
            return None
        texts = [b.get("text") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"
                 and b.get("text")]
        if not texts:
            return None
        return ("assistant", "\n".join(texts), ts)
    return None


# ── 卡片流 digest(契約 §1/§3/§6,與 S2/S3/S4 同款)──────────────────────────

class CC2Digest(carddigest.ApprovalCardMixin):
    """一條 cc2 session 的卡片 digest。來源 = 歷史種子(session 重建時從
    SDK transcript jsonl 撈最近對話,見 `CCSdkSession._seed_history`)+
    live SDK 訊息流;種子一定先於 live 事件落卡,順序不倒置。

    出卡規則(app 零改動的關鍵):
    - assistant 文字:stream_event 的 text_delta 累積成**同一張** draft 卡
      rev++/final:false;整則 AssistantMessage 落定 → 同卡 final:true 全文覆蓋。
    - tool_use block → 一張精簡工具卡(kind "text","🔧 工具: 摘要")。
    - ResultMessage → turn end 事件 + status idle;is_error → 錯誤卡。
    - 審批卡走 ApprovalCardMixin(三 provider 同一套 upsert/resolve)。
    """
    _appr_prefix = "card-cc2-appr-"
    _appr_source = "cc2"
    _appr_default_title = "Claude Code 等待核准"

    def __init__(self, name: str):
        self.name = name
        self.store = carddigest.SessionCardStore()
        self.store.media_session_id = f"cc2:{name}"
        self.busy = False
        self.prompt = None          # pending approval title(mixin 會讀寫)
        self.queue_depth = 0
        self._seq = 0               # 卡 id 序號(單調遞增,replay 不撞號)
        self._draft_id = ""         # 進行中 assistant draft 卡 id
        self._draft_text = ""

    def _next_id(self, tag: str) -> str:
        self._seq += 1
        return f"card-cc2-{self.name}-{tag}{self._seq}"

    def _status(self, label: str = ""):
        self.store.set_status({
            "busy": self.busy, "mode": None, "prompt": self.prompt,
            "phase": "run" if self.busy else "idle",
            "queue_depth": self.queue_depth,
            "label": label or ("等待核准" if self.prompt else
                               ("回覆中" if self.busy else "待命")),
        })

    def turn_begin(self, turn_id: str):
        self.busy = True
        self.store.turn_id = turn_id
        self.store.saw_output = False
        self.store.last_tool = ""
        self.store.push_turn("begin", turn_id)
        self._status("已送出,等待回覆。")

    def user_card(self, text: str):
        self.store.upsert_card(carddigest.make_card(
            self._next_id("u"), self.store.turn_id, "user", "text",
            {"text": text, "fallback_text": text}))

    def seed_card(self, role: str, text: str, ts: float):
        """歷史種子卡:final:true、帶原始 ts(權威時間)、不掛 turn。"""
        self.store.upsert_card(carddigest.make_card(
            self._next_id("h"), "", role, "markdown",
            {"text": text, "fallback_text": text}, ts=ts))

    def text_delta(self, delta: str):
        if not delta:
            return
        if not self._draft_id:
            self._draft_id = self._next_id("m")
            self._draft_text = ""
        self._draft_text += delta
        self.store.saw_output = True
        self.store.last_tool = ""
        self.store.upsert_card(carddigest.make_card(
            self._draft_id, self.store.turn_id, "assistant", "markdown",
            {"text": self._draft_text, "fallback_text": self._draft_text},
            final=False))
        self._status("回覆中")

    def assistant_final(self, text: str):
        """整則 assistant 訊息落定:有 draft 就同卡 final:true 全文覆蓋(全文
        為權威,delta 漏拼也會被蓋正);沒 draft(partial 缺席)直接出終卡。"""
        if text:
            cid = self._draft_id or self._next_id("m")
            self.store.saw_output = True
            self.store.upsert_card(carddigest.make_card(
                cid, self.store.turn_id, "assistant", "markdown",
                {"text": text, "fallback_text": text}))
        self._draft_id = ""
        self._draft_text = ""

    def tool_card(self, tool: str, summary: str):
        text = f"🔧 {tool}: {summary}" if summary else f"🔧 {tool}"
        self.store.upsert_card(carddigest.make_card(
            self._next_id("t"), self.store.turn_id, "assistant", "text",
            {"text": text, "fallback_text": text}))
        self.store.last_tool = tool
        self._status(f"執行 {tool}")

    def error_card(self, text: str):
        self.store.upsert_card(carddigest.make_card(
            self._next_id("e"), self.store.turn_id, "system", "text",
            {"text": f"⚠️ {text}", "fallback_text": f"⚠️ {text}"}))

    def turn_end(self, error: str = ""):
        self.busy = False
        if error:
            self.error_card(error)
        self.store.push_turn("end", self.store.turn_id)
        self.store.turn_id = ""
        self.store.last_tool = ""
        self._draft_id = ""
        self._draft_text = ""
        self._status()


# ── session 本體 ────────────────────────────────────────────────────────────

class CCSdkSession:
    """一條 cc2 session = 一顆常駐 ClaudeSDKClient(跨 turn 保活)+ 一份
    digest + 一條輸入佇列。同時只跑一個 turn;忙碌時輸入排隊,turn 結束
    drain(鏡像 CX pending_inputs 語意)。client 死掉(process 沒了/串流斷)
    → 錯誤卡 + 收 idle + teardown,下一則輸入 lazy 重建(鏡像 Cindy 的
    teardownDeadHandle)。"""

    def __init__(self, registry: "CCSdkRegistry", name: str,
                 workdir: str = "", permission_mode: str = "",
                 sdk_session_id: str = ""):
        self.registry = registry
        self.name = name
        self.workdir = os.path.expanduser(workdir or "~")
        self.permission_mode = _clamp_mode(permission_mode or "acceptEdits")
        self.sdk_session_id = sdk_session_id or ""
        self.digest = CC2Digest(name)
        self.client = None
        self.queue: list[str] = []       # 忙碌時的輸入佇列(turn end 後 drain)
        self.busy = False
        self._task: asyncio.Task | None = None
        # approve_for_session 記憶:此 session 已「本次全允許」的工具名。
        # 純 in-memory —— bridge 重啟/session 重建即清空,之後第一次仍會
        # 出卡再問(fail-safe:寧可多問,不留跨進程的隱形放行)。
        self.session_allowed_tools: set[str] = set()
        self._seed_history()

    def _seed_history(self) -> None:
        """歷史種子:sdk_session_id 對應的 transcript jsonl 撈最後 N 則
        (CC_SDK_HISTORY_SEED,預設 40)user/assistant 文字訊息 → final:true
        卡(原角色/原 ts),先於一切 live 事件。jsonl 缺席 = 全新對話,安靜
        跳過;整檔讀不動/個別行壞掉容忍,各 log 一次,絕不擋 session 起步。"""
        if not self.sdk_session_id:
            return
        limit = history_seed_limit()
        if limit <= 0:
            return
        path = os.path.join(_claude_projects_root(),
                            self.workdir.replace("/", "-"),
                            f"{self.sdk_session_id}.jsonl")
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return
        except Exception as e:  # noqa: BLE001
            _log("cc2_history_seed_failed", session=self.name,
                 error=type(e).__name__, error_message=str(e)[:200])
            return
        rows, bad = [], 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = _seed_row(json.loads(line))
            except Exception:  # noqa: BLE001
                bad += 1
                continue
            if row is not None:
                rows.append(row)
        for role, text, ts in rows[-limit:]:
            self.digest.seed_card(role, text, ts)
        if bad:
            _log("cc2_history_seed_bad_lines", session=self.name,
                 skipped=bad)
        if rows:
            _log("cc2_history_seeded", session=self.name,
                 cards=min(len(rows), limit))

    # ── 輸入 ──

    async def submit(self, text: str) -> dict:
        """統一輸入入口(v2_session_input 的 cc2 分支):忙碌 → 排隊不回 4xx
        (與 CX 同語意,app 靠 `queued` 欄位畫「已排入佇列」)。"""
        if self.busy or (self._task is not None and not self._task.done()):
            self.queue.append(text)
            self.digest.queue_depth = len(self.queue)
            self.digest._status()
            _log("cc2_input_queued", session=self.name, depth=len(self.queue))
            return {"delivery": "queued", "queued": True,
                    "queue_depth": len(self.queue)}
        self._start_turn(text)
        return {"delivery": "accepted", "queued": False}

    def _start_turn(self, text: str) -> None:
        self.busy = True
        self._task = asyncio.get_event_loop().create_task(
            self._run_turn_safe(text))

    async def _run_turn_safe(self, text: str) -> None:
        """turn 外殼:任何例外都收斂成錯誤卡 + idle,**絕不**讓 provider
        例外往上炸掉 dispatch/通知路徑(bridge 全域慣例)。"""
        try:
            await self._run_turn(text)
        except asyncio.CancelledError:
            self.digest.turn_end()
            raise
        except Exception as e:  # noqa: BLE001
            _log("cc2_turn_error", session=self.name, error=type(e).__name__,
                 error_message=str(e)[:200])
            await self._teardown_client()
            self.digest.turn_end(error=f"{type(e).__name__}: {str(e)[:200]}")
        finally:
            self.busy = False
            self._task = None
            self.registry.save()
            if self.queue:                      # turn 結束 → 放行下一則
                nxt = self.queue.pop(0)
                self.digest.queue_depth = len(self.queue)
                self._start_turn(nxt)

    async def _run_turn(self, text: str) -> None:
        turn_id = f"turn-cc2-{uuid.uuid4().hex[:10]}"
        self.digest.turn_begin(turn_id)
        self.digest.user_card(text)
        for attempt in (0, 1):
            try:
                client = await self._ensure_client()
                await client.query(text)
                async for msg in client.receive_response():
                    if self._handle_message(msg):
                        return
                # 沒等到 ResultMessage 串流就收了 = client 半路死掉
                raise RuntimeError("cc2 stream ended without ResultMessage")
            except Exception as e:  # noqa: BLE001
                # 自癒失效 resume(鏡像 Cindy):CLI 回「No conversation found
                # with session ID」→ 丟掉持久化 id、全新重建,**只重放當前
                # 輸入一次**;再失敗就是真錯,照常亮錯誤卡。
                if attempt == 0 and self._is_invalid_resume(e):
                    _log("cc2_resume_invalid", session=self.name,
                         dropped=self.sdk_session_id)
                    self.sdk_session_id = ""
                    self.registry.save()
                    await self._teardown_client()
                    continue
                raise

    @staticmethod
    def _is_invalid_resume(e: BaseException) -> bool:
        return "no conversation found" in str(e).lower()

    # ── client 生命週期 ──

    async def _ensure_client(self):
        if self.client is not None:
            return self.client
        sdk = _sdk()
        opts = sdk.ClaudeAgentOptions(
            cwd=self.workdir,
            permission_mode=self.permission_mode,
            include_partial_messages=True,
            resume=self.sdk_session_id or None,
            can_use_tool=self._can_use_tool,
        )
        client = sdk.ClaudeSDKClient(options=opts)
        await client.connect()
        self.client = client
        _log("cc2_client_connected", session=self.name,
             resume=bool(self.sdk_session_id), workdir=self.workdir,
             permission_mode=self.permission_mode)
        return client

    async def _teardown_client(self) -> None:
        client, self.client = self.client, None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as e:  # noqa: BLE001
            _log("cc2_disconnect_failed", session=self.name,
                 error=type(e).__name__)

    async def interrupt(self) -> bool:
        """停止進行中 turn。回 False = 沒有活躍 turn(呼叫端回 409)。"""
        if not self.busy or self.client is None:
            return False
        try:
            await self.client.interrupt()
        except Exception as e:  # noqa: BLE001
            # interrupt 送不出去多半 = client 已死;turn task 會自己吃到
            # 例外走 teardown 收尾,這裡不重複收。
            _log("cc2_interrupt_failed", session=self.name,
                 error=type(e).__name__, error_message=str(e)[:160])
        return True

    # ── SDK 訊息 → 卡片流(契約翻譯層)──

    def _note_sdk_session(self, sid) -> None:
        sid = str(sid or "")
        if sid and sid != self.sdk_session_id:
            self.sdk_session_id = sid
            self.registry.save()
            self._upsert_resume_hint_card()

    def _upsert_resume_hint_card(self) -> None:
        """終端接手指令卡(2026-08-16 老闆點的):cc2 沒有畫面可 attach,
        接手 = 桌面跑 `claude --resume <sid>` 開同一條對話。固定卡 id →
        不洗版;sdk session id 換了(自癒重建)原位更新成新指令。"""
        if not self.sdk_session_id:
            return
        try:
            txt = (f"📎 終端接手:`claude --resume {self.sdk_session_id}`"
                   f"(等它閒著時;cwd {self.workdir})")
            self.digest.store.upsert_card(carddigest.make_card(
                f"card-cc2-resume-{self.name}", "", "assistant", "text",
                {"text": txt, "fallback_text": txt, "origin": "cc2.resume_hint"}))
        except Exception as _exc:  # noqa: BLE001
            _log("cc2_resume_hint_card_failed", session=self.name,
                 error=type(_exc).__name__, error_message=str(_exc)[:160])

    def _handle_message(self, msg) -> bool:
        """一則 SDK 訊息 → 卡片/turn/status 事件。回 True = turn 已收尾。"""
        sdk = _sdk()
        self._note_sdk_session(getattr(msg, "session_id", None))
        if isinstance(msg, sdk.StreamEvent):
            ev = msg.event if isinstance(msg.event, dict) else {}
            if ev.get("type") == "content_block_delta":
                delta = ev.get("delta") or {}
                if delta.get("type") == "text_delta":
                    self.digest.text_delta(str(delta.get("text") or ""))
            return False
        if isinstance(msg, sdk.AssistantMessage):
            texts, tools = [], []
            for block in msg.content or []:
                if isinstance(block, sdk.TextBlock):
                    texts.append(block.text)
                elif isinstance(block, sdk.ToolUseBlock):
                    tools.append(block)
            # 先收文字 draft(同卡 final:true),再出工具卡 —— 順序與模型
            # 產出一致,app 端才不會「工具卡插在半句話中間」。
            self.digest.assistant_final("".join(texts))
            for block in tools:
                self.digest.tool_card(block.name,
                                      _tool_summary(block.name, block.input))
            return False
        if isinstance(msg, sdk.ResultMessage):
            err = ""
            if getattr(msg, "is_error", False):
                err = str(getattr(msg, "result", "") or "") \
                    or f"turn failed ({getattr(msg, 'subtype', '')})"
            self.digest.turn_end(error=err)
            _log("cc2_turn_end", session=self.name, is_error=bool(err),
                 sdk_session=self.sdk_session_id[:12])
            return True
        if isinstance(msg, sdk.SystemMessage):
            data = getattr(msg, "data", None)
            if isinstance(data, dict):
                self._note_sdk_session(data.get("session_id"))
        return False

    # ── 審批(can_use_tool → Pocket 審核中心)──

    async def _can_use_tool(self, tool: str, tool_input: dict, _ctx):
        """SDK 的結構化審批回呼。建 pending record(卡片 + DB 鏡像)→ 等
        `/app/v1/approvals/{id}/decision` 叫醒 → 逾時/無決議一律 DENY
        (fail-closed,鏡像 Cindy:絕不掛起、絕不默許)。"""
        sdk = _sdk()
        if tool in self.session_allowed_tools:
            # 「本次全允許」記憶命中:同 session 同工具直接放行,不出卡
            # 不落 DB(記憶體記號,bridge 重啟/重建即清空 → 會重新問)。
            _log("cc2_approval_auto_allow", session=self.name, tool=tool)
            return sdk.PermissionResultAllow()
        aid = "cc2-" + uuid.uuid4().hex[:12]
        title = str(getattr(_ctx, "title", "") or
                    f"Claude Code 要使用 {tool}")[:200]
        now = time.time()
        record = {
            "id": aid, "title": title, "source": f"cc2:{self.name}",
            "risk": "medium", "detail": _tool_summary(tool, tool_input),
            "created_at": now, "expires_at": now + approval_timeout() + 5.0,
            "status": "pending", "decided_at": None, "result": None,
            "session_id": f"cc2:{self.name}", "provider": "cc2",
            "kind": "permission",
            # 三態選項(鏡像 codex _handle_approval_request 的宣告形狀,app
            # 零改動):第三顆 key=approve_for_session → 舊 App(只認
            # approve/deny)安全退成一般允許,不會誤送拒絕;style=secondary
            # (較軟的允許)。
            "options": [{"key": "approve", "label": "允許", "style": "primary"},
                        {"key": "approve_for_session", "label": "本次全允許",
                         "style": "secondary"},
                        {"key": "deny", "label": "拒絕", "style": "danger"}],
        }
        waiter = {"event": asyncio.Event(), "decision": None,
                  "record": record, "session": self.name}
        self.registry.pending_approvals[aid] = waiter
        self.digest.handle_approval(record)
        _db_hook(APPROVAL_DB_UPSERT, record)
        _log("cc2_approval_pending", session=self.name, approval_id=aid,
             tool=tool)
        try:
            await asyncio.wait_for(waiter["event"].wait(),
                                   timeout=approval_timeout())
        except asyncio.TimeoutError:
            pass
        finally:
            self.registry.pending_approvals.pop(aid, None)
        decision = waiter["decision"]
        approved = decision in ("approve", "approve_for_session")
        if decision == "approve_for_session":
            self.session_allowed_tools.add(tool)
        status = "approved" if approved else (
            "denied" if decision else "expired")
        self.digest.resolve_approval(record, status)
        _db_hook(APPROVAL_DB_MARK, aid, status)
        _log("cc2_approval_decided", session=self.name, approval_id=aid,
             tool=tool, status=status)
        if approved:
            return sdk.PermissionResultAllow()
        return sdk.PermissionResultDeny(
            message="Pocket 未核准(逾時或拒絕),fail-closed 拒絕執行")


# ── 註冊表(name → session;name→{sdk_session_id,workdir,permission_mode}
#    落 CC_SDK_STATE JSON,bridge 重啟後可 resume)──────────────────────────

class CCSdkRegistry:
    def __init__(self):
        self.sessions: dict[str, CCSdkSession] = {}
        self.pending_approvals: dict[str, dict] = {}   # aid → waiter
        self._loaded = False

    # ── 持久化 ──

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(state_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:  # noqa: BLE001
            # 狀態檔壞掉 = 丟 resume 資訊重來,不准擋住 provider 起步。
            _log("cc2_state_load_failed", error=type(e).__name__,
                 error_message=str(e)[:200])
            return
        for name, row in (data.get("sessions") or {}).items():
            if not isinstance(row, dict):
                continue
            self.sessions[name] = CCSdkSession(
                self, name,
                workdir=str(row.get("workdir") or ""),
                permission_mode=str(row.get("permission_mode") or ""),
                sdk_session_id=str(row.get("sdk_session_id") or ""))

    def save(self) -> None:
        """原子落地(tmp+rename);失敗只記 log —— resume id 掉了下次全新
        重建即可,不值得為它炸掉 turn。"""
        try:
            path = state_path()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            payload = {"sessions": {
                s.name: {"sdk_session_id": s.sdk_session_id,
                         "workdir": s.workdir,
                         "permission_mode": s.permission_mode}
                for s in self.sessions.values()}}
            tmp = f"{path}.tmp-{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            _log("cc2_state_save_failed", error=type(e).__name__,
                 error_message=str(e)[:200])

    # ── session CRUD ──

    def get(self, name: str) -> CCSdkSession | None:
        self.load()
        return self.sessions.get(name)

    def ensure(self, name: str, workdir: str = "",
               permission_mode: str = "") -> CCSdkSession:
        """建立或更新(冪等):同名重 POST 只更新 workdir/permission_mode,
        不打斷進行中的 client/turn。"""
        self.load()
        sess = self.sessions.get(name)
        if sess is None:
            sess = self.sessions[name] = CCSdkSession(
                self, name, workdir=workdir, permission_mode=permission_mode)
            _log("cc2_session_created", session=name, workdir=sess.workdir,
                 permission_mode=sess.permission_mode)
        else:
            if workdir:
                sess.workdir = os.path.expanduser(workdir)
            if permission_mode:
                sess.permission_mode = _clamp_mode(permission_mode)
        self.save()
        return sess

    # ── 審批決議(bridge 統一決議路由的 cc2 分支呼叫)──

    def decide_approval(self, aid: str, key: str) -> str | None:
        """叫醒等待者。回 status;None = 等待者不存在(已決/逾時/bridge
        重啟過)→ 呼叫端回 409。DB/卡片收尾由等待者協程統一做(單一收尾
        點,決議與逾時同一條路)。"""
        waiter = self.pending_approvals.get(aid)
        if waiter is None or waiter["decision"] is not None:
            return None
        # 三態:approve_for_session 對等待者是獨立決議值(等待者藉此記下
        # session_allowed_tools);對外(DB/卡片/回應)仍是 approved ——
        # 狀態字彙不擴,app 零改動。
        decision = ("deny" if key == "deny" else
                    "approve_for_session" if key == "approve_for_session" else
                    "approve")
        waiter["decision"] = decision
        waiter["event"].set()
        return "denied" if decision == "deny" else "approved"

    def pending_for(self, name: str) -> dict | None:
        """該 session 最早一筆 pending 審批(v2 sessions 列 waiting_approval)。"""
        rows = [w["record"] for w in self.pending_approvals.values()
                if w["session"] == name and w["decision"] is None]
        rows.sort(key=lambda r: r.get("created_at") or 0)
        return rows[0] if rows else None

    # ── v2 sessions 列表 ──

    def v2_rows(self) -> list[dict]:
        self.load()
        out = []
        for name in sorted(self.sessions):
            sess = self.sessions[name]
            pend = self.pending_for(name)
            status = "waiting_approval" if pend else (
                "running" if sess.busy else "idle")
            meta = {"permission_mode": sess.permission_mode}
            if pend:
                meta["approval"] = pend
            if sess.sdk_session_id:
                # 終端接手指令(app 未來可直接顯示/複製;對話裡另有同款卡)
                meta["resume_command"] = \
                    f"claude --resume {sess.sdk_session_id}"
            out.append({"id": f"cc2:{name}", "provider": "cc2", "title": name,
                        "subtitle": sess.workdir, "status": status,
                        "last_event_at": None,
                        "capabilities": ["input", "interrupt", "replay",
                                         "follow"] + (["approve"] if pend else []),
                        "meta": meta})
        return out


_REG: CCSdkRegistry | None = None


def registry() -> CCSdkRegistry:
    global _REG
    if _REG is None:
        _REG = CCSdkRegistry()
        _REG.load()
    return _REG


def reset_registry_for_tests() -> None:
    """單測隔離用:丟掉 in-memory 註冊表(下一次 registry() 重讀 CC_SDK_STATE)。"""
    global _REG
    _REG = None
