"""Agent Registry — session 戶政系統(藍圖 AGENT_INTEROP_BLUEPRINT §3)。

善彰的痛點:「子程序不知道怎麼管理,常跑一堆出來,session 管理混亂」——
spawn 有生無滅:沒有出生登記、沒有壽命、沒有收屍。這個模組是治理層的
資料面:每個 session 的出生登記(purpose/class/parent)、生命週期狀態、
TTL 與配額。**純資料 + 純同步**,不 import bridge、不碰任何 provider;
收屍(tmux kill / worktree remove / codex archive)是 bridge 側 reaper 的事,
這裡只負責記帳與挑候選人。

儲存:獨立 sqlite(預設 ~/.pocket/agent-registry.db,env POCKET_REGISTRY_DB
可覆寫)。**絕不**寫 production 的 canonical.db / state.db —— registry 掛了
最多是治理失憶,聊天資料零風險。

治理是 opt-in:只有「經 bridge 創建路徑登記」的 session(registered=1)受
生命週期管理;既有/旁路 session 在 registry 視圖標 registered=false,
reaper 永遠不碰(善彰的活水道零風險)。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

# class → 預設 TTL(秒)。persistent 無壽命(None);env 可調。
CLASSES = ("persistent", "task", "ephemeral")
STATES = ("active", "idle", "done", "archived")

DEFAULT_PURPOSE = "未註明用途"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


class QuotaExceeded(Exception):
    """配額超限 —— reason 是給人看的 zh-TW 一句話(藍圖 §3.3:超額直接拒,
    附人話原因,不排隊)。bridge 側轉成 HTTP 429。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class TeamActiveExists(Exception):
    """該 lead 已有 active team(Cindy uniq_active_team_per_lead)。
    `.team_id` 帶既有那隊的 id —— bridge 回 409 時附給 caller,lead 直接
    沿用即可,不必瞎猜自己上一隊叫什麼。"""

    def __init__(self, team_id: str):
        super().__init__(f"active team exists: {team_id}")
        self.team_id = team_id


class WorkerLabelTaken(Exception):
    """label 在該 team 已被占用(label 每隊唯一 —— lead 用 label 指人,
    重名 = 指令歧義,結構上禁止)。"""


class AgentRegistry:
    """sqlite-backed session 戶口名簿。執行緒安全(單一 Lock 串行化;
    量小到不值得更細的鎖)。所有時間都是 epoch 秒。"""

    def __init__(self, db_path: str, *,
                 task_ttl: float | None = None,
                 ephemeral_ttl: float | None = None,
                 max_children: int | None = None,
                 task_cap: int | None = None,
                 max_depth: int | None = None,
                 idle_secs: float | None = None):
        self.db_path = os.path.expanduser(db_path)
        self.task_ttl = task_ttl if task_ttl is not None \
            else _env_float("REGISTRY_TASK_TTL", 86400.0)
        self.ephemeral_ttl = ephemeral_ttl if ephemeral_ttl is not None \
            else _env_float("REGISTRY_EPHEMERAL_TTL", 7200.0)
        self.max_children = max_children if max_children is not None \
            else _env_int("REGISTRY_MAX_CHILDREN", 3)
        self.task_cap = task_cap if task_cap is not None \
            else _env_int("REGISTRY_TASK_CAP", 12)
        self.max_depth = max_depth if max_depth is not None \
            else _env_int("REGISTRY_MAX_DEPTH", 2)
        self.idle_secs = idle_secs if idle_secs is not None \
            else _env_float("REGISTRY_IDLE_SECS", 600.0)
        self._lock = threading.Lock()
        self._init_db()

    # ── 存儲 ────────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        con = self._connect()
        try:
            con.execute("""CREATE TABLE IF NOT EXISTS sessions(
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                purpose TEXT NOT NULL DEFAULT '',
                class TEXT NOT NULL DEFAULT 'task',
                parent TEXT,
                state TEXT NOT NULL DEFAULT 'active',
                depth INTEGER NOT NULL DEFAULT 0,
                created_ts REAL NOT NULL,
                last_active_ts REAL NOT NULL,
                ttl_secs REAL,
                max_children INTEGER,
                registered INTEGER NOT NULL DEFAULT 1,
                worktree TEXT,
                archived_ts REAL,
                archive_reason TEXT,
                meta TEXT NOT NULL DEFAULT '{}')""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_reg_parent ON sessions(parent)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_reg_state ON sessions(state)")
            # agent_call(1c)的 call 帳本:誰調了誰、chain 家譜(root/parent/
            # depth)、結果。與 sessions 同庫 —— Pocket 編隊視圖一次查得到
            # session 樹 + call 鏈;絕不碰 canonical.db/state.db。
            con.execute("""CREATE TABLE IF NOT EXISTS agent_calls(
                id TEXT PRIMARY KEY,
                caller TEXT NOT NULL,
                target TEXT NOT NULL,
                mode TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'running',
                reply TEXT,
                error TEXT,
                root_call_id TEXT NOT NULL,
                parent_call_id TEXT,
                depth INTEGER NOT NULL DEFAULT 1,
                created_ts REAL NOT NULL,
                updated_ts REAL NOT NULL,
                finished_ts REAL,
                meta TEXT NOT NULL DEFAULT '{}')""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_ac_target ON agent_calls(target)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_ac_caller ON agent_calls(caller)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_ac_root ON agent_calls(root_call_id)")
            # lead 編隊(第四刀,Cindy/Orca 對照 2026-08-16):team/worker 的
            # TOP half 資料面。worker 的 session 本體仍在 sessions 表(spawn
            # 走既有派工路徑、配額照 precheck),這兩張表只管「誰的隊、
            # 誰是誰、現在誰在跑」。與 agent_calls 同庫 —— worker 狀態由
            # call 生命週期驅動(meta.team_worker_id 戳記),一庫查得齊。
            con.execute("""CREATE TABLE IF NOT EXISTS agent_teams(
                id TEXT PRIMARY KEY,
                lead TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_ts REAL NOT NULL,
                ended_ts REAL)""")
            # Cindy uniq_active_team_per_lead:一個 lead 同時只能有一個
            # active team。partial unique index 是「先查再插」之外的最後一道
            # 防線 —— 併發 start 也生不出第二隊。
            con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS
                uniq_active_team_per_lead ON agent_teams(lead)
                WHERE status='active'""")
            con.execute("""CREATE TABLE IF NOT EXISTS agent_workers(
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'idle',
                last_call_id TEXT,
                created_ts REAL NOT NULL,
                updated_ts REAL NOT NULL,
                UNIQUE(team_id, label))""")
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_aw_team ON agent_workers(team_id)")
            con.commit()
        finally:
            con.close()

    # ── 讀取輔助 ────────────────────────────────────────────────────────
    def default_ttl(self, cls: str) -> float | None:
        if cls == "task":
            return self.task_ttl
        if cls == "ephemeral":
            return self.ephemeral_ttl
        return None    # persistent:無壽命

    @staticmethod
    def _row_dict(row) -> dict:
        d = dict(row)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except ValueError:
            d["meta"] = {}
        d["registered"] = bool(d.get("registered"))
        return d

    def expires_ts(self, row: dict) -> float | None:
        """到期時刻 = last_active + ttl。persistent(ttl None)永不到期。"""
        ttl = row.get("ttl_secs")
        if ttl is None or row.get("class") == "persistent":
            return None
        return float(row["last_active_ts"]) + float(ttl)

    def effective_state(self, row: dict, now: float | None = None) -> str:
        """讀取時計算的呈現狀態:archived/done 照存;其餘依活動時間折算
        active⇄idle(藍圖 §3.2:無 turn 超過 idle_secs → idle)。"""
        now = time.time() if now is None else now
        st = row.get("state") or "active"
        if st in ("archived", "done"):
            return st
        if now - float(row.get("last_active_ts") or 0) > self.idle_secs:
            return "idle"
        return "active"

    # ── 出生登記 ─────────────────────────────────────────────────────────
    def precheck(self, parent: str | None, cls: str) -> int:
        """配額前檢(藍圖 §3.3)——在真正 spawn(codex thread/start、ccsess
        new…)**之前**呼叫,超額就地拒絕,不留半個孤兒。回傳新 session 的
        depth。persistent(白名單常駐)不受配額限制。"""
        with self._lock:
            return self._precheck_locked(parent, cls)

    def _precheck_locked(self, parent: str | None, cls: str) -> int:
        con = self._connect()
        try:
            depth = 0
            if parent:
                prow = con.execute(
                    "SELECT * FROM sessions WHERE id=?", (parent,)).fetchone()
                if prow is not None:
                    depth = int(prow["depth"]) + 1
            if cls == "persistent":
                return depth
            if depth > self.max_depth:
                raise QuotaExceeded(
                    f"超過配額:派生深度已達上限 {self.max_depth} 層"
                    f"(A→B→C 止步),不可再往下生子 session")
            if parent:
                n = con.execute(
                    "SELECT COUNT(*) FROM sessions WHERE parent=? "
                    "AND state != 'archived' AND registered=1",
                    (parent,)).fetchone()[0]
                cap = self._parent_max_children(con, parent)
                if n >= cap:
                    raise QuotaExceeded(
                        f"超過配額:{parent} 名下已有 {n} 個未歸檔子 session"
                        f"(上限 {cap});請先歸檔或 🧹收工 再派新工")
            if cls == "task":
                n = con.execute(
                    "SELECT COUNT(*) FROM sessions WHERE class='task' "
                    "AND state != 'archived' AND registered=1").fetchone()[0]
                if n >= self.task_cap:
                    raise QuotaExceeded(
                        f"超過配額:全域 task 類 session 已達 {n} 個"
                        f"(上限 {self.task_cap});請先 🧹收工 釋放額度")
            return depth
        finally:
            con.close()

    def _parent_max_children(self, con, parent_id: str) -> int:
        row = con.execute("SELECT max_children FROM sessions WHERE id=?",
                          (parent_id,)).fetchone()
        if row is not None and row["max_children"]:
            return int(row["max_children"])
        return self.max_children

    def register(self, sid: str, *, provider: str, name: str = "",
                 purpose: str = "", cls: str = "task",
                 parent: str | None = None, ttl_secs: float | None = None,
                 worktree: str | None = None, registered: bool = True,
                 meta: dict | None = None, enforce_quota: bool = True) -> dict:
        """出生登記。已存在同 id → 冪等回傳既有戶口(persona 每次開機
        re-register 不炸)。enforce_quota=False 給「spawn 已發生、只補記帳」
        的路徑(如 codex thread 已 start)——配額該在 spawn 前用 precheck。"""
        if cls not in CLASSES:
            cls = "task"
        purpose = (purpose or "").strip() or DEFAULT_PURPOSE
        now = time.time()
        with self._lock:
            con = self._connect()
            try:
                existing = con.execute(
                    "SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
                if existing is not None:
                    return self._row_dict(existing)
                depth = 0
                if enforce_quota and registered:
                    depth = self._precheck_locked(parent, cls)
                elif parent:
                    prow = con.execute("SELECT depth FROM sessions WHERE id=?",
                                       (parent,)).fetchone()
                    depth = int(prow["depth"]) + 1 if prow is not None else 0
                if ttl_secs is None:
                    ttl_secs = self.default_ttl(cls)
                con.execute(
                    """INSERT INTO sessions(id, provider, name, purpose, class,
                       parent, state, depth, created_ts, last_active_ts,
                       ttl_secs, max_children, registered, worktree, meta)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sid, provider, name, purpose, cls, parent or None,
                     "active", depth, now, now, ttl_secs, None,
                     1 if registered else 0, worktree,
                     json.dumps(meta or {}, ensure_ascii=False)))
                con.commit()
                return self.get(sid) or {}
            finally:
                con.close()

    # ── 收編 / 釋放(全機發現面 §2.3 的狀態機)────────────────────────────
    def adopt(self, sid: str, *, provider: str, name: str = "",
              purpose: str = "", cls: str | None = None,
              parent: str | None = None, worktree: str | None = None,
              meta: dict | None = None) -> dict:
        """把「發現但未管」的 session 轉成 registered=1 的正式戶口。

        三種入口一律回同一形狀(冪等,重複收編不炸):
        - 沒戶口 → 新登記。**不套配額**:行程早就在跑了,配額擋下來只會
          留下一個「看得到、管不到」的孤兒,與收編的目的正好相反。
        - 有戶口但 registered=0(legacy/旁路)→ 原地轉正,補 purpose/class/TTL。
        - 已 registered=1 → 只補有給的欄位,不動出生時間。

        `state='archived'` 的戶口遇到收編會復活:人明確表示這條還活著、
        要管它,記帳面沒有理由繼續當它是死的。
        """
        if cls is not None and cls not in CLASSES:
            cls = "task"
        now = time.time()
        with self._lock:
            con = self._connect()
            try:
                row = con.execute("SELECT * FROM sessions WHERE id=?",
                                  (sid,)).fetchone()
                if row is None:
                    eff_cls = cls or "task"
                    depth = 0
                    if parent:
                        prow = con.execute(
                            "SELECT depth FROM sessions WHERE id=?",
                            (parent,)).fetchone()
                        depth = int(prow["depth"]) + 1 if prow is not None else 0
                    m = dict(meta or {})
                    m.setdefault("adopted_ts", now)
                    con.execute(
                        """INSERT INTO sessions(id, provider, name, purpose,
                           class, parent, state, depth, created_ts,
                           last_active_ts, ttl_secs, max_children, registered,
                           worktree, meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (sid, provider, name,
                         (purpose or "").strip() or DEFAULT_PURPOSE, eff_cls,
                         parent or None, "active", depth, now, now,
                         self.default_ttl(eff_cls), None, 1, worktree,
                         json.dumps(m, ensure_ascii=False)))
                    con.commit()
                else:
                    d = self._row_dict(row)
                    sets = ["registered=1", "last_active_ts=?"]
                    args: list = [now]
                    if d.get("state") == "archived":
                        sets += ["state='active'", "archived_ts=NULL",
                                 "archive_reason=NULL"]
                    if name and not d.get("name"):
                        sets.append("name=?")
                        args.append(name)
                    if (purpose or "").strip():
                        sets.append("purpose=?")
                        args.append(purpose.strip())
                    if cls is not None:
                        sets += ["class=?", "ttl_secs=?"]
                        args += [cls, self.default_ttl(cls)]
                    elif not d.get("registered") and d.get("ttl_secs") is None \
                            and (d.get("class") or "task") != "persistent":
                        # 轉正的 legacy 戶口沒有壽命 → 補上該班別的預設
                        sets.append("ttl_secs=?")
                        args.append(self.default_ttl(d.get("class") or "task"))
                    if worktree and not d.get("worktree"):
                        sets.append("worktree=?")
                        args.append(worktree)
                    m = dict(d.get("meta") or {})
                    m.setdefault("adopted_ts", now)
                    if meta:
                        m.update(meta)
                    sets.append("meta=?")
                    args.append(json.dumps(m, ensure_ascii=False))
                    args.append(sid)
                    con.execute(
                        f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", args)
                    con.commit()
                fresh = con.execute("SELECT * FROM sessions WHERE id=?",
                                    (sid,)).fetchone()
                return self._row_dict(fresh)
            finally:
                con.close()

    def release(self, sid: str) -> dict | None:
        """收編的逆操作:registered=0。**戶口留著**(歷史/家譜不消失),
        而 registered=0 本身就是 reaper 的免疫標記(`sweep_candidates` 第一
        條就是跳過未登記)——釋放後這條再也不會被自動收屍。"""
        with self._lock:
            con = self._connect()
            try:
                row = con.execute("SELECT id FROM sessions WHERE id=?",
                                  (sid,)).fetchone()
                if row is None:
                    return None
                con.execute("UPDATE sessions SET registered=0 WHERE id=?", (sid,))
                con.commit()
                fresh = con.execute("SELECT * FROM sessions WHERE id=?",
                                    (sid,)).fetchone()
                return self._row_dict(fresh)
            finally:
                con.close()

    # ── 日常記帳 ─────────────────────────────────────────────────────────
    def get(self, sid: str) -> dict | None:
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM sessions WHERE id=?",
                              (sid,)).fetchone()
            return self._row_dict(row) if row is not None else None
        finally:
            con.close()

    def touch(self, sid: str) -> None:
        """有 turn 活動 → 更新 last_active_ts + 回 active(archived 不復活;
        未登記 id 靜默略過——touch 掛在輸入熱路徑上,不能因記帳丟例外)。"""
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    "UPDATE sessions SET last_active_ts=?, "
                    "state = CASE WHEN state IN ('idle','done') THEN 'active' "
                    "ELSE state END WHERE id=? AND state != 'archived'",
                    (time.time(), sid))
                con.commit()
            finally:
                con.close()

    def set_worktree(self, sid: str, worktree: str) -> None:
        """spawn 時記下 worktree 實際路徑 —— reaper 只收「登記過路徑」的
        worktree,絕不用猜的(藍圖 §3.2 GC 連帶收乾淨的安全前提)。"""
        with self._lock:
            con = self._connect()
            try:
                con.execute("UPDATE sessions SET worktree=? WHERE id=?",
                            (worktree, sid))
                con.commit()
            finally:
                con.close()

    def update(self, sid: str, *, purpose: str | None = None,
               cls: str | None = None,
               ttl_extend_secs: float | None = None) -> dict | None:
        """續命/改班(POST /app/v2/registry/{id})。ttl_extend_secs:保證
        「從現在起至少再活 N 秒」——新到期 = max(原到期, now) + N。"""
        with self._lock:
            con = self._connect()
            try:
                row = con.execute("SELECT * FROM sessions WHERE id=?",
                                  (sid,)).fetchone()
                if row is None:
                    return None
                d = self._row_dict(row)
                sets, args = [], []
                if purpose is not None and purpose.strip():
                    sets.append("purpose=?")
                    args.append(purpose.strip())
                if cls is not None and cls in CLASSES and cls != d["class"]:
                    sets.append("class=?")
                    args.append(cls)
                    # 改班 → TTL 跟著班別走:persistent 摘壽命;task/ephemeral
                    # 從 persistent 轉回來時補預設壽命。
                    sets.append("ttl_secs=?")
                    args.append(self.default_ttl(cls))
                    d["class"], d["ttl_secs"] = cls, self.default_ttl(cls)
                if ttl_extend_secs is not None and ttl_extend_secs > 0 \
                        and d["class"] != "persistent":
                    now = time.time()
                    cur_exp = self.expires_ts(d)
                    new_exp = max(cur_exp or now, now) + float(ttl_extend_secs)
                    sets.append("ttl_secs=?")
                    args.append(new_exp - float(d["last_active_ts"]))
                if sets:
                    args.append(sid)
                    con.execute(
                        f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", args)
                    con.commit()
                fresh = con.execute("SELECT * FROM sessions WHERE id=?",
                                    (sid,)).fetchone()
                return self._row_dict(fresh)
            finally:
                con.close()

    def archive(self, sid: str, reason: str = "manual") -> dict | None:
        """記帳面歸檔(destructive teardown 是 bridge reaper 的事)。"""
        with self._lock:
            con = self._connect()
            try:
                row = con.execute("SELECT * FROM sessions WHERE id=?",
                                  (sid,)).fetchone()
                if row is None:
                    return None
                if row["state"] != "archived":
                    con.execute(
                        "UPDATE sessions SET state='archived', archived_ts=?, "
                        "archive_reason=? WHERE id=?",
                        (time.time(), reason, sid))
                    con.commit()
                fresh = con.execute("SELECT * FROM sessions WHERE id=?",
                                    (sid,)).fetchone()
                return self._row_dict(fresh)
            finally:
                con.close()

    def mark_done(self, sid: str) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute("UPDATE sessions SET state='done' "
                            "WHERE id=? AND state != 'archived'", (sid,))
                con.commit()
            finally:
                con.close()

    # ── 視圖與收屍候選 ───────────────────────────────────────────────────
    def list_rows(self, include_archived: bool = False) -> list[dict]:
        con = self._connect()
        try:
            q = "SELECT * FROM sessions"
            if not include_archived:
                q += " WHERE state != 'archived'"
            q += " ORDER BY created_ts"
            return [self._row_dict(r) for r in con.execute(q).fetchall()]
        finally:
            con.close()

    def children_ids(self, rows: list[dict]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in rows:
            p = r.get("parent")
            if p:
                out.setdefault(p, []).append(r["id"])
        return out

    def is_orphan(self, row: dict, by_id: dict[str, dict]) -> bool:
        """孤兒 = parent 已 archived(或戶口已消失)但自己還沒歸檔
        (藍圖 §3.4 標紅提醒)。無 parent(人手動/常駐)不算。"""
        p = row.get("parent")
        if not p or row.get("state") == "archived":
            return False
        prow = by_id.get(p) or self.get(p)
        return prow is None or prow.get("state") == "archived"

    def sweep_candidates(self, now: float | None = None, *,
                         require_expired: bool = True) -> list[dict]:
        """收屍候選:**只看 registered=1**(opt-in 治理;legacy 免疫)、
        只收 task/ephemeral(persistent 永不自動收)、目前非 active。
        require_expired=True(reaper):idle **且** TTL 到期才收;
        require_expired=False(🧹收工 sweep):idle 即收,不等 TTL。"""
        now = time.time() if now is None else now
        out = []
        for r in self.list_rows():
            if not r.get("registered"):
                continue                      # 鐵律:未登記絕不收
            if r.get("class") not in ("task", "ephemeral"):
                continue                      # persistent 永不自動歸檔
            eff = self.effective_state(r, now)
            if eff == "active":
                continue
            if require_expired and eff != "done":
                exp = self.expires_ts(r)
                if exp is None or now < exp:
                    continue                  # 還沒到期,再等等
            out.append(r)
        return out

    # ── agent_call 帳本(1c:互調的 call 列,chain 家譜可查)────────────────
    @staticmethod
    def _call_row_dict(row) -> dict:
        d = dict(row)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except ValueError:
            d["meta"] = {}
        return d

    # 「未結案」的 call 狀態(closure 契約,2026-08-16):
    #   dispatching = 帳已落、send 還沒被目標接受(accepted 前絕不標 running)
    #   running     = send 已 accepted,收割人掛著等回覆
    # 其餘一律視為終態,寫入 finished_ts。bridge 重啟時要對這兩種做對帳
    # (見 bridge._agent_call_reconcile),否則收割人只活在記憶體,call 會
    # 永遠卡在看板上冒充進行中。
    CALL_UNFINISHED = ("running", "dispatching")

    def call_create(self, call_id: str, *, caller: str, target: str,
                    mode: str, message: str = "", status: str = "running",
                    root_call_id: str | None = None,
                    parent_call_id: str | None = None, depth: int = 1,
                    error: str | None = None, meta: dict | None = None) -> dict:
        """落一筆 call。root_call_id 缺省 = 自己是 root(chain 家譜起點)。
        message 只留前 2000 字(帳本要能追責,但不是聊天備份)。"""
        now = time.time()
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    """INSERT INTO agent_calls(id, caller, target, mode, message,
                       status, error, root_call_id, parent_call_id, depth,
                       created_ts, updated_ts, finished_ts, meta)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (call_id, caller, target, mode, (message or "")[:2000],
                     status, error, root_call_id or call_id,
                     parent_call_id or None, int(depth), now, now,
                     now if status not in self.CALL_UNFINISHED else None,
                     json.dumps(meta or {}, ensure_ascii=False)))
                con.commit()
            finally:
                con.close()
        return self.call_get(call_id) or {}

    def call_list_unfinished(self) -> list[dict]:
        """重啟對帳用:所有還沒結案的 call(dispatching/running)。"""
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    "SELECT * FROM agent_calls WHERE status IN (?,?) "
                    "ORDER BY created_ts",
                    self.CALL_UNFINISHED).fetchall()
                return [self._call_row_dict(r) for r in rows]
            finally:
                con.close()

    def call_update(self, call_id: str, *, status: str | None = None,
                    reply: str | None = None, error: str | None = None,
                    meta_merge: dict | None = None) -> dict | None:
        with self._lock:
            con = self._connect()
            try:
                row = con.execute("SELECT * FROM agent_calls WHERE id=?",
                                  (call_id,)).fetchone()
                if row is None:
                    return None
                d = self._call_row_dict(row)
                sets, args = ["updated_ts=?"], [time.time()]
                # 終態不被覆蓋:call 一旦結案(done/timeout/error/…),晚到的
                # 結算(重啟對帳 vs 記憶體收割人的競態)不得改寫 status ——
                # 先到的終態贏。reply/error/meta 照常可補(晚到的收割可能
                # 帶著更完整的回覆文字)。
                if status is not None and d["status"] not in self.CALL_UNFINISHED:
                    status = None
                if status is not None:
                    sets.append("status=?")
                    args.append(status)
                    if (status not in self.CALL_UNFINISHED
                            and d.get("finished_ts") is None):
                        sets.append("finished_ts=?")
                        args.append(time.time())
                if reply is not None:
                    sets.append("reply=?")
                    args.append(reply)
                if error is not None:
                    sets.append("error=?")
                    args.append(error)
                if meta_merge:
                    m = dict(d.get("meta") or {})
                    m.update(meta_merge)
                    sets.append("meta=?")
                    args.append(json.dumps(m, ensure_ascii=False))
                args.append(call_id)
                con.execute(f"UPDATE agent_calls SET {', '.join(sets)} WHERE id=?",
                            args)
                con.commit()
                fresh = con.execute("SELECT * FROM agent_calls WHERE id=?",
                                    (call_id,)).fetchone()
                return self._call_row_dict(fresh)
            finally:
                con.close()

    def call_get(self, call_id: str) -> dict | None:
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM agent_calls WHERE id=?",
                              (call_id,)).fetchone()
            return self._call_row_dict(row) if row is not None else None
        finally:
            con.close()

    def call_list(self, session: str | None = None, root: str | None = None,
                  limit: int = 50) -> list[dict]:
        """查帳:某 session 參與的 call(caller 或 target)/ 某 chain 全列。
        新→舊(Pocket 呼叫鏈視圖的資料源)。"""
        con = self._connect()
        try:
            q, args = "SELECT * FROM agent_calls", []
            conds = []
            if session:
                conds.append("(caller=? OR target=?)")
                args += [session, session]
            if root:
                conds.append("root_call_id=?")
                args.append(root)
            if conds:
                q += " WHERE " + " AND ".join(conds)
            q += " ORDER BY created_ts DESC LIMIT ?"
            args.append(max(1, int(limit)))
            return [self._call_row_dict(r) for r in con.execute(q, args)]
        finally:
            con.close()

    def call_active_for_target(self, target: str,
                               recent_secs: float = 600.0) -> dict | None:
        """chain 推斷:現在有沒有 call 正打在 `target` 身上?有 → target 發出
        的新 call 視為同 chain 的下一層。running 恆算;sent/done(fire_and_
        forget 或剛收割完)在 recent_secs 回看窗內也算 —— 投遞完成不代表
        目標的 turn 結束,窗內的外呼大概率仍是奉命行事。"""
        con = self._connect()
        try:
            row = con.execute(
                "SELECT * FROM agent_calls WHERE target=? AND ("
                " status='running' OR"
                " (status IN ('sent','done') AND updated_ts >= ?))"
                " ORDER BY created_ts DESC LIMIT 1",
                (target, time.time() - float(recent_secs))).fetchone()
            return self._call_row_dict(row) if row is not None else None
        finally:
            con.close()

    def call_ancestors(self, call_row: dict, max_hops: int = 12) -> list[dict]:
        """含自身往上追整條 chain(新→舊)。斷鏈/超 hop 就停,絕不無窮迴圈。"""
        out, cur, seen = [], call_row, set()
        while cur is not None and len(out) < max_hops:
            cid = str(cur.get("id") or "")
            if not cid or cid in seen:
                break
            seen.add(cid)
            out.append(cur)
            pid = str(cur.get("parent_call_id") or "")
            cur = self.call_get(pid) if pid else None
        return out

    def call_chain_size(self, root_call_id: str) -> int:
        con = self._connect()
        try:
            return int(con.execute(
                "SELECT COUNT(*) FROM agent_calls WHERE root_call_id=?"
                " AND status != 'denied'", (root_call_id,)).fetchone()[0])
        finally:
            con.close()

    # ── lead 編隊(agent_team,第四刀)──────────────────────────────────────
    # worker 狀態機(Cindy updateWorkerStatus 語意):
    #   idle → running:派單被 v2_agent_call 落帳時綁上 last_call_id
    #   running → done|error:**只有**綁著的那顆 call 結案才能寫(CAS);
    #       晚到/過期 call 的結算絕不覆蓋新任務的狀態(call_update
    #       終態贏的同款鐵律)
    #   running → idle:dispatch 失敗回滾 —— 同樣只有綁著的 call 能回滾,
    #       且終態不被回滾覆蓋
    WORKER_STATUSES = ("idle", "running", "done", "error")
    WORKER_TERMINAL = ("done", "error")

    def team_active(self, lead: str) -> dict | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT * FROM agent_teams WHERE lead=? AND status='active'",
                (lead,)).fetchone()
            return dict(row) if row is not None else None
        finally:
            con.close()

    def team_get(self, team_id: str) -> dict | None:
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM agent_teams WHERE id=?",
                              (team_id,)).fetchone()
            return dict(row) if row is not None else None
        finally:
            con.close()

    def team_start(self, team_id: str, lead: str) -> dict:
        """開隊。已有 active team → TeamActiveExists(帶既有 id)。
        先查再插 + partial unique index 雙保險(競態下 IntegrityError 也
        轉成同一個例外,呼叫端只需要懂一種拒絕)。"""
        now = time.time()
        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    "SELECT id FROM agent_teams WHERE lead=? AND status='active'",
                    (lead,)).fetchone()
                if row is not None:
                    raise TeamActiveExists(row["id"])
                try:
                    con.execute(
                        "INSERT INTO agent_teams(id, lead, status, created_ts)"
                        " VALUES(?,?,'active',?)", (team_id, lead, now))
                    con.commit()
                except sqlite3.IntegrityError:
                    row = con.execute(
                        "SELECT id FROM agent_teams WHERE lead=? AND"
                        " status='active'", (lead,)).fetchone()
                    raise TeamActiveExists(row["id"] if row is not None
                                           else "(unknown)")
                fresh = con.execute("SELECT * FROM agent_teams WHERE id=?",
                                    (team_id,)).fetchone()
                return dict(fresh)
            finally:
                con.close()

    def team_end(self, team_id: str) -> dict | None:
        """收隊(記帳面)。worker 的 session 一律不動 —— 銷毀是人的決定,
        走既有 sweep/archive 路徑。冪等:已 ended 的再 end 不改 ended_ts。"""
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    "UPDATE agent_teams SET status='ended', ended_ts=?"
                    " WHERE id=? AND status='active'", (time.time(), team_id))
                con.commit()
                fresh = con.execute("SELECT * FROM agent_teams WHERE id=?",
                                    (team_id,)).fetchone()
                return dict(fresh) if fresh is not None else None
            finally:
                con.close()

    def worker_add(self, worker_id: str, *, team_id: str, session_id: str,
                   role: str = "", label: str = "") -> dict:
        """worker 落籍(session 已由既有派工路徑 spawn 完)。label 每隊唯一
        —— 先查再插 + UNIQUE(team_id,label) 雙保險。"""
        now = time.time()
        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    "SELECT id FROM agent_workers WHERE team_id=? AND label=?",
                    (team_id, label)).fetchone()
                if row is not None:
                    raise WorkerLabelTaken(label)
                try:
                    con.execute(
                        """INSERT INTO agent_workers(id, team_id, session_id,
                           role, label, status, created_ts, updated_ts)
                           VALUES(?,?,?,?,?,'idle',?,?)""",
                        (worker_id, team_id, session_id, role, label, now, now))
                    con.commit()
                except sqlite3.IntegrityError:
                    raise WorkerLabelTaken(label)
                fresh = con.execute("SELECT * FROM agent_workers WHERE id=?",
                                    (worker_id,)).fetchone()
                return dict(fresh)
            finally:
                con.close()

    def worker_get(self, worker_id: str) -> dict | None:
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM agent_workers WHERE id=?",
                              (worker_id,)).fetchone()
            return dict(row) if row is not None else None
        finally:
            con.close()

    def worker_by_label(self, team_id: str, label: str) -> dict | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT * FROM agent_workers WHERE team_id=? AND label=?",
                (team_id, label)).fetchone()
            return dict(row) if row is not None else None
        finally:
            con.close()

    def worker_list(self, team_id: str) -> list[dict]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT * FROM agent_workers WHERE team_id=?"
                " ORDER BY created_ts", (team_id,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def worker_mark_running(self, worker_id: str, call_id: str) -> dict | None:
        """派單落帳 → worker 綁上這顆 call 並轉 running。新 call 允許把
        done/error 的 worker 重新拉上工(新任務就是新一輪);唯一不放行:
        **同一顆** call 已把 worker 結成終態(收割人比 HTTP 路徑先跑完的
        競態)—— 終態贏,不得倒退。"""
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    "UPDATE agent_workers SET status='running',"
                    " last_call_id=?, updated_ts=? WHERE id=?"
                    " AND NOT (last_call_id=? AND status IN ('done','error'))",
                    (call_id, time.time(), worker_id, call_id))
                con.commit()
            finally:
                con.close()
        return self.worker_get(worker_id)

    def worker_note_settled(self, worker_id: str, call_id: str,
                            status: str) -> dict | None:
        """call 結案 → worker 狀態跟著走(done/error)。CAS:只有「這顆
        call 仍是 worker 目前綁著的 call、且 worker 還在 running」才寫 ——
        晚到的重複結算與過期 call 都寫不進來(終態不被覆蓋)。"""
        if status not in ("done", "error"):
            return self.worker_get(worker_id)
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    "UPDATE agent_workers SET status=?, updated_ts=?"
                    " WHERE id=? AND last_call_id=? AND status='running'",
                    (status, time.time(), worker_id, call_id))
                con.commit()
            finally:
                con.close()
        return self.worker_get(worker_id)

    def worker_rollback_idle(self, worker_id: str, call_id: str) -> dict | None:
        """dispatch 失敗回滾:回到派發前(idle)。同樣 CAS —— 只有這顆 call
        標上的 running 才退;終態/新 call 的狀態絕不被回滾覆蓋
        (closure 契約「回滾不得覆蓋已有終態」的 worker 版)。"""
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    "UPDATE agent_workers SET status='idle', updated_ts=?"
                    " WHERE id=? AND last_call_id=? AND status='running'",
                    (time.time(), worker_id, call_id))
                con.commit()
            finally:
                con.close()
        return self.worker_get(worker_id)
