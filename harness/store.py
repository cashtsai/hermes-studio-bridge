"""Harness 四庫(Memory / Skill / Prompt / Subagent-route)的 sqlite 存儲。

藍圖 §2:「軌跡回寫四庫」。每一列都是**版本化的提案**,帶狀態機:

    proposed ──approve──▶ approved ──▶ active ──(同 key 新版上線)──▶ superseded
        └────reject────▶ rejected

`approve()` 一次走完 approved→active(善彰按下去就是要生效),但兩段轉換
都會過狀態機檢查、都留時間戳 —— 稽核時看得出「誰在什麼時候讓它生效」。

## 紅線

- **只寫自己的 DB**(預設 `~/.pocket/harness.db`,env `HARNESS_DB`)。
  production 的 canonical.db / state.db 一律唯讀,連 import 都不碰。
  harness 整個炸掉,最壞就是少一晚提案,聊天資料零風險。
- **沒有任何自動 approve 路徑**。這個模組不提供「條件成熟就自己生效」的
  API;`approve()` 一定要有 `by`(誰批的),而 bridge 端只在人打 HTTP 才叫。

## 為什麼是四張表而不是一張帶 kind 的表

四庫的 payload 形狀差很多(skill 有步驟列、prompt 有節點歸屬、route 有成功
率統計)。塞進一張表就得全靠 JSON blob,查詢/約束全失守。共用欄位由
`_COMMON_COLS` 統一產生,所以四張表的 DDL 與 CRUD 其實只寫了一遍。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time

from harness.trajectory import redact_text

DEFAULT_DB = "~/.pocket/harness.db"

# store 名 → (id 前綴, 該庫專屬欄位的 DDL)
STORES: dict = {
    # 學到的耐久事實。scope=global(跨節點共用)或 node:<sid>(只屬這個節點)
    "memory": ("mem", (
        ("fact", "TEXT NOT NULL DEFAULT ''"),
        ("tags", "TEXT NOT NULL DEFAULT '[]'"),
    )),
    # 蒸餾出來的可重複程序(CC 的 skills 機制未來可直接掛)
    "skill": ("skl", (
        ("name", "TEXT NOT NULL DEFAULT ''"),
        ("when_to_use", "TEXT NOT NULL DEFAULT ''"),
        ("steps", "TEXT NOT NULL DEFAULT '[]'"),
    )),
    # 給某節點的 append_system_prompt 片段 —— 核准後直接寫進 spawn-config pin
    "prompt": ("prm", (
        ("node", "TEXT NOT NULL DEFAULT ''"),
        ("provider", "TEXT NOT NULL DEFAULT ''"),
        ("fragment", "TEXT NOT NULL DEFAULT ''"),
    )),
    # 任務類型 → 歷史上誰做得成(未來餵給 agent_call 路由)
    "subagent_route": ("rte", (
        ("task_kind", "TEXT NOT NULL DEFAULT ''"),
        ("target", "TEXT NOT NULL DEFAULT ''"),
        ("success_n", "INTEGER NOT NULL DEFAULT 0"),
        ("sample_n", "INTEGER NOT NULL DEFAULT 0"),
    )),
}

STATES = ("proposed", "approved", "rejected", "active", "superseded")

# 合法轉換。刻意**不放** proposed→active:任何生效都必須先經過 approved,
# 也就是必須有人按過。少一條邊,就少一個「不小心自動上線」的可能。
_TRANSITIONS = {
    ("proposed", "approved"),
    ("proposed", "rejected"),
    ("approved", "active"),
    ("approved", "rejected"),
    ("active", "rejected"),
    ("active", "superseded"),
}

_COMMON_COLS = (
    ("id", "TEXT PRIMARY KEY"),
    ("store", "TEXT NOT NULL"),
    ("scope", "TEXT NOT NULL DEFAULT 'global'"),
    ("key", "TEXT NOT NULL"),
    ("version", "INTEGER NOT NULL DEFAULT 1"),
    ("state", "TEXT NOT NULL DEFAULT 'proposed'"),
    ("rationale", "TEXT NOT NULL DEFAULT ''"),
    ("evidence", "TEXT NOT NULL DEFAULT '[]'"),
    ("preview", "TEXT NOT NULL DEFAULT ''"),
    ("created_ts", "REAL NOT NULL"),
    ("updated_ts", "REAL NOT NULL"),
    ("decided_ts", "REAL"),
    ("decided_by", "TEXT"),
    ("supersedes", "TEXT"),
    ("applied", "INTEGER NOT NULL DEFAULT 0"),
    ("apply_note", "TEXT NOT NULL DEFAULT ''"),
    ("meta", "TEXT NOT NULL DEFAULT '{}'"),
)

_JSON_COLS = ("evidence", "meta", "tags", "steps")


def _redact_json(obj) -> str:
    """遞迴遮罩後序列化。**秘密防線的最後一道**:不管呼叫端有沒有先洗過,
    進 sqlite 的每一段字串都保證過過遮罩。

    為什麼在這裡再做一次(蒸餾器 `_shape` 已經洗過):寫進這張表的東西會被
    模型讀、被晨報顯示、被 API 回給 app —— 一旦漏就是三重外洩。把保證做成
    **結構性的**(進庫必洗),而不是靠每個呼叫端記得洗。
    """
    def walk(o):
        if isinstance(o, str):
            return redact_text(o)
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [walk(v) for v in o]
        return o
    return json.dumps(walk(obj), ensure_ascii=False)


class StateError(ValueError):
    """狀態機違規 —— `.detail` 是要回給人看的 zh-TW 一句話(bridge 轉 409)。"""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def db_path() -> str:
    return os.path.expanduser(os.environ.get("HARNESS_DB", "") or DEFAULT_DB)


class HarnessStore:
    """四庫的 sqlite 門面。執行緒安全(單一 Lock;量小,不值得更細的鎖)。

    照 `AgentRegistry` 的形狀寫:純同步、純資料,不 import bridge、不碰
    provider、不連網。蒸餾器與 HTTP 端點都只透過這個類別碰資料。
    """

    def __init__(self, path: str | None = None):
        self.db_path = os.path.expanduser(path or db_path())
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
            for store, (_pfx, extra) in STORES.items():
                cols = ", ".join(f"{n} {d}" for n, d in _COMMON_COLS + extra)
                con.execute(f"CREATE TABLE IF NOT EXISTS {store}({cols})")
                con.execute(f"CREATE INDEX IF NOT EXISTS idx_{store}_state "
                            f"ON {store}(state)")
                con.execute(f"CREATE INDEX IF NOT EXISTS idx_{store}_key "
                            f"ON {store}(scope, key, version)")
            # 軌跡出處(不是第五庫,是 evidence 的可解析出處)。提案的
            # evidence 存的是 traj id,晨報/稽核要點得開才有意義;同時當
            # 「這條軌跡蒸餾過了嗎」的游標,夜批不會重複啃同一批。
            con.execute("""CREATE TABLE IF NOT EXISTS trajectories(
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL DEFAULT '',
                ts REAL NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                purpose TEXT NOT NULL DEFAULT '',
                ok INTEGER NOT NULL DEFAULT 1,
                duration_s REAL NOT NULL DEFAULT 0,
                payload TEXT NOT NULL,
                ingested_ts REAL NOT NULL,
                distilled_ts REAL)""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_traj_ts ON trajectories(ts)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_traj_sess "
                        "ON trajectories(session_id)")
            # 夜批的跑批帳(晨報要說「昨晚跑了沒、看了幾條、提了幾案」)
            con.execute("""CREATE TABLE IF NOT EXISTS distill_runs(
                id TEXT PRIMARY KEY,
                started_ts REAL NOT NULL,
                finished_ts REAL,
                hours REAL NOT NULL DEFAULT 0,
                trajectories INTEGER NOT NULL DEFAULT 0,
                proposals INTEGER NOT NULL DEFAULT 0,
                model TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '')""")
            con.commit()
        finally:
            con.close()

    # ── 列轉換 ──────────────────────────────────────────────────────────
    @staticmethod
    def _row_dict(row) -> dict:
        d = dict(row)
        for c in _JSON_COLS:
            if c in d and isinstance(d[c], str):
                try:
                    d[c] = json.loads(d[c])
                except (ValueError, TypeError):
                    d[c] = [] if c in ("evidence", "tags", "steps") else {}
        d["applied"] = bool(d.get("applied"))
        return d

    # ── 軌跡出處 ────────────────────────────────────────────────────────
    def put_trajectory(self, traj: dict) -> str:
        """落一條正規化軌跡(冪等:同 id 重放只更新 payload,不重複列)。"""
        tid = str(traj.get("id") or "")
        if not tid:
            raise ValueError("trajectory 缺 id(請用 trajectory.trajectory_id 產生)")
        now = time.time()
        res = traj.get("result") or {}
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    "INSERT INTO trajectories(id, session_id, turn_id, ts, provider,"
                    " purpose, ok, duration_s, payload, ingested_ts)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,"
                    " ok=excluded.ok, duration_s=excluded.duration_s,"
                    " ts=excluded.ts, purpose=excluded.purpose",
                    (tid, str(traj.get("session_id") or ""),
                     str(traj.get("turn_id") or ""), float(traj.get("ts") or now),
                     str(traj.get("provider") or ""),
                     redact_text(traj.get("purpose")),
                     1 if res.get("ok", True) else 0,
                     float(res.get("duration_s") or 0.0),
                     _redact_json(traj), now))     # 縱深防禦:進庫必洗
                con.commit()
            finally:
                con.close()
        return tid

    def trajectories_since(self, since_ts: float, *, limit: int = 500,
                           only_undistilled: bool = False) -> list[dict]:
        sql = ("SELECT payload FROM trajectories WHERE ts >= ?"
               + (" AND distilled_ts IS NULL" if only_undistilled else "")
               + " ORDER BY ts ASC LIMIT ?")
        con = self._connect()
        try:
            rows = con.execute(sql, (float(since_ts), int(limit))).fetchall()
        finally:
            con.close()
        out = []
        for r in rows:
            try:
                out.append(json.loads(r["payload"]))
            except (ValueError, TypeError):
                continue
        return out

    def mark_distilled(self, traj_ids) -> int:
        ids = [str(t) for t in (traj_ids or []) if t]
        if not ids:
            return 0
        now = time.time()
        with self._lock:
            con = self._connect()
            try:
                con.executemany("UPDATE trajectories SET distilled_ts=? WHERE id=?",
                                [(now, t) for t in ids])
                con.commit()
                return con.total_changes
            finally:
                con.close()

    # ── 提案 ────────────────────────────────────────────────────────────
    def propose(self, store: str, *, key: str, payload: dict,
                scope: str = "global", rationale: str = "",
                evidence=None, preview: str = "", meta=None) -> dict:
        """寫一筆新提案(state=proposed)。

        版本自動遞增:同一個 (store, scope, key) 的最大 version + 1。
        「同一件事的第二個版本」與「兩件不同的事」由 key 區分 —— 所以蒸餾器
        給 key 時要穩定(例如 skill 用名字、prompt 用節點 id)。
        """
        if store not in STORES:
            raise ValueError(f"未知的 store:{store}(可用:{'/'.join(STORES)})")
        key = str(key or "").strip()
        if not key:
            raise ValueError("提案必須有 key(同一件事的版本識別)")
        pfx, extra = STORES[store]
        now = time.time()
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute(
                    f"SELECT COALESCE(MAX(version), 0) AS v FROM {store}"
                    " WHERE scope=? AND key=?", (scope, key)).fetchone()
                version = int(cur["v"]) + 1
                pid = pfx + "-" + hashlib.sha1(
                    f"{store}\x00{scope}\x00{key}\x00{version}".encode("utf-8")
                ).hexdigest()[:16]
                fields = {
                    "id": pid, "store": store, "scope": scope, "key": key,
                    "version": version, "state": "proposed",
                    "rationale": redact_text(rationale),
                    "evidence": json.dumps(list(evidence or []), ensure_ascii=False),
                    "preview": redact_text(preview),
                    "created_ts": now, "updated_ts": now,
                    "meta": _redact_json(meta or {}),
                }
                for name, _ddl in extra:
                    v = (payload or {}).get(name)
                    if name in _JSON_COLS:
                        v = _redact_json(v if v is not None else [])
                    elif v is None:
                        v = 0 if "INTEGER" in _ddl else ""
                    elif isinstance(v, str):
                        v = redact_text(v)
                    fields[name] = v
                cols = ", ".join(fields)
                marks = ", ".join("?" for _ in fields)
                con.execute(f"INSERT INTO {store}({cols}) VALUES({marks})",
                            tuple(fields.values()))
                con.commit()
                row = con.execute(f"SELECT * FROM {store} WHERE id=?",
                                  (pid,)).fetchone()
            finally:
                con.close()
        return self._row_dict(row)

    def get(self, pid: str) -> dict | None:
        """跨四庫用 id 找一筆(id 前綴已帶庫別,但還是全掃以免前綴假設外洩)。"""
        con = self._connect()
        try:
            for store in STORES:
                row = con.execute(f"SELECT * FROM {store} WHERE id=?",
                                  (pid,)).fetchone()
                if row is not None:
                    return self._row_dict(row)
        finally:
            con.close()
        return None

    def list(self, *, store: str | None = None, state: str | None = None,
             scope: str | None = None, key: str | None = None,
             limit: int = 200) -> list[dict]:
        stores = [store] if store else list(STORES)
        for s in stores:
            if s not in STORES:
                raise ValueError(f"未知的 store:{s}")
        out: list = []
        con = self._connect()
        try:
            for s in stores:
                sql = f"SELECT * FROM {s} WHERE 1=1"
                args: list = []
                if state:
                    sql += " AND state=?"
                    args.append(state)
                if scope:
                    sql += " AND scope=?"
                    args.append(scope)
                if key:
                    sql += " AND key=?"
                    args.append(key)
                sql += " ORDER BY created_ts DESC LIMIT ?"
                args.append(int(limit))
                out.extend(self._row_dict(r)
                           for r in con.execute(sql, tuple(args)).fetchall())
        finally:
            con.close()
        out.sort(key=lambda r: r.get("created_ts") or 0, reverse=True)
        return out[:limit]

    def active(self, store: str, *, scope: str | None = None) -> list[dict]:
        """某庫現正生效的條目(節點 spawn / 未來路由要讀的就是這個)。"""
        return self.list(store=store, state="active", scope=scope, limit=500)

    # ── 狀態機 ──────────────────────────────────────────────────────────
    def _set_state(self, con, store: str, pid: str, frm: str, to: str,
                   by: str = "", note: str | None = None) -> None:
        if (frm, to) not in _TRANSITIONS:
            raise StateError(f"不允許的狀態轉換:{frm} → {to}")
        now = time.time()
        sql = f"UPDATE {store} SET state=?, updated_ts=?, decided_ts=?, decided_by=?"
        args: list = [to, now, now, by or ""]
        if note is not None:
            sql += ", apply_note=?"
            args.append(note)
        sql += " WHERE id=? AND state=?"
        args += [pid, frm]
        cur = con.execute(sql, tuple(args))
        if cur.rowcount != 1:
            raise StateError(f"狀態已被改動(預期 {frm}),請重讀提案再操作")

    def approve(self, pid: str, *, by: str = "human",
                apply_note: str = "") -> dict:
        """人審通過:proposed → approved → active,並把同 key 的舊 active 退位。

        `by` 是誰批的(稽核用)。這個方法**永遠只在人打 HTTP 時**被呼叫;
        蒸餾器不 import 它、也沒有任何背景任務會走到這裡。
        """
        row = self.get(pid)
        if row is None:
            raise StateError("找不到這筆提案")
        store = row["store"]
        if row["state"] != "proposed":
            raise StateError(
                f"只有 proposed 的提案能核准(這筆現在是 {row['state']})")
        with self._lock:
            con = self._connect()
            try:
                self._set_state(con, store, pid, "proposed", "approved", by)
                # 同 key 的舊 active 退位 —— 一個 key 同時只有一版生效,
                # 不然 prompt 片段會疊加、路由會兩套。
                for old in con.execute(
                        f"SELECT id FROM {store} WHERE scope=? AND key=?"
                        " AND state='active' AND id!=?",
                        (row["scope"], row["key"], pid)).fetchall():
                    self._set_state(con, store, old["id"], "active",
                                    "superseded", by)
                self._set_state(con, store, pid, "approved", "active", by)
                if apply_note:
                    con.execute(f"UPDATE {store} SET apply_note=?, applied=1"
                                " WHERE id=?", (apply_note, pid))
                con.execute(f"UPDATE {store} SET supersedes=("
                            f"SELECT id FROM {store} WHERE scope=? AND key=?"
                            " AND state='superseded' ORDER BY version DESC LIMIT 1)"
                            " WHERE id=?", (row["scope"], row["key"], pid))
                con.commit()
                out = con.execute(f"SELECT * FROM {store} WHERE id=?",
                                  (pid,)).fetchone()
            finally:
                con.close()
        return self._row_dict(out)

    def reject(self, pid: str, *, by: str = "human", reason: str = "") -> dict:
        row = self.get(pid)
        if row is None:
            raise StateError("找不到這筆提案")
        store = row["store"]
        with self._lock:
            con = self._connect()
            try:
                self._set_state(con, store, pid, row["state"], "rejected",
                                by, note=reason or "")
                con.commit()
                out = con.execute(f"SELECT * FROM {store} WHERE id=?",
                                  (pid,)).fetchone()
            finally:
                con.close()
        return self._row_dict(out)

    def mark_applied(self, pid: str, note: str = "") -> None:
        """記下「核准的東西真的落到目的地了」(如 prompt 片段寫進 spawn pin)。"""
        row = self.get(pid)
        if row is None:
            return
        with self._lock:
            con = self._connect()
            try:
                con.execute(f"UPDATE {row['store']} SET applied=1, apply_note=?,"
                            " updated_ts=? WHERE id=?",
                            (note or "", time.time(), pid))
                con.commit()
            finally:
                con.close()

    # ── 跑批帳 ──────────────────────────────────────────────────────────
    def run_start(self, *, hours: float, model: str = "") -> str:
        rid = "run-" + hashlib.sha1(
            f"{time.time()}\x00{hours}".encode("utf-8")).hexdigest()[:12]
        with self._lock:
            con = self._connect()
            try:
                con.execute("INSERT INTO distill_runs(id, started_ts, hours, model)"
                            " VALUES(?,?,?,?)", (rid, time.time(), hours, model))
                con.commit()
            finally:
                con.close()
        return rid

    def run_finish(self, rid: str, *, trajectories: int = 0, proposals: int = 0,
                   error: str = "") -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute("UPDATE distill_runs SET finished_ts=?, trajectories=?,"
                            " proposals=?, error=? WHERE id=?",
                            (time.time(), int(trajectories), int(proposals),
                             str(error or "")[:500], rid))
                con.commit()
            finally:
                con.close()

    def last_run(self) -> dict | None:
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM distill_runs"
                              " ORDER BY started_ts DESC LIMIT 1").fetchone()
        finally:
            con.close()
        return dict(row) if row is not None else None
