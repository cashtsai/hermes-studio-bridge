"""全機 agent 發現與收編(SUBPROCESS_HARNESS_DESIGN_20260811 §2.3)。

善彰定調:「Pocket 是那台機器的**指揮艙**,不是只管從 Pocket 開的。」
使用者自己在桌機/CLI 開的 session、BYO-key 開的、別家模型開的,一律
**發現 → 呈現 → 可收編**。

這個模組是發現面的**純函式層**:tmux/ps 輸出解析、managed vs discovered
判定、ccsess 設定檔的文字轉換。**不 import bridge、不跑任何 subprocess、
不碰檔案**——所有 I/O 由 bridge 側薄殼負責,這裡只做可單測的判斷。

兩條鐵律(整個發現面的安全前提):

1. **只讀不動**:本模組不產生、也不得產生任何 kill / send-keys /
   respawn / restart 指令。收編是純記帳(cc = 加 ccsess 名單 + 登記戶口;
   cx/hermes/openclaw = 純登記),pane 上跑到一半的工作完全不受影響。
2. **API key 只回報有無**:`env_blob_has_key` 是唯一碰到環境變數字串的
   地方,它**只回傳 bool**。key 的值不得被回傳、記錄、快取或放進 payload。
"""
from __future__ import annotations

import re

# registry 的 provider 字串(與 agent_registry / _registry_legacy_row 對齊,
# app 端渲染不用分家)。
CC_PROVIDER = "claude_code"
CX_PROVIDER = "codex"
HERMES_PROVIDER = "hermes"
DISPATCH_PROVIDER = "dispatch"
OPENCLAW_PROVIDER = "openclaw"

STATE_MANAGED = "managed"          # 已在治理內(ccsess 名單 / registry 登記)
STATE_DISCOVERED = "discovered"    # 看得到、還沒收編;reaper 永不碰

# BYO-key 偵測用的 env 名(§2.2)。**只看有沒有這個名字,不看值**。
API_KEY_ENV_BY_PROVIDER = {
    CC_PROVIDER: ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    CX_PROVIDER: ("OPENAI_API_KEY",),
}

# 掃全機 pane 的 tmux 格式。欄位順序固定,parse_tmux_panes 依賴它。
TMUX_PANE_FORMAT = ("#{session_name}|#{pane_current_command}|#{pane_pid}"
                    "|#{pane_current_path}|#{session_created}")


# ── tmux / ps 解析 ──────────────────────────────────────────────────────

def parse_tmux_panes(out: str) -> list[dict]:
    """`tmux list-panes -a -F TMUX_PANE_FORMAT` 的輸出 → pane dict 列。

    session_created 在尾巴、pane_current_path 可能含 `|`(路徑合法字元),
    所以頭三欄由左往右吃、created 由右往左吃,中間全部還原成路徑。
    """
    rows: list[dict] = []
    for line in (out or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        try:
            pane_pid = int(parts[2])
        except ValueError:
            continue                      # 沒有 pid 的行沒有可控性,直接丟
        created = None
        tail = 0
        if len(parts) >= 5:
            try:
                created = float(parts[-1])
                tail = 1
            except ValueError:
                created = None
        path = "|".join(parts[3:len(parts) - tail]) if tail else "|".join(parts[3:])
        rows.append({"session": parts[0], "command": parts[1],
                     "pane_pid": pane_pid, "path": path,
                     "created_ts": created})
    return rows


def build_child_map(procs: dict) -> dict[int, list[int]]:
    """`{pid: (ppid, cmdline)}` → `{ppid: [pid, ...]}`。"""
    kids: dict[int, list[int]] = {}
    for pid, val in (procs or {}).items():
        try:
            ppid = int(val[0])
        except (TypeError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(pid)
    return kids


def descendants(root_pid: int, kids: dict, limit: int = 500) -> list[int]:
    """含自身的子孫 pid(廣度優先,防環、有上限)。"""
    out: list[int] = []
    stack, seen = [int(root_pid)], set()
    while stack and len(out) < limit:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        stack.extend(kids.get(pid, []))
    return out


def is_claude_cmdline(cmd: str) -> bool:
    """這條 cmdline 是不是 claude CLI 本體?

    **不可以**用 tmux 的 `pane_current_command` 判斷:實測它回的是版本
    字串(如 `2.1.207`)而不是 `claude`,靠指令名會把全機 cc 漏光。正解
    是回頭掃 pane pid 的行程樹 cmdline —— 這個函式就是那個判斷。

    比 bridge 既有的 `"claude" in cmd` 嚴:只認 argv[0] 的檔名(或 node
    包一層的 claude 入口),免得 `tail -f claude.log` / `grep claude`
    這種旁觀者被當成 agent。
    """
    parts = (cmd or "").split()
    if not parts:
        return False
    base = parts[0].rsplit("/", 1)[-1]
    if base in ("claude", "claude-code"):
        return True
    if base in ("node", "bun", "deno") and len(parts) > 1:
        arg = parts[1]
        if "claude" not in arg:
            return False
        return arg.rsplit("/", 1)[-1] in ("claude", "claude-code", "cli.js")
    return False


def find_agent_proc(pane_pid: int, procs: dict, kids: dict,
                    predicate) -> tuple[int, str] | None:
    """pane 的子孫樹裡第一個滿足 predicate 的行程 → (pid, cmdline)。"""
    for pid in descendants(pane_pid, kids):
        cmd = (procs.get(pid) or (0, ""))[1]
        if cmd and predicate(cmd):
            return pid, cmd
    return None


_CC_MODEL_RE = re.compile(r"--model[= ]\s*([^\s]+)")
_CC_REMOTE_RE = re.compile(r"--remote-control[= ]\s*([^\s]+)")
_CC_SID_RE = re.compile(r"--(?:resume|session-id)[= ]\s*"
                        r"([0-9a-fA-F][0-9a-fA-F-]{7,63})")
_CC_PERM_RE = re.compile(r"--permission-mode[= ]\s*([^\s]+)")


def parse_cc_cmdline(cmd: str) -> dict:
    """從 claude cmdline 撈免費就能拿到的執行態:模型、審核模式、
    session uuid、ccsess 具名。撈不到就不放這個 key(app 端不用處理空字串)。"""
    out: dict = {}
    for key, rx in (("model", _CC_MODEL_RE),
                    ("permission_mode", _CC_PERM_RE),
                    ("session_id", _CC_SID_RE),
                    ("remote_control", _CC_REMOTE_RE)):
        m = rx.search(cmd or "")
        if m:
            out[key] = m.group(1)
    return out


def env_blob_has_key(blob: str, names) -> bool:
    """這串環境變數裡有沒有設(且非空)其中一個 key?**只回 bool**。

    ⚠️ 呼叫端契約:blob 帶著使用者的真 key。這個函式不回傳它、不記錄它;
    呼叫端拿到 bool 之後必須立刻丟棄 blob,絕不可寫進 payload/log/快取。
    """
    text = blob or ""
    for name in names or ():
        needle = f"{name}="
        idx = text.find(needle)
        while idx >= 0:
            # 要嘛在開頭,要嘛前面是空白 —— 免得 MY_ANTHROPIC_API_KEY= 誤中
            if idx == 0 or text[idx - 1].isspace():
                start = idx + len(needle)
                end = text.find(" ", start)
                if (len(text) if end < 0 else end) > start:
                    return True          # 有名有值 = 有帶 key
                break
            idx = text.find(needle, idx + 1)
    return False


# ── ccsess sessions.conf 的純文字轉換 ────────────────────────────────────
# 格式:`name|workdir|enabled`,`#` 開頭是註解。這個檔是 ccsess CLI 與
# bridge 共用的,**註解與行序一律原樣保留**(使用者手寫的說明不能被機器吃掉)。

def parse_conf_rows(text: str) -> list[tuple[str, str, str]]:
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) >= 3:
            rows.append((parts[0], parts[1], parts[2].strip()))
    return rows


def conf_upsert_lines(lines: list[str], name: str, workdir: str,
                      enabled: str = "1") -> list[str]:
    """收編寫入:同名就地覆寫,沒有就附在最後。註解/空行/其他行不動。"""
    out, found = [], False
    for line in lines or []:
        if not line or line.startswith("#"):
            out.append(line)
            continue
        parts = line.split("|")
        if parts and parts[0] == name:
            out.append(f"{name}|{workdir}|{enabled}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{name}|{workdir}|{enabled}")
    return out


def conf_remove_lines(lines: list[str], name: str) -> tuple[list[str], bool]:
    """釋放(選配):把該 session 的行整條移除。回 (新行列, 有沒有動到)。"""
    out, removed = [], False
    for line in lines or []:
        if line and not line.startswith("#"):
            parts = line.split("|")
            if parts and parts[0] == name:
                removed = True
                continue
        out.append(line)
    return out, removed


# ── 四路發現面 → 統一 item 形狀 ──────────────────────────────────────────
# item:{id, provider, name, workdir?, state, registry_state?, busy?, model?,
#       has_api_key?, source?, since_ts?}

def cc_discovery_items(panes: list[dict], conf_rows, claude_by_pane: dict,
                       *, api_key_by_pid: dict | None = None) -> list[dict]:
    """cc 發現面:全機 tmux pane × ccsess 名單。

    - `managed`:在 `sessions.conf` 且 enabled=1(ccsess 會 ensure/自癒它)。
    - `discovered`:使用者自己 `tmux new` 開的 claude,或名單裡 enabled=0
      的封存 lane 卻還活著 —— 兩種都看得到、都可以一鍵收編。
    - **硬限制**:不在 tmux 裡的 claude 沒有 pane 可控,發現不到也控制不了
      (設計如此,不是 bug)。

    `claude_by_pane`:{pane_pid: {"pid":…, "cmdline":…}},由 bridge 側掃
    ps 行程樹填(pane_current_command 是版本字串,不能用)。
    """
    conf = {name: (wd, en) for name, wd, en in (conf_rows or [])}
    by_session: dict[str, tuple] = {}
    for pane in panes or []:
        sess = pane.get("session") or ""
        if not sess:
            continue
        info = (claude_by_pane or {}).get(pane.get("pane_pid"))
        cur = by_session.get(sess)
        # 一個 tmux session 可能多 pane:優先挑真的有 claude 在跑的那個
        if cur is None or (info is not None and cur[1] is None):
            by_session[sess] = (pane, info)

    items = []
    for sess in sorted(by_session):
        pane, info = by_session[sess]
        in_conf = sess in conf
        if info is None and not in_conf:
            continue                     # 純 shell 的 tmux session,不是 agent
        conf_wd, enabled = conf.get(sess, ("", ""))
        managed = in_conf and enabled == "1"
        source = "ccsess" if managed else ("ccsess-disabled" if in_conf else "tmux")
        item = {
            "id": f"{CC_PROVIDER}:{sess}",
            "provider": CC_PROVIDER,
            "name": sess,
            # 收編要寫進名單的就是這個 workdir:名單有就用名單的(權威),
            # 沒有才用 pane 當下的路徑(使用者 cd 過就會偏,已知限制)。
            "workdir": conf_wd or pane.get("path") or "",
            "state": STATE_MANAGED if managed else STATE_DISCOVERED,
            "source": source,
            "since_ts": pane.get("created_ts"),
            "alive": info is not None,
            "tmux_session": sess,
            "pane_pid": pane.get("pane_pid"),
        }
        parsed = parse_cc_cmdline(info["cmdline"]) if info else {}
        if parsed.get("model"):
            item["model"] = parsed["model"]
        if parsed.get("permission_mode"):
            item["permission_mode"] = parsed["permission_mode"]
        if parsed.get("session_id"):
            item["session_id"] = parsed["session_id"]
        if parsed.get("remote_control"):
            item["remote_control"] = parsed["remote_control"]
        if api_key_by_pid is not None and info is not None:
            item["has_api_key"] = bool(api_key_by_pid.get(info["pid"], False))
        items.append(item)
    return items


def cx_discovery_items(summaries: list[dict], registered_ids) -> list[dict]:
    """cx 發現面:`thread/list` 的 sourceKinds cli/vscode/exec/appServer 本來
    就看得到,只是以前沒當「可收編」呈現。bridge 沒登記過的 = 使用者自己開的。"""
    registered = set(registered_ids or ())
    items = []
    for s in summaries or []:
        tid = str(s.get("thread_id") or s.get("id") or "")
        if not tid:
            continue
        sid = f"{CX_PROVIDER}:{tid}"
        item = {
            "id": sid,
            "provider": CX_PROVIDER,
            "name": s.get("name") or tid,
            "workdir": s.get("workdir") or "",
            "state": STATE_MANAGED if sid in registered else STATE_DISCOVERED,
            "source": s.get("source") or "unknown",
            "busy": str(s.get("status") or "") == "running",
            "since_ts": _epoch_secs(s.get("updatedAt")),
        }
        if s.get("model"):
            item["model"] = s["model"]
        if s.get("modelProvider"):
            item["model_provider"] = s["modelProvider"]
        items.append(item)
    return items


def hermes_discovery_items(personas) -> list[dict]:
    """hermes 常駐人格:bridge 自有 POOL,恆為 managed(白名單 persistent,
    `_registry_ensure_personas` 開機就落籍)。"""
    return [{"id": f"{HERMES_PROVIDER}:{mid}", "provider": HERMES_PROVIDER,
             "name": display or mid, "state": STATE_MANAGED,
             "source": "persona"}
            for mid, display in personas or ()]


def dispatch_discovery_items(subsessions: dict) -> list[dict]:
    """/dispatch 開出來的子行程(bridge 自有 SUBSESSIONS)——人格雙手的
    實體,bridge 生的所以一律 managed。"""
    items = []
    for sid, sub in (subsessions or {}).items():
        sub = sub or {}
        items.append({
            "id": sid, "provider": DISPATCH_PROVIDER,
            "name": sub.get("name") or sid,
            "workdir": sub.get("worktree") or sub.get("cwd") or "",
            "state": STATE_MANAGED,
            "source": "dispatch",
            "busy": str(sub.get("status") or "") == "running",
            "parent": sub.get("parent") or None,
            "since_ts": _epoch_secs(sub.get("lastAt")),
        })
    return items


def openclaw_discovery_items(rows: list[dict], registered_ids) -> list[dict]:
    """openclaw gateway `sessions.list`:全列。bridge 沒登記過的照樣呈現
    成 discovered —— 它已經可控(v1 relay),收編只是補戶口/purpose/TTL。"""
    registered = set(registered_ids or ())
    items = []
    for row in rows or []:
        sid = str(row.get("id") or "")
        if not sid:
            continue
        item = {
            "id": sid, "provider": OPENCLAW_PROVIDER,
            "name": row.get("title") or sid,
            "state": STATE_MANAGED if sid in registered else STATE_DISCOVERED,
            "source": "gateway",
            "busy": str(row.get("status") or "") == "running",
            "since_ts": _epoch_secs(row.get("last_event_at")),
        }
        if row.get("subtitle"):
            item["model"] = row["subtitle"]
        items.append(item)
    return items


def _epoch_secs(val) -> float | None:
    """毫秒/秒/ISO 都可能來:數字才收,>1e11 視為毫秒。"""
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float)):
        v = float(val)
        if v <= 0:
            return None
        return v / 1000.0 if v > 1e11 else v
    return None
