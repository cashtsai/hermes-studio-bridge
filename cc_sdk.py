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
TURN_END_PUSH = None       # callable(session_name, summary) — 回合完成推播
MEDIA_SINK = None          # callable(session_id) -> sink(mime, b64) -> dict|None


def configure(log=None, approval_db_upsert=None, approval_db_mark=None,
              turn_end_push=None, media_sink=None) -> None:
    """bridge 開機時接線。掛鉤保存在模組層,session 物件不各自持有。"""
    global LOG, APPROVAL_DB_UPSERT, APPROVAL_DB_MARK, TURN_END_PUSH, MEDIA_SINK
    if media_sink is not None:
        MEDIA_SINK = media_sink
    if log is not None:
        LOG = log
    if approval_db_upsert is not None:
        APPROVAL_DB_UPSERT = approval_db_upsert
    if approval_db_mark is not None:
        APPROVAL_DB_MARK = approval_db_mark
    if turn_end_push is not None:
        TURN_END_PUSH = turn_end_push


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


def context_usage_timeout() -> float:
    """問上下文用量的上限秒數。小而硬 —— 這支跑在回合收尾路徑上。"""
    try:
        return float(os.environ.get("CC_SDK_CONTEXT_TIMEOUT", "") or 8.0)
    except ValueError:
        return 8.0


def history_seed_limit() -> int:
    """歷史種子上限(0 = 關閉):session 重建時從 SDK transcript jsonl 撈
    最後 N 則 user/assistant 文字訊息餵進卡片庫,先於一切 live 事件。"""
    try:
        return int(os.environ.get("CC_SDK_HISTORY_SEED", "") or 40)
    except ValueError:
        return 40


def _project_key(workdir: str) -> str:
    """workdir → `~/.claude/projects/` 底下的目錄名。

    **必須**用 SDK 官方演算法:它會 realpath(解符號連結)、NFC 正規化、把所有
    非英數字元換成 `-`,超長還會加雜湊後綴。原本這裡寫 `workdir.replace("/","-")`,
    路徑一旦含點號、經過符號連結、或結尾有斜線就**算錯目錄**:
      /tmp/foo            → 現行 -tmp-foo        官方 -private-tmp-foo
      /Users/<you>/apps/a.b → 現行 -Users-<you>-apps-a.b 官方 -Users-<you>-apps-a-b
    算錯的後果是 `_seed_history` 找不到檔案後**靜默 return** —— 歷史整段不見,
    而且沒有任何錯誤訊息(2026-10-29 稽核發現)。
    SDK 缺席時退回舊寫法,至少不比修之前差。"""
    try:
        from claude_agent_sdk import project_key_for_directory
        return project_key_for_directory(os.path.expanduser(workdir or "~"))
    except Exception:  # noqa: BLE001
        return os.path.expanduser(workdir or "~").replace("/", "-")


def _live_session_ids() -> set:
    """目前**正被別人持有**的 session id(終端機開著的 claude、別的 bridge…)。

    CLI 在 `~/.claude/sessions/<pid>.json` 維護活著的持有者登記。沒有任何
    檔案鎖存在(SDK 與 CLI 都沒有),兩個寫入者同時接同一條 session 會讓
    transcript 的父子鏈分岔 —— 所以「不要去接已經有人拿著的」是唯一的防線。
    讀不到就回空集合(寧可不擋,也不要因為讀檔失敗就癱瘓續接)。
    """
    out = set()
    try:
        import glob
        for f in glob.glob(os.path.expanduser("~/.claude/sessions/*.json")):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    row = json.load(fh)
            except Exception:  # noqa: BLE001
                continue
            pid, sid = row.get("pid"), str(row.get("sessionId") or "")
            if not sid:
                continue
            try:                      # pid 還活著才算佔用(殘留檔不擋路)
                os.kill(int(pid), 0)
            except Exception:  # noqa: BLE001
                continue
            out.add(sid)
    except Exception:  # noqa: BLE001
        return set()
    return out


def newest_session_for(workdir: str) -> str:
    """這個工作目錄最近一條、**且沒人正在用**的 session id。

    對應 tmux 那邊 `ccsess` 的「--continue 按目錄接回上一段對話」——使用者不必
    記 session id。SDK 的 `continue_conversation=True` 不適用:它不告訴你挑中
    哪一條,bridge 就無法把 id 記進 state、下次也接不回同一條。
    改成自己解析:`list_sessions` 已按最後修改時間排序,挑第一個沒被佔用的。
    """
    try:
        from claude_agent_sdk import list_sessions
        rows = list_sessions(directory=os.path.expanduser(workdir or "~"),
                             limit=8, include_worktrees=False)
    except Exception as e:  # noqa: BLE001
        _log("cc2_list_sessions_failed", error=str(e)[:160])
        return ""
    live = _live_session_ids()
    for r in rows or []:
        sid = str(getattr(r, "session_id", "") or "")
        if sid and sid not in live:
            return sid
    return ""


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


# SDK client.set_permission_mode 的 docstring 列了六種;原本這裡只收四種,
# 多的兩種會被 _clamp_mode 悄悄吞成 acceptEdits —— 使用者選了 dontAsk 卻拿到
# 自動接受編輯,是「比沒做還糟」的那種錯(2026-10-29 稽核補)。
PERMISSION_MODES = ("acceptEdits", "bypassPermissions", "default", "plan",
                    "dontAsk", "auto")

# 建立 session 時可釘住的選項。刻意與 tmux 的 spawn pin 正典同一組
# (`~/.config/ccsess/spawn/<name>.json`:model/effort/permission_mode/
# max_budget_usd/fallback_model/append_system_prompt + BYO api_key),
# 這樣同一份 launch config 兩條路徑語意一致。
# `append_system_prompt` 不是 SDK 欄位,是我們的糖 —— 轉成官方 preset+append
# 形狀(見 _spawn_option_kwargs)。`api_key` 不走這裡(見 SPAWN_SECRET_KEYS)。
SPAWN_PIN_KEYS = ("effort", "fallback_model", "max_budget_usd",
                  "append_system_prompt", "disallowed_tools",
                  "add_dirs", "max_thinking_tokens")

# **刻意拒收**的鍵,附理由。靜默忽略不行 —— 使用者會以為設定生效了。
SPAWN_PIN_REFUSED = {
    # 2026-10-29:這顆是我上一批自己發明的(tmux 的 pin 正典裡沒有),而且
    # SDK 會直接警告:「can_use_tool will not be invoked for: <tools>. An
    # allowed_tools entry that allows a whole tool auto-approves it before
    # the callback is consulted.」—— 整條工具的 allow 會**靜默繞過 Pocket
    # 的審批閘**。cc2 的賣點之一就是 fail-closed 審批進審核中心,留一個
    # 能悄悄關掉它的旋鈕是不能接受的。要放寬權限請用 permission_mode。
    "allowed_tools": "allowed_tools 會繞過 Pocket 審批閘;要放寬請用 permission_mode",
    # setting_sources 會改變 CLAUDE.md / settings.json 的載入範圍 —— 那會
    # 連帶改掉專案規則與既有 allow 規則,影響面遠超過「啟動選項」。
    "setting_sources": "setting_sources 會改變 CLAUDE.md/settings 載入範圍,不開放遠端設定",
}

# BYO 憑證不落 state JSON(那是明文檔)。沿用 tmux 同一個 0600 secret 目錄,
# 所以同一把 key 設一次兩條路徑都吃得到。
SPAWN_SECRET_KEYS = ("api_key",)


SESSION_NAME_RE = r"[A-Za-z0-9_-]{1,64}"


def valid_session_name(name: str) -> bool:
    """session 名合法性。secret 檔路徑是 `join(secret_dir, name)`,name 若含
    `/` 或 `..` 就能寫到目錄外 —— 這裡是最後一道,不能只靠 HTTP 層驗。"""
    import re as _re
    return bool(_re.fullmatch(SESSION_NAME_RE, str(name or "")))


def _secret_dir() -> str:
    return os.path.expanduser(
        os.environ.get("CCSESS_SECRET_DIR", "") or "~/.config/ccsess/secret")


def write_spawn_secret(name: str, api_key: str) -> bool:
    """BYO ANTHROPIC_API_KEY → 0600 檔(host-local,永不外洩,不進 state)。
    空值 = 清掉。回傳是否寫了檔。"""
    if not valid_session_name(name):
        _log("cc2_spawn_secret_bad_name", session=str(name)[:40])
        return False
    d = _secret_dir()
    path = os.path.join(d, name)
    try:
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o700)
        if api_key:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, api_key.encode("utf-8"))
            finally:
                os.close(fd)
            return True
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:  # noqa: BLE001
        _log("cc2_spawn_secret_failed", session=name,
             error=type(e).__name__, error_message=str(e)[:160])
    return False


def move_spawn_secret(old: str, new: str) -> None:
    """改名時把 BYO key 一起搬過去。不搬的話改完名字就讀不到那把 key 了,
    而且**沒有任何錯誤** —— 下一次連線安靜退回預設憑證(D2)。"""
    if not (valid_session_name(old) and valid_session_name(new)):
        return
    d = _secret_dir()
    src, dst = os.path.join(d, old), os.path.join(d, new)
    try:
        if os.path.exists(src):
            os.replace(src, dst)
    except Exception as e:  # noqa: BLE001
        _log("cc2_spawn_secret_move_failed", old=old, new=new,
             error=type(e).__name__)


def delete_spawn_secret(name: str) -> None:
    """刪 session 時一併清掉 BYO key。留著的話之後同名的新 session 會**悄悄
    繼承**上一個人的憑證(D2)。"""
    if not valid_session_name(name):
        return
    try:
        path = os.path.join(_secret_dir(), name)
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:  # noqa: BLE001
        _log("cc2_spawn_secret_delete_failed", session=name,
             error=type(e).__name__)


def read_spawn_secret(name: str) -> str:
    """讀回 BYO key(只給 _ensure_client 用;**絕不**放進任何回應或 log)。"""
    if not valid_session_name(name):
        return ""
    try:
        with open(os.path.join(_secret_dir(), name), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:  # noqa: BLE001
        return ""


def refused_spawn_keys(raw) -> dict:
    """body 裡出現的「刻意拒收」鍵 → {鍵: 理由}。呼叫端要把這個回給使用者,
    不能靜默忽略 —— 靜默的話人會以為設定生效了。"""
    if not isinstance(raw, dict):
        return {}
    return {k: why for k, why in SPAWN_PIN_REFUSED.items() if k in raw}


def sanitize_spawn_opts(raw) -> dict:
    """外來 body → 可安全落地的 spawn pins。白名單外一律丟掉;型別不對也丟,
    不要讓壞值撐到 connect 當下才炸(那時錯誤會長得像「SDK 壞了」)。
    拒收清單裡的鍵另見 `refused_spawn_keys`(要回報,不是默默丟)。"""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k in SPAWN_PIN_KEYS:
        if k not in raw or raw[k] is None:
            continue
        v = raw[k]
        if k in ("allowed_tools", "disallowed_tools", "add_dirs",
                 "setting_sources"):
            if isinstance(v, (list, tuple)):
                items = [str(x) for x in v if str(x).strip()]
                if items:
                    out[k] = items
        elif k == "max_thinking_tokens":
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                pass
        elif k == "max_budget_usd":
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
        else:
            txt = str(v).strip()
            if txt:
                out[k] = txt
    return out


# tmux 那邊的模式字彙是 `normal`(見 bridge 的 _CC_MODES),SDK 用的是
# `default`。app 的模式選擇器送的是 tmux 字彙 —— 不收的話預設那一格在 cc2
# 上直接 400(2026-10-29 複查抓到)。雙向都認。
MODE_ALIASES = {"normal": "default", "manual": "default"}


def normalize_mode(mode: str) -> str:
    """外來字彙 → SDK 字彙。認不出來回空字串(呼叫端決定要 400 還是退預設)。"""
    m = str(mode or "").strip()
    m = MODE_ALIASES.get(m, m)
    return m if m in PERMISSION_MODES else ""


def _clamp_mode(mode: str) -> str:
    """退成安全預設。**會記 log** —— 原本靜默吞掉看不懂的值,使用者選了
    `dontAsk` 卻拿到 acceptEdits 時完全查不出為什麼(D14)。"""
    m = normalize_mode(mode)
    if m:
        return m
    if str(mode or "").strip():
        _log("cc2_permission_mode_unknown", requested=str(mode)[:40],
             fallback="acceptEdits")
    return "acceptEdits"


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
        self.busy_since: float = 0.0   # 回合起算(epoch);turn_begin 設、turn_end 清
        self.last_event_at: float = 0.0  # 最後活動(epoch);v2_rows 給 app 排序用
        self.ctx_tokens: int = 0       # 目前上下文佔用 token(0 = 還不知道)
        self.ctx_max: int = 0          # 這個模型的上下文上限
        self.ctx_pct: float = 0.0      # 佔用百分比(0–100)
        self.mode: str = ""            # 權限模式鏡像(CCSdkSession 同步進來)
        self.model: str = ""           # 模型鏡像(同上);空 = 跟隨 CLI 預設
        self.name = name
        self.store = carddigest.SessionCardStore()
        self.store.media_session_id = f"cc2:{name}"
        self.busy = False
        self.prompt = None          # pending approval title(mixin 會讀寫)
        self.queue_depth = 0
        self._seq = 0               # 卡 id 序號(單調遞增,replay 不撞號)
        self._draft_id = ""         # 進行中 assistant draft 卡 id
        self._draft_text = ""
        # tool_use_id → (卡 id, 工具名, 呼叫摘要)。工具結果回來時用它找回
        # 原本那張工具卡,把結果**回填到同一張**(不另開卡,手機上才不洗版)。
        self._tool_cards: dict = {}

    def _next_id(self, tag: str) -> str:
        self._seq += 1
        return f"card-cc2-{self.name}-{tag}{self._seq}"

    def _status(self, label: str = ""):
        self.store.set_status({
            # busy_since:app 的 WorkingBar 靠它顯示「已 N 秒」—— 沒有的話
            # 2 秒與 30 分鐘長得一模一樣(tmux 那邊早就有,cc2 漏了)。
            # mode:權限模式,app 的模式選擇器讀這個;原本硬寫 None → 畫面空白。
            # mode/model 由 CCSdkSession 在送出前注入(digest 不持有它們)。
            "busy": self.busy, "mode": self.mode or None,
            "busy_since": self.busy_since or None,
            "model": self.model or None,
            # context:tmux 那邊早有(_cc_context_usage_cached),cc2 原本整個
            # 沒有 —— 長對話逼近上限時使用者完全看不到,只會莫名被壓縮。
            # 刻意**不**放累計花費:機主 2026-10-28 明確要求拿掉費用預估
            # (「那個數字很不精準常常看的很奇怪」)。
            "context": ({"used": self.ctx_tokens, "size": self.ctx_max,
                         "pct": round(self.ctx_pct, 1)}
                        if self.ctx_max else None),
            "prompt": self.prompt,
            # blocked:對齊 tmux 的同名欄位(2026-08-18「隱形卡住」事故之後
            # 加的)。cc2 卡住的形態只有一種 —— 有審批/提問在等人回答,
            # 回合就停在那裡。欄位名刻意與 tmux 一致,app 不用分 provider。
            "blocked": bool(self.prompt),
            "phase": "run" if self.busy else "idle",
            "queue_depth": self.queue_depth,
            "label": label or ("等待核准" if self.prompt else
                               ("回覆中" if self.busy else "待命")),
        })

    def turn_begin(self, turn_id: str):
        self.busy = True
        self.busy_since = time.time()
        self.last_event_at = self.busy_since
        self.store.turn_id = turn_id
        self.store.saw_output = False
        self.store.last_tool = ""
        self.store.push_turn("begin", turn_id)
        self._status("已送出,等待回覆。")

    def last_assistant_text(self) -> str:
        """最後一則**真的回覆**的純文字(推播摘要用)。

        只認 origin=cc2.assistant。不能只看 role=="assistant" —— 工具卡
        (「🔧 Bash: …」)和終端接手提示卡也都是 assistant,一個沒出文字、
        只跑了工具的回合會把「🔧 Bash: git status」或「📎 終端接手:
        claude --resume …」當成推播內容推到手機上。
        歷史種子卡(origin 空)也排除:那是上一輪的話,不是這一輪的結果。
        """
        for c in reversed(list(self.store.cards.values())):
            body = c.get("body") or {}
            if body.get("origin") != "cc2.assistant":
                continue
            t = str(body.get("text") or "").strip()
            if t:
                return t
        return ""

    def user_card(self, text: str, attachments: list | None = None,
                  client_id: str = ""):
        """使用者送出的回顯卡。attachments 帶 on-disk path(bridge 存好的),
        app 才顯示得出縮圖;client_id 讓 app 的待送泡泡對得上這張卡。"""
        body = {"text": text, "fallback_text": text}
        if attachments:
            body["attachments"] = [dict(a) for a in attachments if isinstance(a, dict)]
        if client_id:
            body["client_id"] = str(client_id)
        self.store.upsert_card(carddigest.make_card(
            self._next_id("u"), self.store.turn_id, "user", "text", body))

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
        self.last_event_at = time.time()
        self.store.saw_output = True
        self.store.last_tool = ""
        self.store.upsert_card(carddigest.make_card(
            self._draft_id, self.store.turn_id, "assistant", "markdown",
            {"text": self._draft_text, "fallback_text": self._draft_text,
             "origin": "cc2.assistant"},
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
                {"text": text, "fallback_text": text,
                 "origin": "cc2.assistant"}))
        self._draft_id = ""
        self._draft_text = ""

    def media_card(self, items: list, tool: str = ""):
        """工具回傳的圖 → 一張 attachment 卡(與 CC/CX/OpenClaw 同款形狀)。
        cc2 原本完全不處理工具結果,所以截圖/MCP 直回圖一律落空 ——
        `/app/v2/sessions/cc2:x/media` 永遠是空的(2026-10-29 稽核)。"""
        items = [i for i in (items or []) if isinstance(i, dict)]
        if not items:
            return
        label = f"🖼 {tool} 回傳 {len(items)} 張圖" if tool else \
            f"🖼 回傳 {len(items)} 張圖"
        self.store.upsert_card(carddigest.make_card(
            self._next_id("img"), self.store.turn_id, "assistant", "text",
            {"text": label, "fallback_text": label,
             "origin": "cc2.media", "attachments": items}))
        self.store.saw_output = True
        self.last_event_at = time.time()

    def tool_card(self, tool: str, summary: str, use_id: str = ""):
        text = f"🔧 {tool}: {summary}" if summary else f"🔧 {tool}"
        cid = self._next_id("t")
        self.store.upsert_card(carddigest.make_card(
            cid, self.store.turn_id, "assistant", "text",
            {"text": text, "fallback_text": text, "origin": "cc2.tool"}))
        if use_id:
            # 第四格 = 是否已回填過真輸出(D6:避免被後到的空結果蓋掉)
            self._tool_cards[str(use_id)] = (cid, tool, summary, False)
        self.store.last_tool = tool
        self._status(f"執行 {tool}")

    def tool_result(self, use_id: str, result: str, is_error: bool = False):
        """工具結果回填到原本那張工具卡。

        為什麼要有這個:cc2 原本只出「🔧 Bash: git status」,**結果從來不顯示**
        —— 使用者看得到它跑了什麼,看不到跑出什麼。tmux 路徑在 pane 裡本來就
        看得到(那是它唯一的優點之一)。2026-10-29 補。
        結果可能很長(編譯輸出動輒上萬字),截斷並明說截了 —— 手機上塞全文
        不但沒用,還會把卡片流撐爆。
        """
        row = self._tool_cards.get(str(use_id))
        if not row:
            return
        cid, tool, summary, filled = row
        body = (result or "").strip()
        if not body and filled:
            # D6:同一個 tool_use_id 可能回來兩次(例如先文字、再純圖的那則)。
            # 第二次是空的就**不要蓋掉**已經收到的真輸出 —— 否則使用者剛看到
            # 編譯輸出,下一瞬間變成「(沒有輸出)」。
            return
        head = f"🔧 {tool}: {summary}" if summary else f"🔧 {tool}"
        if not body:
            body = "(沒有輸出)"
        self._tool_cards[str(use_id)] = (cid, tool, summary, True)
        limit = 800
        if len(body) > limit:
            body = body[:limit] + f"\n…(還有 {len(result.strip()) - limit} 字,"\
                                  "完整輸出在終端機接手時看得到)"
        mark = "⚠️ 失敗" if is_error else "↳"
        text = f"{head}\n{mark} {body}"
        self.store.upsert_card(carddigest.make_card(
            cid, self.store.turn_id, "assistant", "text",
            {"text": text, "fallback_text": text, "origin": "cc2.tool"}))
        self.last_event_at = time.time()

    def error_card(self, text: str):
        self.store.upsert_card(carddigest.make_card(
            self._next_id("e"), self.store.turn_id, "system", "text",
            {"text": f"⚠️ {text}", "fallback_text": f"⚠️ {text}"}))

    def turn_end(self, error: str = ""):
        self.busy = False
        self.busy_since = 0.0            # 回合結束 → 計時器收掉,不跨回合累加
        self.last_event_at = time.time()
        if error:
            self.error_card(error)
        self.store.push_turn("end", self.store.turn_id)
        self.store.turn_id = ""
        self.store.last_tool = ""
        self._draft_id = ""
        self._draft_text = ""
        self._tool_cards.clear()   # 不跨回合累積(長 session 會無上限長大)
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
                 sdk_session_id: str = "", model: str = ""):
        self.registry = registry
        self.name = name
        self.workdir = os.path.expanduser(workdir or "~")
        self.permission_mode = _clamp_mode(permission_mode or "acceptEdits")
        self.sdk_session_id = sdk_session_id or ""
        self.model: str = (model or "").strip()   # 空 = 跟隨 CLI 預設;傳給 ClaudeAgentOptions
        # 建立時可釘的其他 SDK 選項(tmux 那邊有一整個 pin 目錄,cc2 原本
        # 只有 workdir/permission_mode)。只收白名單內的鍵 —— 這份 dict 會
        # 落進 state 檔,不能讓任意鍵直通 ClaudeAgentOptions 建構子。
        self.spawn_opts: dict = {}
        # 自癒(invalid resume)換掉的舊 sdk_session_id。保留是為了歷史翻頁 ——
        # 換 id 之後舊那份 transcript 還在磁碟上,但沒人記得它屬於這條 session,
        # 於是 history_page 永遠撈不到、重啟後更是徹底失聯(2026-10-29 稽核)。
        self.prior_session_ids: list[str] = []
        self.in_flight_prompt: str = ""  # 正在跑的那則(重啟後用來說明中斷)
        self.archived: bool = False      # 封存 = 不列出來,對話不刪
        self.digest = CC2Digest(name)
        # digest 自己不知道權限模式/模型,但 app 的狀態列要顯示 —— 同步鏡像進去。
        self.digest.mode = self.permission_mode
        self.client = None
        self.queue: list[str] = []       # 忙碌時的輸入佇列(turn end 後 drain)
        self.busy = False
        self._task: asyncio.Task | None = None
        # approve_for_session 記憶:此 session 已「本次全允許」的工具名。
        # 純 in-memory —— bridge 重啟/session 重建即清空,之後第一次仍會
        # 出卡再問(fail-safe:寧可多問,不留跨進程的隱形放行)。
        self.session_allowed_tools: set[str] = set()
        self._seed_history()

    def history_page(self, limit: int = 40, offset: int = 0) -> dict:
        """歷史分頁(對應 tmux 的 `/ccsessions/{name}/history`)。

        `_seed_history` 只放最近 N 則進卡片流,再往前就沒有了 —— 長對話等於
        翻不回去。這支直接讀 transcript jsonl,給 app 往前捲:offset 從**最新**
        往回數,回傳 `more` 告訴它還有沒有更早的。
        """
        out = {"messages": [], "more": False, "total": 0}
        # 跨自癒讀:自癒換掉 sdk_session_id 之後,舊那份 transcript 還在磁碟上。
        # 只讀當前 id 的話,換 id 之前的對話永遠翻不回來。按時間順序(舊→新)
        # 串起來,舊的在前。
        ids = [i for i in (self.prior_session_ids or []) if i]
        if self.sdk_session_id:
            ids.append(self.sdk_session_id)
        if not ids:
            return out
        proj = os.path.join(_claude_projects_root(), _project_key(self.workdir))
        rows = []
        truncated = False
        # 每檔讀取上限。transcript 長起來是數十 MB,整檔讀進來(而且這支由
        # async 路由呼叫)會把事件圈凍住 —— 所有 HTTP、所有 SSE 一起卡
        # (2026-10-29 盤查)。只讀尾端,翻到底就誠實說還有更早的。
        cap = 1 << 23          # 8 MiB/檔
        for sid in ids:
            path = os.path.join(proj, f"{sid}.jsonl")
            try:
                with open(path, "rb") as fb:
                    size = os.fstat(fb.fileno()).st_size
                    if size > cap:
                        fb.seek(size - cap)
                        fb.readline()      # 丟掉被切半的那一行
                        truncated = True
                    raw = fb.read()
                lines = raw.decode("utf-8", "replace").splitlines()
            except FileNotFoundError:
                continue
            except Exception as e:  # noqa: BLE001
                _log("cc2_history_page_failed", session=self.name,
                     sdk_session=sid[:12], error=str(e)[:160])
                continue
            for ln in lines:
                try:
                    d = json.loads(ln)    # _seed_row 收的是解析後的 dict
                except Exception:  # noqa: BLE001
                    continue
                row = _seed_row(d)
                if row:
                    rows.append({"role": row[0], "text": row[1], "ts": row[2]})
        out["total"] = len(rows)
        limit = max(0, int(limit or 0))
        if offset:
            # offset 從最新往回數;超過總數 → 空頁(不是繞回去拿最舊那幾則)。
            rows = rows[:max(0, len(rows) - max(0, int(offset)))]
        page = rows[-limit:] if (rows and limit) else []
        # truncated = 我們只讀了尾端,前面還有讀不到的 → more 必為真,
        # total 也只是「讀得到的範圍內」的數量,不假裝是全部。
        out["more"] = (len(rows) > len(page)) or truncated
        out["truncated"] = truncated
        out["messages"] = page
        return out

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
                            _project_key(self.workdir),
                            f"{self.sdk_session_id}.jsonl")
        try:
            # 只讀尾端。整檔 readlines 不行:transcript 長起來是數十 MB,而這支
            # 在 `__init__` 裡、由 `load()` 在請求/開機路徑上呼叫 —— 一條長對話
            # 就能讓那個請求卡住(複查抓到)。種子只要最後 N 則,尾端夠了。
            tail_bytes = 1 << 20          # 1 MiB,遠大於 40 則對話
            with open(path, "rb") as fb:
                size = os.fstat(fb.fileno()).st_size
                if size > tail_bytes:
                    fb.seek(size - tail_bytes)
                    fb.readline()         # 丟掉被切半的那一行
                raw = fb.read()
            lines = raw.decode("utf-8", "replace").splitlines()
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

    async def submit(self, text: str, attachments: list | None = None,
                     client_id: str = "") -> dict:
        """統一輸入入口(v2_session_input 的 cc2 分支):忙碌 → 排隊不回 4xx
        (與 CX 同語意,app 靠 `queued` 欄位畫「已排入佇列」)。"""
        if self.busy or (self._task is not None and not self._task.done()):
            self.queue.append((text, attachments or [], client_id))
            self.digest.queue_depth = len(self.queue)
            self.digest._status()
            _log("cc2_input_queued", session=self.name, depth=len(self.queue))
            return {"delivery": "queued", "queued": True,
                    "queue_depth": len(self.queue)}
        self._start_turn(text, attachments=attachments, client_id=client_id)
        return {"delivery": "accepted", "queued": False}

    def _start_turn(self, text: str, attachments: list | None = None,
                    client_id: str = "") -> None:
        self.busy = True
        self._task = asyncio.get_event_loop().create_task(
            self._run_turn_safe(text, attachments or [], client_id))

    async def _run_turn_safe(self, text: str, attachments: list | None = None,
                             client_id: str = "") -> None:
        """turn 外殼:任何例外都收斂成錯誤卡 + idle,**絕不**讓 provider
        例外往上炸掉 dispatch/通知路徑(bridge 全域慣例)。"""
        try:
            await self._run_turn(text, attachments or [], client_id)
        except asyncio.CancelledError:
            self.digest.turn_end()
            raise
        except Exception as e:  # noqa: BLE001
            _log("cc2_turn_error", session=self.name, error=type(e).__name__,
                 error_message=str(e)[:200])
            await self._teardown_client()
            # 認證/額度類錯誤給人看得懂的指路,不要丟 SDK 例外字串。
            hint = self._auth_hint(e)
            self.digest.turn_end(
                error=hint or f"{type(e).__name__}: {str(e)[:200]}")
        finally:
            # 回合收尾刷一次上下文用量 —— 放這裡而不是每個 delta,因為它是一次
            # SDK 來回;每個 token 都問會把串流拖垮。
            try:
                await self.refresh_context_usage()
            except BaseException:  # noqa: BLE001
                # 連 CancelledError 一起吞:這段跑在 finally 裡,往外丟的話
                # 下面的 busy=False / queue drain 全都不會執行,session 就
                # 永遠停在「回覆中」(D7)。這個數字不值得那個風險。
                pass
            # 回合完成推播 —— tmux 有、cc2 原本完全沒有,背景跑完使用者不會知道。
            # 掛鉤由 bridge 注入(cc_sdk 不 import bridge);fail-safe:推播壞掉
            # 絕不影響回合收尾。
            if TURN_END_PUSH is not None and not self.archived:
                try:
                    TURN_END_PUSH(self.name, self.digest.last_assistant_text())
                except Exception as e:  # noqa: BLE001
                    _log("cc2_turn_end_push_failed", session=self.name,
                         error=str(e)[:160])
            self.busy = False
            self.in_flight_prompt = ""
            self._task = None
            self.registry.save()
            if self.queue:                      # turn 結束 → 放行下一則
                nxt = self.queue.pop(0)
                self.digest.queue_depth = len(self.queue)
                # 佇列元素是 (text, attachments, client_id);舊格式(純字串)
                # 也吃得下 —— 升級當下若佇列裡還有舊項目不會炸。
                if isinstance(nxt, tuple):
                    self._start_turn(nxt[0], attachments=nxt[1],
                                     client_id=nxt[2] if len(nxt) > 2 else "")
                else:
                    self._start_turn(nxt)

    async def _run_turn(self, text: str, attachments: list | None = None,
                        client_id: str = "") -> None:
        self.in_flight_prompt = text
        turn_id = f"turn-cc2-{uuid.uuid4().hex[:10]}"
        self.digest.turn_begin(turn_id)
        self.digest.user_card(text, attachments=attachments, client_id=client_id)
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
                    if self.sdk_session_id:
                        self.prior_session_ids.append(self.sdk_session_id)
                    self.sdk_session_id = ""
                    self.registry.save()
                    await self._teardown_client()
                    continue
                raise

    @staticmethod
    def _is_invalid_resume(e: BaseException) -> bool:
        return "no conversation found" in str(e).lower()

    @staticmethod
    def _auth_hint(e: BaseException) -> str:
        """認證類錯誤 → 人看得懂的指路。沒命中回空字串。

        tmux 那邊有 `POST /ccsessions/{name}/login`(靠 tmux 把 `/login` 打進
        TUI)。cc2 沒有 TUI,登入這件事做不到 —— 但**可以不要丟一串 SDK 例外**。
        原本完全沒處理:主機沒登入時卡片上只會出現一行英文堆疊訊息,使用者
        根本不知道要去登入(2026-10-29 盤查)。字樣取自 CLI 二進位裡的實際訊息。
        """
        low = str(e).lower()
        if "run /login" in low or "not authenticated" in low or \
                "please log in" in low:
            return ("這台主機還沒登入 Claude Code。請在終端機跑 `claude` 後輸入 "
                    "`/login`(或在 Pocket 的 CC 頁籤登入),登入後這條對話直接可用。")
        if "invalid api key" in low or "authentication_error" in low:
            return ("Claude 憑證無效。若這條 session 設了自備 API key,請重新設定;"
                    "否則在終端機跑 `claude` 並 `/login` 重新登入。")
        if "credit balance" in low or "quota" in low or "rate_limit" in low:
            return "Claude 額度或速率上限擋住了這一輪。等一下再送,或換個模型。"
        return ""

    # ── client 生命週期 ──

    async def _ensure_client(self):
        if self.client is not None:
            return self.client
        # 不去接**已經有人拿著**的 session。SDK 與 CLI 都沒有檔案鎖,兩個寫入者
        # 同時接同一條會讓 transcript 的父子鏈分岔(= 對話永久損壞)。
        # 使用者在終端機 `claude --resume` 接走時就是這個情況:寧可明說接不了,
        # 也不要安靜地把對話寫壞。
        if self.sdk_session_id and self.sdk_session_id in _live_session_ids():
            msg = ("這條對話正被另一個 claude 行程持有(例如你在終端機接走了)。"
                   "等它結束後再送,或改用新的 session。")
            self.digest.error_card(msg)
            _log("cc2_session_held_elsewhere", session=self.name,
                 sdk_session_id=self.sdk_session_id[:18])
            raise RuntimeError(msg)
        sdk = _sdk()
        opts_kwargs = self._spawn_option_kwargs()
        if self.model:
            opts_kwargs["model"] = self.model   # 原本完全沒傳 → cc2 無法切模型
        opts = sdk.ClaudeAgentOptions(
            cwd=self.workdir,
            permission_mode=self.permission_mode,
            **opts_kwargs,
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

    def _spawn_option_kwargs(self) -> dict:
        """spawn pins → ClaudeAgentOptions kwargs。未知鍵在 sanitize 時就擋掉了,
        這裡只做形狀轉換;轉換失敗寧可丟掉那一顆,不要讓 connect 整個失敗。"""
        out = {}
        key = read_spawn_secret(self.name)
        if key:
            # BYO 憑證:只傳這一把,**不要**整份灌 os.environ。
            # SDK 的 subprocess transport 本來就會繼承 os.environ,而且它刻意
            # 濾掉 `CLAUDECODE`、並設 `CLAUDE_CODE_ENTRYPOINT=sdk-py`;
            # `options.env` 是最後覆蓋的那層,整份灌進去會把 CLAUDECODE 灌回來、
            # 也會把 entrypoint 蓋成父行程的值。
            # 與 tmux 同語意(ccsess 也只是 `ANTHROPIC_API_KEY="$(cat 檔)"` 前綴)。
            out["env"] = {"ANTHROPIC_API_KEY": key}
        for k, v in (self.spawn_opts or {}).items():
            if k == "append_system_prompt":
                # 官方形狀:保留 claude_code 預設 system prompt,後面接我們的。
                # 直接塞 system_prompt 字串會**整個換掉**預設提示,那會讓
                # Claude Code 失去它原本的工具使用規範 —— 不是我們要的。
                out["system_prompt"] = {"type": "preset",
                                        "preset": "claude_code",
                                        "append": str(v)}
            else:
                out[k] = v
        return out

    async def apply_runtime_settings(self, model=None,
                                     permission_mode=None) -> dict:
        """改**活著**的 session 的模型/權限模式。

        為什麼需要獨立一條路:`ClaudeAgentOptions` 只在 connect 當下讀一次,
        所以重 POST create 改了欄位對已連線的 client 完全沒作用(改完畫面顯示
        新模式、實際仍用舊的 —— 比沒做還糟)。SDK 有 set_model /
        set_permission_mode 可以熱改,走它。
        client 還沒建:只記下來,下次 connect 自然生效。
        """
        changed = {}
        if permission_mode:
            want = _clamp_mode(str(permission_mode))
            if want != str(permission_mode):
                raise ValueError(
                    f"不認得的權限模式 {permission_mode!r};可用:"
                    + "/".join(PERMISSION_MODES))
            self.permission_mode = want
            self.digest.mode = want
            changed["permission_mode"] = want
        if model is not None:
            self.model = str(model).strip()
            self.digest.model = self.model
            changed["model"] = self.model or None
        applied_live = False
        if changed and self.client is not None:
            try:
                if "permission_mode" in changed:
                    await self.client.set_permission_mode(
                        changed["permission_mode"])
                if "model" in changed:
                    await self.client.set_model(self.model or None)
                applied_live = True
            except Exception as e:  # noqa: BLE001
                # 熱改失敗不是致命:值已經記下來,下次重連就是新的。誠實回報
                # applied_live=false,呼叫端才知道「這一輪還是舊設定」。
                _log("cc2_runtime_settings_live_failed", session=self.name,
                     error=type(e).__name__, error_message=str(e)[:160])
        self.digest._status()
        self.registry.save()
        _log("cc2_runtime_settings", session=self.name,
             applied_live=applied_live, **{k: str(v) for k, v in changed.items()})
        return {"ok": True, "changed": changed, "applied_live": applied_live,
                "note": ("" if applied_live or not changed else
                         "已記錄,下次重新連線時生效(目前沒有在線的 client)")}

    async def refresh_context_usage(self) -> dict:
        """問 SDK 現在的上下文佔用(等同 CLI 的 `/context`),寫進狀態。

        tmux 路徑一直有這個數字,cc2 整個沒有 —— 長對話逼近上限時使用者只會
        莫名被壓縮,完全看不到前兆。client 不在線就跳過(不為了這個數字去
        建連線,那會憑空開一個 claude 行程)。
        """
        if self.client is None:
            return {}
        try:
            # **必須有逾時**:這支跑在 `_run_turn_safe` 的 finally 裡。SDK 的
            # get_context_usage 是一次 CLI 來回、本身沒有逾時 —— client 若是
            # 半死(行程在、stdio 不回),沒逾時就會把回合收尾永遠卡住,
            # session 停在 busy、使用者再也送不出東西。這個數字是加值資訊,
            # 值不值得冒那個風險?不值得。
            usage = await asyncio.wait_for(self.client.get_context_usage(),
                                           timeout=context_usage_timeout())
        except asyncio.TimeoutError:
            _log("cc2_context_usage_timeout", session=self.name)
            return {}
        except Exception as e:  # noqa: BLE001
            _log("cc2_context_usage_failed", session=self.name,
                 error=type(e).__name__, error_message=str(e)[:160])
            return {}
        if not isinstance(usage, dict):
            return {}
        try:
            self.digest.ctx_tokens = int(usage.get("totalTokens") or 0)
            self.digest.ctx_max = int(usage.get("maxTokens") or 0)
            self.digest.ctx_pct = float(usage.get("percentage") or 0.0)
        except (TypeError, ValueError):
            return {}
        return {"used": self.digest.ctx_tokens, "size": self.digest.ctx_max,
                "pct": self.digest.ctx_pct}

    async def _teardown_client(self) -> None:
        client, self.client = self.client, None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as e:  # noqa: BLE001
            _log("cc2_disconnect_failed", session=self.name,
                 error=type(e).__name__)

    async def compact(self) -> dict:
        """壓縮對話(對應 tmux 的 `ccsess compress`)。

        SDK 沒有壓縮 API,但 `/compact` 是 CLI 的斜線指令 —— 當成一般訊息送進去
        即可,CLI 會自己處理並在 transcript 留下壓縮邊界。忙碌中不送(會排隊到
        下一輪,語意就不是「現在壓縮」了)。
        """
        if self.busy:
            return {"ok": False, "error": "busy",
                    "message": "這一輪還在跑;等它結束再壓縮"}
        await self.submit("/compact")
        _log("cc2_compact_requested", session=self.name)
        return {"ok": True, "message": "已送出壓縮指令"}

    async def interrupt(self) -> bool:
        """停止進行中 turn。回 False = 沒有活躍 turn(呼叫端回 409)。

        2026-10-29 修兩件事:
        ① **先解開卡在審批/提問的等待者。** 回合停在 `can_use_tool` 裡等人回答
           時,光送 client.interrupt() 不會叫醒那個協程 —— 它會一路等到 110 秒
           fail-closed。使用者按了停止卻什麼都沒發生,最久兩分鐘(複查抓到)。
           提問 → 當「略過」(回答問題不是危險操作);一般審批 → 拒絕。
        ② 原本**無條件回 True**,連 client 已死都說成功。改成誠實回報。
        """
        if not self.busy:
            return False
        woke = 0
        for aid, w in list(self.registry.pending_approvals.items()):
            if w.get("session") != self.name or w.get("decision") is not None:
                continue
            opts = [str(o.get("key") or "")
                    for o in ((w.get("record") or {}).get("options") or [])]
            w["decision"] = "ask:skip" if any(
                k.startswith("ask:") for k in opts) else "deny"
            w["event"].set()
            woke += 1
        if woke:
            _log("cc2_interrupt_woke_approvals", session=self.name, count=woke)
        if self.client is None:
            # 沒有 client 但有 turn task(例如正卡在審批上):叫醒等待者就夠了,
            # 回合會自己走到收尾。
            return bool(woke)
        try:
            await self.client.interrupt()
        except Exception as e:  # noqa: BLE001
            # interrupt 送不出去多半 = client 已死;turn task 會自己吃到
            # 例外走 teardown 收尾,這裡不重複收。
            _log("cc2_interrupt_failed", session=self.name,
                 error=type(e).__name__, error_message=str(e)[:160])
            return bool(woke)
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
                elif type(block).__name__ == "ServerToolUseBlock":
                    # D5:server 端工具(web_search / web_fetch /
                    # code_execution)不是 ToolUseBlock 的子類,原本整個被跳過
                    # —— 模型在搜網路,使用者看到的是一片空白、以為卡住了。
                    # 它不經 can_use_tool(server 端執行),所以只出卡不審批。
                    tools.append(block)
            # 先收文字 draft(同卡 final:true),再出工具卡 —— 順序與模型
            # 產出一致,app 端才不會「工具卡插在半句話中間」。
            self.digest.assistant_final("".join(texts))
            for block in tools:
                self.digest.tool_card(block.name,
                                      _tool_summary(block.name, block.input),
                                      use_id=getattr(block, "id", "") or "")
            # 工具結果**也會夾在 AssistantMessage 裡**(SDK message_parser 的
            # case "assistant" 同樣收 tool_result,不只 case "user")。只在
            # UserMessage 收會漏掉一部分,而且安靜無聲 —— 只是結果看不到。
            # 放在工具卡之後:同一則訊息裡若同時有 use 與 result,卡先建好才回填。
            self._collect_tool_results(msg)
            self._collect_tool_images(msg)
            return False
        # ⚠️ `getattr` 而不是 `sdk.UserMessage` —— SDK 版本之間型別會增減,
        # 直接取屬性會在**訊息迴圈正中間**丟 AttributeError,整個回合被
        # cc2_turn_error 打斷(2026-10-29 實測:舊版 stub 就重現了)。
        # 取不到時退成空 tuple,`isinstance` 一律 False:少收一種訊息
        # 比整個回合炸掉好,而且那條路本來就是「順便多收一點」。
        if isinstance(msg, getattr(sdk, "UserMessage", ())):
            # 工具結果。cc2 原本整個不看 UserMessage —— 工具回傳的圖進不了
            # 卡片庫,文字結果也完全看不到(只看得到「呼叫了什麼」)。
            self._collect_tool_results(msg)
            self._collect_tool_images(msg)
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

    def _collect_tool_results(self, msg) -> None:
        """ToolResultBlock 的文字 → 回填到對應的工具卡。整段 fail-safe。"""
        for block in (getattr(msg, "content", None) or []):
            # ServerToolResultBlock 形狀與 ToolResultBlock 相同(SDK 註解明說
            # "Mirrors ToolResultBlock's shape"),只是 content 是原始 dict。
            if type(block).__name__ not in ("ToolResultBlock",
                                            "ServerToolResultBlock"):
                continue
            use_id = str(getattr(block, "tool_use_id", "") or "")
            if not use_id:
                continue
            content = getattr(block, "content", None)
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                # 圖的部分另由 _collect_tool_images 處理,這裡只取文字。
                text = "\n".join(
                    str(p.get("text") or "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text")
            elif isinstance(content, dict):
                # server tool 的結果是原始 dict(對這一層不透明)。盡量挑出
                # 人看得懂的欄位,挑不到就壓成 JSON —— 總比一片空白好。
                text = str(content.get("content") or content.get("text")
                           or content.get("result") or "")
                if not text:
                    try:
                        text = json.dumps(content, ensure_ascii=False)[:600]
                    except Exception:  # noqa: BLE001
                        text = ""
            else:
                text = ""
            try:
                self.digest.tool_result(
                    use_id, text, is_error=bool(getattr(block, "is_error", False)))
            except Exception as e:  # noqa: BLE001
                _log("cc2_tool_result_card_failed", session=self.name,
                     error=type(e).__name__, error_message=str(e)[:120])

    def _collect_tool_images(self, msg) -> None:
        """ToolResultBlock 裡的 base64 圖 → media store → attachment 卡。
        整段 fail-safe:圖入庫壞掉絕不影響回合(文字回覆比圖重要)。"""
        if MEDIA_SINK is None:
            return
        try:
            sink = MEDIA_SINK(f"cc2:{self.name}")
        except Exception:  # noqa: BLE001
            return
        if sink is None:
            return
        for block in (getattr(msg, "content", None) or []):
            if type(block).__name__ != "ToolResultBlock":
                continue
            content = getattr(block, "content", None)
            if not isinstance(content, list):
                continue
            items = []
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "image":
                    continue
                src = part.get("source") or {}
                data = src.get("data")
                if not data:
                    continue
                try:
                    wired = sink(str(src.get("media_type") or "image/png"),
                                 str(data))
                except Exception as e:  # noqa: BLE001
                    _log("cc2_media_sink_failed", session=self.name,
                         error=type(e).__name__, error_message=str(e)[:120])
                    continue
                if wired:
                    items.append(wired)
            if items:
                self.digest.media_card(items, tool=self.digest.store.last_tool)
                _log("cc2_tool_images", session=self.name, count=len(items))

    # ── 審批(can_use_tool → Pocket 審核中心)──

    @staticmethod
    def _ask_question_options(tool_input: dict) -> tuple:
        """AskUserQuestion 的 input → (卡片選項, 題目, 是否多選)。

        CLI 的 AskUserQuestion 工具會帶 `questions: [{question, header,
        multiSelect, options:[{label, description}]}]`。原本 cc2 把它當成一般
        工具,卡片只有「允許/拒絕」——使用者看不到選項、只能盲選,等同卡住
        (機主 2026-10-29)。這裡把真正的選項攤成卡片按鈕。
        只處理第一題(Pocket 的審批卡一次一題;多題時其餘退回一般審批)。
        """
        qs = tool_input.get("questions") or []
        if not isinstance(qs, list) or not qs:
            return [], "", False
        q0 = qs[0] if isinstance(qs[0], dict) else {}
        opts = q0.get("options") or []
        if not isinstance(opts, list) or not opts:
            return [], "", False
        out = []
        for i, o in enumerate(opts[:6]):      # 卡片按鈕上限,超過的截斷
            if not isinstance(o, dict):
                continue
            label = str(o.get("label") or "").strip()
            if not label:
                continue
            out.append({"key": f"ask:{i}", "label": label[:40],
                        "style": "primary" if i == 0 else "secondary"})
        if not out:
            return [], "", False
        # 一律補「略過」—— CLI 的原生 UI 也永遠有 Skip,不能讓人無路可走。
        out.append({"key": "ask:skip", "label": "略過", "style": "danger"})
        title = str(q0.get("question") or q0.get("header") or "請選擇")[:200]
        return out, title, bool(q0.get("multiSelect"))

    async def _can_use_tool(self, tool: str, tool_input: dict, _ctx):
        """SDK 的結構化審批回呼。建 pending record(卡片 + DB 鏡像)→ 等
        `/app/v1/approvals/{id}/decision` 叫醒 → 逾時/無決議一律 DENY
        (fail-closed,鏡像 Cindy:絕不掛起、絕不默許)。"""
        sdk = _sdk()
        if tool != "AskUserQuestion" and tool in self.session_allowed_tools:
            # 「本次全允許」記憶命中:同 session 同工具直接放行,不出卡
            # 不落 DB(記憶體記號,bridge 重啟/重建即清空 → 會重新問)。
            # **排除 AskUserQuestion**:放行提問 = CLI 回「沒人回答」,等於
            # 把提問功能永久關掉且無聲(見 _can_use_tool 尾端的說明)。
            _log("cc2_approval_auto_allow", session=self.name, tool=tool)
            return sdk.PermissionResultAllow()
        aid = "cc2-" + uuid.uuid4().hex[:12]
        ask_opts, ask_title, ask_multi = ([], "", False)
        if tool == "AskUserQuestion":
            ask_opts, ask_title, ask_multi = self._ask_question_options(tool_input)
        title = ask_title or str(getattr(_ctx, "title", "") or
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
            "options": ask_opts or [
                {"key": "approve", "label": "允許", "style": "primary"},
                {"key": "approve_for_session", "label": "本次全允許",
                 "style": "secondary"},
                {"key": "deny", "label": "拒絕", "style": "danger"}],
        }
        if ask_opts:
            record["kind"] = "question"       # app 可據此換版面(不換也能用)
            record["risk"] = "low"            # 回答問題不是危險操作
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
        if decision == "approve_for_session" and not ask_opts:
            # D8:AskUserQuestion **永遠不准**進「本次全允許」記憶。進去之後
            # 後續每個提問都會在出卡前被自動放行,而放行 = CLI 回「沒人回答」
            # —— 使用者再也看不到任何提問,而且完全無聲。
            self.session_allowed_tools.add(tool)
        if ask_opts and decision:
            # D1:提問的 canonical 狀態是 `answered`,**不是** denied。
            # 原本一律套 approve/deny 二元判斷,`ask:N` 不在允許清單裡 →
            # 被算成 denied:使用者選了「Postgres」,卡片卻顯示「已拒絕」,
            # DB 也記成 denied(2026-10-29 複查抓到;我自己的測試只驗了
            # 路由回傳值,所以一路綠)。
            # 字彙依 `_approval_decide_core` 的宣告:permission→approved|denied、
            # question→answered(result=key)。
            status = "answered"
        else:
            status = "approved" if approved else (
                "denied" if decision else "expired")
        self.digest.resolve_approval(record, status)
        _db_hook(APPROVAL_DB_MARK, aid, status)
        _log("cc2_approval_decided", session=self.name, approval_id=aid,
             tool=tool, status=status)
        if ask_opts:
            # AskUserQuestion:把使用者選的那顆**當成工具結果**送回模型。
            #
            # 為什麼不是 `PermissionResultAllow(updated_input=...)`:試過,無效。
            # 2026-10-29 實測(真 SDK 0.2.174 + 真 CLI 2.1.207):把答案塞進
            # tool_input,工具結果一律是
            #     "The user did not answer the questions."
            # —— AskUserQuestion 的答案是 CLI **互動式 UI** 自己收的,不在
            # tool_input 裡(`strings` 掃 CLI 二進位也查不到我們塞的鍵名)。
            # 在無 TTY 的 SDK 模式下放行它 = 必然「沒人回答」,使用者按了等於
            # 白按。
            #
            # 可行的管道是 deny 的 message:實測它會原文變成 tool_result 送到
            # 模型眼前(is_error=true,但模型照樣讀懂並正確引用答案)。格式用
            # CLI 原生 TUI 自己產生的 `[User answered <header>]: <答案>`,模型
            # 是 native 看得懂的,不必額外解釋。
            picked = ""
            extract_error = ""      # 「取不到答案」與「使用者略過」必須分得出來
            if isinstance(decision, str) and decision.startswith("ask:"):
                idx = decision.split(":", 1)[1]
                if idx != "skip":
                    try:
                        q0 = (tool_input.get("questions") or [{}])[0]
                        picked = str((q0.get("options") or [])[int(idx)]
                                     .get("label") or "")
                    except Exception as exc:  # noqa: BLE001
                        # ⚠️ 不可以靜默退成 ""。下面 `if not picked:` 會回
                        # 「The user did not answer the questions.」—— 使用者明明
                        # 按了,模型卻被告知沒人回答。**這正是這條分支自己宣稱
                        # 修好的那個 bug**,從例外這條路爬回來。
                        # 而且原本那行 _log 會把它記成 picked="(略過)",跟使用者
                        # 真的按略過**完全無法區分** —— 不是沒留痕,是留了個會
                        # 誤導的痕跡,比沒留更難查。
                        picked = ""
                        extract_error = f"{type(exc).__name__}: {exc}"[:120]
            _log("cc2_question_answered", session=self.name,
                 approval_id=aid,
                 picked=picked[:40] or ("(取答案失敗)" if extract_error else "(略過)"),
                 extract_error=extract_error or None,
                 multi=ask_multi)
            if not picked:
                # 略過/逾時:回 CLI 原生的那句話,模型自己決定怎麼繼續
                # (**不** fail-closed 打斷回合 —— 回答問題不是危險操作)。
                return sdk.PermissionResultDeny(
                    message="The user did not answer the questions.",
                    interrupt=False)
            header = "the question"
            try:
                q0 = (tool_input.get("questions") or [{}])[0]
                header = str(q0.get("header") or q0.get("question")
                             or header)[:60]
            except Exception:  # noqa: BLE001
                pass
            # multiSelect 題:Pocket 的審批卡一次只收一顆,所以只送一個答案。
            # 明講「只選了這一項」而不是假裝涵蓋全部,模型才不會以為使用者
            # 放棄其餘選項而自行補齊。
            answer = picked if not ask_multi else f"{picked}(只選了這一項)"
            msg_text = f"[User answered {header}]: {answer}"
            try:
                n_q = len(tool_input.get("questions") or [])
            except Exception:  # noqa: BLE001
                n_q = 1
            if n_q > 1:
                # D9:Pocket 的審批卡一次一題,只答得了第一題。不講的話模型會
                # 以為整組都答完了,然後拿沒人回答的那幾題去推論。
                msg_text += (f" (only the first of {n_q} questions was shown "
                             "to the user; the rest are unanswered — ask them "
                             "one at a time if you still need them)")
            return sdk.PermissionResultDeny(message=msg_text, interrupt=False)
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
            # model 是後加的欄位:舊的 state 檔沒有這個鍵 → 空字串 = 跟隨 CLI 預設,
            # 與加這段之前的行為相同(純加法,不會把既有 session 弄壞)。
            self.sessions[name].model = str(row.get("model") or "")
            self.sessions[name].archived = bool(row.get("archived") or False)
            self.sessions[name].spawn_opts = sanitize_spawn_opts(
                row.get("spawn_opts"))
            self.sessions[name].prior_session_ids = [
                str(x) for x in (row.get("prior_session_ids") or [])
                if str(x).strip()]
            # 重啟復原:把上次被中斷的回合誠實講出來(不自動重送 —— 工具副作用
            # 可能已經執行了一半,替使用者重跑是危險的),排隊中的則原樣接回。
            sess = self.sessions[name]
            # 新格式是 dict(帶附件/client_id);舊格式純字串也吃得下
            # (升級當下佇列裡還有舊項目不會炸)。
            sess.queue = []
            for q in (row.get("queued") or []):
                if isinstance(q, str):
                    sess.queue.append((q, [], ""))
                elif isinstance(q, dict) and str(q.get("text") or ""):
                    sess.queue.append((str(q["text"]),
                                       list(q.get("attachments") or []),
                                       str(q.get("client_id") or "")))
            sess.digest.queue_depth = len(sess.queue)
            unfinished = str(row.get("in_flight") or "").strip()
            if unfinished:
                sess.digest.error_card(
                    "上一則在 bridge 重啟時被中斷,沒有完成:「"
                    + unfinished[:80] + ("…」" if len(unfinished) > 80 else "」")
                    + " 需要的話請重送。")

    def save(self) -> None:
        """原子落地(tmp+rename);失敗只記 log —— resume id 掉了下次全新
        重建即可,不值得為它炸掉 turn。"""
        try:
            path = state_path()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            payload = {"sessions": {
                s.name: {"sdk_session_id": s.sdk_session_id,
                         "workdir": s.workdir,
                         "permission_mode": s.permission_mode,
                         "model": s.model,
                         "spawn_opts": dict(s.spawn_opts or {}),
                         # 自癒換過 id 的舊逐字稿:留著才翻得回更早的歷史
                         # (不留的話那段對話重啟後就永久找不到了)。
                         "prior_session_ids": list(s.prior_session_ids or [])[-8:],
                         "archived": bool(s.archived),
                         # bridge 重啟會殺掉進行中的回合(client 是 bridge 的
                         # 子行程)。把「當時正在跑什麼」與排隊中的留下來,
                         # 重啟後才能誠實告訴使用者,而不是安靜地什麼都沒發生。
                         "in_flight": s.in_flight_prompt if s.busy else "",
                         # 排隊項目完整落地(原本只存文字 → 重啟後附件與
                         # client_id 都掉了:圖不見了、待送泡泡也對不上)。
                         "queued": [
                             {"text": q[0], "attachments": list(q[1] or []),
                              "client_id": q[2] if len(q) > 2 else ""}
                             if isinstance(q, tuple)
                             else {"text": str(q), "attachments": [],
                                   "client_id": ""}
                             for q in s.queue]}
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
               permission_mode: str = "", model: str = "",
               spawn_opts: dict | None = None) -> CCSdkSession:
        """建立或更新(冪等):同名重 POST 只更新 workdir/permission_mode/model,
        不打斷進行中的 client/turn。

        注意 model/permission_mode 對**已連線**的 client 不會自動生效 ——
        `ClaudeAgentOptions` 只在 connect 當下讀一次。要改活著的 session 走
        `apply_runtime_settings()`(它呼叫 SDK 的 set_model/set_permission_mode)。
        """
        self.load()
        sess = self.sessions.get(name)
        if sess is None:
            sess = self.sessions[name] = CCSdkSession(
                self, name, workdir=workdir, permission_mode=permission_mode,
                model=model)
            sess.spawn_opts = sanitize_spawn_opts(spawn_opts)
            # 按工作目錄接回上一段對話(對應 ccsess 的 `--continue`)——
            # 使用者不必記 session id。只接**沒人正在用**的那條;接到了就
            # 重新 seed 歷史,畫面一進去就有上下文。預設開,可用
            # CC_SDK_ADOPT_BY_CWD=0 關掉(接到不相干對話時的逃生口)。
            if not sess.sdk_session_id and str(
                    os.environ.get("CC_SDK_ADOPT_BY_CWD", "1")).strip().lower() \
                    not in ("0", "false", "no", "off"):
                adopted = newest_session_for(sess.workdir)
                if adopted:
                    sess.sdk_session_id = adopted
                    sess._seed_history()
                    _log("cc2_adopted_by_cwd", session=name,
                         sdk_session_id=adopted[:18], workdir=sess.workdir)
            _log("cc2_session_created", session=name, workdir=sess.workdir,
                 permission_mode=sess.permission_mode)
        else:
            if workdir:
                sess.workdir = os.path.expanduser(workdir)
            # 先改值再鏡像 —— 反過來寫的話鏡像會慢一拍(狀態列顯示上一次的
            # 權限模式,要再 POST 一次才追上)。
            if permission_mode:
                sess.permission_mode = _clamp_mode(permission_mode)
            if model:
                sess.model = model.strip()
            if spawn_opts:
                # 冪等更新:只覆蓋這次帶來的鍵,沒帶的沿用(不然每次 create
                # 都要把所有 pin 重送一遍)。
                sess.spawn_opts.update(sanitize_spawn_opts(spawn_opts))
            sess.digest.mode = sess.permission_mode   # 鏡像跟著更新
            sess.digest.model = sess.model
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
        if isinstance(key, str) and key.startswith("ask:"):
            waiter["decision"] = key          # 提問:key 原樣帶回(ask:N / ask:skip)
            waiter["event"].set()
            return "approved"
        decision = ("deny" if key == "deny" else
                    "approve_for_session" if key == "approve_for_session" else
                    "approve")
        waiter["decision"] = decision
        waiter["event"].set()
        return "denied" if decision == "deny" else "approved"

    def rename(self, old: str, new: str) -> None:
        """完整改名。只換 `sessions` 的鍵是不夠的 —— 原本漏掉四件事,每一件
        都安靜失效(2026-10-29 複查):
          · BYO key 的 secret 檔是按名字存的 → 讀不到,退回預設憑證
          · `digest.name` 沒換 → 後續卡 id 還掛舊名
          · `digest.store.media_session_id` 沒換 → 已出的圖網址對不上
          · pending 審批的 `waiter["session"]` 沒換 → `pending_for(新名)` 空的,
            那筆審批變成**按不下去**,只能等 110 秒 fail-closed 拒絕
        """
        sess = self.sessions.pop(old, None)
        if sess is None:
            return
        sess.name = new
        sess.digest.name = new
        try:
            sess.digest.store.media_session_id = f"cc2:{new}"
        except Exception:  # noqa: BLE001
            pass
        for w in self.pending_approvals.values():
            if w.get("session") == old:
                w["session"] = new
        move_spawn_secret(old, new)
        self.sessions[new] = sess
        self.save()

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
            if getattr(sess, "archived", False):
                continue                      # 封存的不進列表(對話仍在)
            pend = self.pending_for(name)
            status = "waiting_approval" if pend else (
                "running" if sess.busy else "idle")
            # D12:app 列表/詳情要用的欄位原本都沒吐(workdir 連 subtitle 退路
            # 都拿不到、WorkingBar 的計時拿不到、排隊深度看不到)。
            meta = {"permission_mode": sess.permission_mode,
                    "workdir": sess.workdir,
                    "queue_depth": len(sess.queue or []),
                    "busy_since": getattr(sess.digest, "busy_since", 0) or None}
            if sess.model:
                meta["model"] = sess.model
            if sess.spawn_opts:
                meta["spawn_opts"] = dict(sess.spawn_opts)
            ctx_max = getattr(sess.digest, "ctx_max", 0)
            if ctx_max:
                meta["context"] = {
                    "used": sess.digest.ctx_tokens, "size": ctx_max,
                    "pct": round(sess.digest.ctx_pct, 1)}
            if pend:
                meta["approval"] = pend
                meta["blocked"] = True
            if sess.sdk_session_id:
                # 終端接手指令(app 未來可直接顯示/複製;對話裡另有同款卡)
                meta["resume_command"] = \
                    f"claude --resume {sess.sdk_session_id}"
            # last_event_at 原本硬寫 None → app 列表排序拿不到時間、永遠墊底;
            # subtitle 改用最後一則對話(拿不到才退回 workdir),與 tmux 列同語意。
            last_at = getattr(sess.digest, "last_event_at", 0.0) or None
            preview = ""
            try:
                cards = list(getattr(sess.digest.store, "cards", {}).values())
                for c in reversed(cards):
                    t = str((c.get("body") or {}).get("text") or "").strip()
                    if t:
                        preview = t.replace("\n", " ")[:120]
                        break
            except Exception:  # noqa: BLE001
                preview = ""
            out.append({"id": f"cc2:{name}", "provider": "cc2", "title": name,
                        "subtitle": preview or sess.workdir, "status": status,
                        "last_event_at": last_at,
                        # D3:原本漏 "attachments" —— app 若以 capabilities 決定
                        # 要不要顯示附件鈕,整批附件功能等於隱形。不列 "keys"
                        # (SDK 沒有 pane 可送按鍵,那是誠實的差異)。
                        "capabilities": ["input", "interrupt", "attachments",
                                         "replay", "follow"]
                        + (["approve"] if pend else []),
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
