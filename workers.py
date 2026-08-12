"""Worker 可見層(SUBPROCESS_HARNESS_DESIGN §2.4)——看見 session **裡面**在跑什麼。

registry(agent_registry.py)管的是 **session**:長壽、有 TTL、有配額、被 reaper 收、
進家譜樹。**worker 不是 session**:它是一個 session 內部派出的短命工人(CC 的 Agent/Task
subagent、CX 的 guardian child thread、Hermes 的 dispatch SUBSESSION),生命週期以
秒/分計、churn 極高。

**刻意不塞進 registry 的 session 表**——理由寫在設計書 §2.4:硬套 TTL/reaper/配額
那套會把治理層淹掉(每分鐘幾十筆生滅),也會讓家譜樹爆炸(善彰這個 session 一次
同時跑 3-7 隻手)。所以這裡是一個**純記憶體、每 session 一圈 ring、靜默期自動過期**
的即時視圖:

- 不落 DB、不寫磁碟。bridge 重啟即空 —— **這是正確行為**,它回答的是「現在在跑
  什麼」,不是「今天跑過什麼」(那是稽核紀錄,不是這一層的職責)。
- 沒有背景任務、沒有 reaper、沒有計時器。過期是**讀寫時惰性計算**的,旗標關掉
  時這個模組的成本嚴格為零。
- 靜默期過期(預設 300s):工人只要不再回報就當它結束了。這對「回報 done 的
  PostToolUse hook 沒跑到」(CC 被 Ctrl-C、進程被殺)特別重要 —— 沒有這條規則,
  面板會永遠掛著一隻假的「執行中」。
"""
from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict

DEFAULT_TTL_SECS = 300.0
DEFAULT_CAP = 50            # 每 session 的 ring 上限
DEFAULT_MAX_SESSIONS = 200  # 全域 session 數上限(防呆,正常遠達不到)

STATES = ("running", "done", "failed")
_LABEL_MAX = 200
_META_JSON_MAX = 2048       # meta 序列化後上限,擋住把整份 prompt 塞進來


def normalize_state(raw) -> str:
    """把外界丟進來的 state 收斂成三態。認不得的一律當 running ——
    寧可讓面板多亮一盞燈(靜默期會收掉),也不要讓工人整個消失。"""
    s = str(raw or "").strip().lower()
    if s in STATES:
        return s
    if s in ("ok", "success", "succeeded", "complete", "completed", "finished"):
        return "done"
    if s in ("error", "fail", "failure", "cancelled", "canceled", "interrupted",
             "stalled", "timeout"):
        return "failed"
    return "running"


def _clean_meta(meta) -> dict:
    """meta 必須是 dict、且序列化後不能太大(工人回報是外部來源,不能無條件信任)。"""
    if not isinstance(meta, dict) or not meta:
        return {}
    out = {}
    for k, v in meta.items():
        if not isinstance(k, str):
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v[:_LABEL_MAX] if isinstance(v, str) else v
    try:
        if len(json.dumps(out, ensure_ascii=False)) > _META_JSON_MAX:
            return {}
    except Exception:      # noqa: BLE001 —— 不可序列化的 meta 直接丟掉,絕不讓它炸端點
        return {}
    return out


class WorkerStore:
    """每 session 一圈 ring + 靜默期過期。執行緒安全(hook 走 HTTP 進來)。"""

    def __init__(self, ttl_secs: float = DEFAULT_TTL_SECS,
                 cap: int = DEFAULT_CAP,
                 max_sessions: int = DEFAULT_MAX_SESSIONS):
        self.ttl_secs = float(ttl_secs)
        self.cap = max(1, int(cap))
        self.max_sessions = max(1, int(max_sessions))
        self._lock = threading.Lock()
        # session -> OrderedDict[worker_id, worker dict]
        self._sessions: OrderedDict[str, OrderedDict] = OrderedDict()

    # ── 內部:惰性過期 ────────────────────────────────────────────────
    def _expire_locked(self, ring: OrderedDict, now: float) -> None:
        cutoff = now - self.ttl_secs
        dead = [wid for wid, w in ring.items() if w["updated_ts"] < cutoff]
        for wid in dead:
            ring.pop(wid, None)

    def _prune_sessions_locked(self, now: float) -> None:
        """順手掃掉整個已經空掉的 session,不讓 key 無限累積。"""
        empty = [sid for sid, ring in self._sessions.items() if not ring]
        for sid in empty:
            self._sessions.pop(sid, None)
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)   # 最久沒被碰的整個 session 掉

    # ── 寫入 ────────────────────────────────────────────────────────
    def report(self, session: str, worker_id: str, label: str = "",
               state="running", parent_worker: str | None = None,
               meta=None, now: float | None = None) -> dict:
        """Upsert 一筆工人回報,回傳落地後的 worker(呼叫端負責驗參)。

        同一個 worker_id 重複回報 = 更新 state/label/updated_ts,**started_ts 不變**
        (工人開始的時間是它的身分,不該被第二次回報洗掉)。已被靜默期收走的
        worker_id 再回報 = 當新工人復活,拿到新的 started_ts —— 這是誠實的:
        中間那段空窗它確實不在視圖裡。
        """
        now = time.time() if now is None else float(now)
        session = str(session)
        worker_id = str(worker_id)
        label = str(label or "")[:_LABEL_MAX]
        state = normalize_state(state)
        parent = str(parent_worker)[:_LABEL_MAX] if parent_worker else None
        meta = _clean_meta(meta)
        with self._lock:
            ring = self._sessions.get(session)
            if ring is None:
                ring = OrderedDict()
                self._sessions[session] = ring
            self._sessions.move_to_end(session)
            self._expire_locked(ring, now)
            cur = ring.get(worker_id)
            if cur is None:
                cur = {"worker_id": worker_id, "label": label, "state": state,
                       "parent_worker": parent, "started_ts": now,
                       "updated_ts": now, "meta": meta}
                ring[worker_id] = cur
                # ring 上限:滿了就擠掉最舊的一筆(插入序 = 開工序)
                while len(ring) > self.cap:
                    ring.popitem(last=False)
            else:
                cur["state"] = state
                cur["updated_ts"] = now
                if label:
                    cur["label"] = label
                if parent:
                    cur["parent_worker"] = parent
                if meta:
                    cur["meta"] = {**(cur.get("meta") or {}), **meta}
            self._prune_sessions_locked(now)
            return dict(cur)

    # ── 讀取 ────────────────────────────────────────────────────────
    def list(self, session: str, now: float | None = None) -> list[dict]:
        """該 session 目前還「活著」(靜默期內)的工人,開工序由舊到新。"""
        now = time.time() if now is None else float(now)
        with self._lock:
            ring = self._sessions.get(str(session))
            if not ring:
                return []
            self._expire_locked(ring, now)
            out = [dict(w) for w in ring.values()]
            self._prune_sessions_locked(now)
            return out

    def sessions(self) -> list[str]:
        with self._lock:
            return list(self._sessions.keys())

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


def counts_of(workers: list[dict]) -> dict:
    out = {"running": 0, "done": 0, "failed": 0}
    for w in workers or []:
        st = w.get("state")
        if st in out:
            out[st] += 1
    return out


def merge(reported: list[dict], projected: list[dict]) -> list[dict]:
    """回報進來的工人 + provider 投影出來的工人,合成一份清單。

    worker_id 撞號時**回報端優先**:hook 回報的是第一手事實(有 label、有精確的
    起訖),投影只是從 provider 現成記錄推出來的二手視圖。
    """
    seen = {w.get("worker_id") for w in reported}
    out = list(reported)
    out.extend(w for w in (projected or []) if w.get("worker_id") not in seen)
    # 次鍵 worker_id:投影出來的工人(hermes SUBSESSIONS 沒有真正的開工時間,
    # 只能拿 lastAt 當 started_ts)在連續 poll 之間 started_ts 會微幅移動,
    # 單鍵排序會讓清單在 app 上無故重排。次鍵讓順序穩定。
    out.sort(key=lambda w: (w.get("started_ts") or 0, str(w.get("worker_id") or "")))
    return out
