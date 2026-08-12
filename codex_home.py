#!/usr/bin/env python3
"""Codex 家目錄隔離（`CODEX_HOME`）：讓 bridge 擁有自己的 thread-store。

# 為什麼要這個模組

codex 的 thread-store **每條 thread 只允許一個 writer**。想寫的有三方：

  1. ChatGPT 桌面 app（自己會起一顆 app-server）
  2. 這支 bridge（Pocket 的 CX）
  3. 任何一個 `codex` CLI

2026-08-12 的事故鏈：桌面 app 05:32 自動更新 → 殘留殭屍 app-server →
`thread-store conflict: already has an active writer` → Pocket 的 CX 全滅。
當天的止血（`codex/fix-managed-codex-transport`）是叫 bridge 去接桌面 app 的
**managed daemon**，共用同一顆 writer。那修好了衝突，卻把依賴方向倒過來：

  > **Pocket 能不能用，變成取決於一個第三方 GUI app 有沒有活著。**

這對產品不可接受，而且龍蝦那台無頭 Ubuntu（`feat/lobster-ubuntu`）根本沒有
桌面 app，那條路直接不成立。

# 這個模組做什麼

給 bridge 一個**自己的** `CODEX_HOME`（預設 `~/.pocket/codex-home`），
thread-store 從此完全歸自己所有，隨時都能起自己的 app-server，不必跟誰搶鎖。

代價（善彰 2026-08-12 明確接受）：語意變得跟 CC 一樣 ——
**只有「透過 bridge / Pocket 開的 session」才會出現在 Pocket 裡**，
在 VS Code / 桌面 app / 終端機 `codex` 開的 thread 不會再出現。
舊 thread 可以用 `scripts/migrate-codex-threads.py` 一次性搬過來（見下）。

# 三個設計決定（都經過實機驗證，codex-cli 0.147）

## 1. 憑證：symlink `auth.json`

`auth.json` 放在 `CODEX_HOME` 底下，隔離家目錄一開始是「未登入」狀態
（實測 `CODEX_HOME=<空目錄> codex login status` → `Not logged in`）。

實測 codex **寫 auth.json 是就地 truncate+write，不是 tmp+rename**
（拿 symlink 當 `auth.json`，跑 `codex login --with-api-key` 之後 symlink
仍在、內容寫進了 symlink 指到的那個檔）。所以 symlink 是安全且正確的選擇：

  * 隔離家目錄與 `~/.codex` **共用同一個實體檔**，只有一份 refresh token
    世系。token 轉動（rotation）時兩邊同時更新，不會有「複製一份 → 兩邊各自
    refresh → 其中一份被作廢」的登出事故。
  * 完全不必把 token 內容複製到第二個地方。

因此預設 **不複製憑證，只做 symlink**。若 symlink 已被換成實體檔（有人手動
複製過），我們**不動它**，只記一則 log。

## 2. 設定：複製 `config.toml` + 可自動刷新

codex 沒有 `include` 語法，隔離家目錄讀不到 `~/.codex/config.toml`
（`model_reasoning_effort = "xhigh"` 那些會整組消失）。所以 bootstrap 時
**複製**一份，並記下「我複製當下的內容雜湊」：

  * 下次啟動時若隔離副本沒被手改（雜湊 == 記錄值）而來源變了 → **自動重新複製**
    （善彰改 `~/.codex/config.toml` → 重啟 bridge 就生效）。
  * 若隔離副本被手改過 → **保留手改**，不覆蓋，只記 log。

`POCKET_CODEX_CONFIG_MODE` 可選 `copy`（預設）/ `symlink`（想讓兩邊永遠一致，
但代價是 codex 若寫 config 會寫回 `~/.codex`）/ `none`（完全自己管）。

## 3. 舊 thread：可以搬，而且是唯讀來源的一次性複製

thread-store = `<home>/state_5.sqlite` 的 `threads` 資料列 +
`rollout_path` 指到的 `<home>/sessions/**/rollout-*.jsonl` 逐字稿。
兩邊都搬過去、把 `rollout_path` 改寫成新家的路徑，thread 就能在隔離家目錄
被列出/resume。見 `migrate_threads()`：來源全程唯讀（先快照再讀快照），
可重複執行（已存在就跳過）。

# 環境變數

  POCKET_CODEX_ISOLATED   1/0/true/false/on/off/auto（預設 auto）
                          auto = macOS 關、其他平台（龍蝦無頭 Ubuntu）開
  POCKET_CODEX_HOME       隔離家目錄路徑（預設 ~/.pocket/codex-home）
  POCKET_CODEX_CONFIG_MODE  copy（預設）/ symlink / none
  CODEX_HOME              「共用家目錄」在哪（預設 ~/.codex）；隔離關閉時
                          bridge 就是用這個，行為與今天一致
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

DEFAULT_SHARED_HOME = "~/.codex"
DEFAULT_ISOLATED_HOME = "~/.pocket/codex-home"
CONFIG_ORIGIN_MARKER = ".pocket-config-origin.json"

_TRUE = ("1", "true", "yes", "on", "enabled")
_FALSE = ("0", "false", "no", "off", "disabled")


class CodexHomeError(RuntimeError):
    """隔離家目錄 bootstrap / 搬遷失敗。"""


# ───────────────────────── 路徑與旗標 ─────────────────────────

def shared_home() -> str:
    """共用家目錄（桌面 app / CLI 用的那個）。`CODEX_HOME` 有設就聽它的。"""
    return os.path.expanduser(os.environ.get("CODEX_HOME") or DEFAULT_SHARED_HOME)


def isolated_home() -> str:
    """bridge 專屬家目錄。"""
    return os.path.expanduser(
        os.environ.get("POCKET_CODEX_HOME") or DEFAULT_ISOLATED_HOME)


def isolation_enabled() -> bool:
    """要不要用隔離家目錄。

    預設 `auto`：**macOS 關**（善彰的機器上這次合併要零風險，開關由 plist 掌握）、
    **非 macOS 開**（龍蝦那台無頭 Ubuntu 根本沒有桌面 app 的 managed daemon，
    隔離是嚴格更好且沒有代價的預設）。
    """
    raw = (os.environ.get("POCKET_CODEX_ISOLATED") or "auto").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return sys.platform != "darwin"


def effective_home() -> str:
    """這一刻 bridge 實際會用的 codex 家目錄。"""
    return isolated_home() if isolation_enabled() else shared_home()


def config_mode() -> str:
    mode = (os.environ.get("POCKET_CODEX_CONFIG_MODE") or "copy").strip().lower()
    return mode if mode in ("copy", "symlink", "none") else "copy"


def child_env(base: dict | None = None) -> dict | None:
    """給 `codex` 子行程的環境變數。

    隔離關閉時回 `None`（= 原封不動繼承 bridge 的環境，行為與今天逐位元組
    相同，連 `CODEX_HOME` 有沒有出現在環境裡都一樣）。
    """
    if not isolation_enabled():
        return None
    env = dict(os.environ if base is None else base)
    env["CODEX_HOME"] = isolated_home()
    return env


def sessions_dirs() -> list[str]:
    """要掃 rollout jsonl（用量/rate-limit）的目錄，新的排前面。

    隔離開啟時共用家目錄仍然要掃：rate limit 是**帳號層級**的事實，桌面 app
    寫下的那筆一樣算數，少掃只會讓用量顯示變舊。
    """
    dirs = [os.path.join(effective_home(), "sessions")]
    shared = os.path.join(shared_home(), "sessions")
    if isolation_enabled() and os.path.realpath(shared) not in (
            os.path.realpath(d) for d in dirs):
        dirs.append(shared)
    return dirs


# ───────────────────────── bootstrap ─────────────────────────

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_marker(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — marker 壞掉不該擋開機
        return {}


def _bootstrap_auth(home: str, source: str, report: dict) -> None:
    src = os.path.join(source, "auth.json")
    dst = os.path.join(home, "auth.json")
    if os.path.islink(dst):
        if os.path.exists(dst):
            report["auth"] = "symlink-ok"
            return
        # 斷掉的 symlink（來源被搬走過）：來源回來了就重新接上
        if os.path.exists(src):
            os.unlink(dst)
        else:
            report["auth"] = "symlink-broken"
            return
    elif os.path.exists(dst):
        # 有人刻意放了實體檔（例如專用帳號的憑證）——尊重它，不要偷偷換掉
        report["auth"] = "regular-file-kept"
        return
    if not os.path.exists(src):
        report["auth"] = "source-missing"
        return
    os.symlink(src, dst)
    report["auth"] = "symlinked"


def _bootstrap_config(home: str, source: str, mode: str, report: dict) -> None:
    if mode == "none":
        report["config"] = "skipped"
        return
    src = os.path.join(source, "config.toml")
    dst = os.path.join(home, "config.toml")
    marker_path = os.path.join(home, CONFIG_ORIGIN_MARKER)
    if not os.path.exists(src):
        report["config"] = "source-missing"
        return
    if mode == "symlink":
        if os.path.islink(dst) and os.path.exists(dst):
            report["config"] = "symlink-ok"
            return
        if os.path.exists(dst) and not os.path.islink(dst):
            report["config"] = "regular-file-kept"
            return
        if os.path.islink(dst):
            os.unlink(dst)
        os.symlink(src, dst)
        report["config"] = "symlinked"
        return

    src_sha = _sha256_file(src)
    if os.path.islink(dst):
        # 從 symlink 模式切回 copy 模式：拆掉連結，改成真的複製一份
        os.unlink(dst)
    if os.path.exists(dst):
        cur_sha = _sha256_file(dst)
        marker = _read_marker(marker_path)
        if cur_sha == src_sha:
            report["config"] = "up-to-date"
            # 已經一致就不要每 60 秒重寫一次 marker(那只是無謂的磁碟寫入)
            if marker.get("copied_sha256") != src_sha:
                _write_config_marker(marker_path, src, src_sha)
            return
        if marker.get("copied_sha256") != cur_sha:
            # 隔離副本被手改過 → 那是有意的，不要覆蓋掉
            report["config"] = "local-edit-kept"
            return
        report["config"] = "refreshed"
    else:
        report["config"] = "copied"
    tmp = dst + ".tmp-pocket"
    shutil.copyfile(src, tmp)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dst)
    _write_config_marker(marker_path, src, src_sha)


def _write_config_marker(marker_path: str, src: str, sha: str) -> None:
    payload = {"source": src, "copied_sha256": sha, "copied_at": time.time()}
    tmp = marker_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, marker_path)


def bootstrap(home: str | None = None, source: str | None = None,
              mode: str | None = None) -> dict:
    """把隔離家目錄準備好（可重複執行；**完全不寫入來源家目錄**）。

    回報 dict：`{"home","created","auth","config","source"}`，給呼叫端記 log。
    """
    home = os.path.expanduser(home or isolated_home())
    source = os.path.expanduser(source or shared_home())
    if os.path.realpath(home) == os.path.realpath(source):
        raise CodexHomeError(
            f"隔離家目錄不能等於共用家目錄({home})——那就沒有隔離了")
    report = {"home": home, "source": source, "created": False,
              "auth": "", "config": ""}
    parent = os.path.dirname(home.rstrip("/"))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
        os.chmod(parent, 0o700)
    if not os.path.isdir(home):
        os.makedirs(home, exist_ok=True)
        report["created"] = True
    os.chmod(home, 0o700)          # 憑證住在這裡，一律 0700
    os.makedirs(os.path.join(home, "sessions"), exist_ok=True)
    _bootstrap_auth(home, source, report)
    _bootstrap_config(home, source, mode or config_mode(), report)
    return report


# ───────────────────── thread 搬遷（一次性、選擇性）─────────────────────

_THREAD_TABLES = ("thread_dynamic_tools", "thread_spawn_edges")


def state_db_path(home: str) -> str:
    """`<home>/state_<n>.sqlite` 裡版號最大的那顆；沒有就回 ""。"""
    best, best_n = "", -1
    for path in glob.glob(os.path.join(os.path.expanduser(home), "state_*.sqlite")):
        stem = os.path.basename(path)[len("state_"):-len(".sqlite")]
        try:
            n = int(stem)
        except ValueError:
            continue
        if n > best_n:
            best, best_n = path, n
    return best


def resolve_codex_bin() -> str:
    """跟 bridge 同一份候選序（重複一次，讓這支能獨立跑，不必 import bridge）。"""
    for c in (os.environ.get("CODEX_BIN"),
              "/Applications/Codex.app/Contents/Resources/codex",
              "/Applications/ChatGPT.app/Contents/Resources/codex",
              os.path.expanduser("~/.local/bin/codex"),
              shutil.which("codex")):
        if c and os.path.exists(c):
            return c
    return os.path.expanduser("~/.local/bin/codex")


def ensure_state_db(home: str, timeout: float = 60.0) -> str:
    """隔離家目錄還沒有 thread-store 的話，起一次 app-server 讓 codex 自己建。

    只做 `initialize`，不開任何 thread，建完就收工。
    """
    path = state_db_path(home)
    if path:
        return path
    env = dict(os.environ)
    env["CODEX_HOME"] = os.path.expanduser(home)
    proc = subprocess.Popen(
        [resolve_codex_bin(), "app-server", "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, cwd=os.path.expanduser("~"))
    try:
        proc.stdin.write((json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": "pocketagent-bridge-migrate",
                                      "title": "PocketAgent Bridge",
                                      "version": "0.1"},
                       "capabilities": {"experimentalApi": True}}}) + "\n").encode())
        proc.stdin.flush()
        proc.stdout.readline()
        deadline = time.time() + timeout
        while time.time() < deadline:
            path = state_db_path(home)
            if path:
                break
            time.sleep(0.2)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
    if not path:
        raise CodexHomeError(
            f"起不了 app-server 或它沒有建出 state_*.sqlite：{home}")
    return path


def _snapshot_state_db(src_db: str, tmpdir: str) -> str:
    """把來源 thread-store **複製**出來再讀。

    為什麼不直接開來源：來源是 WAL 模式，sqlite 就算 `mode=ro` 也可能要動
    `-shm`/做 checkpoint，那就是寫入。任務紅線是「絕不變更 `~/.codex` 底下
    任何東西」，所以一律先複製快照，之後只碰快照。
    """
    dst = os.path.join(tmpdir, "source-state.sqlite")
    shutil.copyfile(src_db, dst)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(src_db + suffix):
            shutil.copyfile(src_db + suffix, dst + suffix)
    return dst


def _copy_rollout(src: str, dst: str) -> str:
    """複製逐字稿。APFS 上優先用 clone（`cp -c`）：秒殺、不佔額外空間，
    而且是 copy-on-write —— 之後在隔離家目錄續寫**不會**動到來源那份。"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if sys.platform == "darwin":
        try:
            subprocess.run(["cp", "-c", src, dst], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "cloned"
        except Exception:  # noqa: BLE001 — 非 APFS / cp 沒有 -c
            pass
    shutil.copyfile(src, dst)
    return "copied"


def _target_rollout_path(src_path: str, source_home: str, target_home: str) -> str:
    src_path = os.path.abspath(src_path)
    root = os.path.abspath(source_home)
    if src_path.startswith(root + os.sep):
        return os.path.join(target_home, os.path.relpath(src_path, root))
    return os.path.join(target_home, "sessions", "imported",
                        os.path.basename(src_path))


def select_threads(conn: sqlite3.Connection, thread_ids=None, recent: int = 0,
                   include_archived: bool = False) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows: list[sqlite3.Row] = []
    seen = set()
    for tid in (thread_ids or []):
        row = conn.execute("SELECT * FROM threads WHERE id=?", (tid,)).fetchone()
        if row is not None and row["id"] not in seen:
            seen.add(row["id"])
            rows.append(row)
    if recent:
        where = "" if include_archived else "WHERE archived=0 AND preview<>''"
        for row in conn.execute(
                f"SELECT * FROM threads {where} "
                "ORDER BY recency_at_ms DESC, id DESC LIMIT ?", (recent,)):
            if row["id"] not in seen:
                seen.add(row["id"])
                rows.append(row)
    return rows


def list_threads(source: str | None = None, limit: int = 30,
                 include_archived: bool = False) -> list[dict]:
    """列出來源家目錄裡可搬的 thread（唯讀，走快照）。"""
    source = os.path.expanduser(source or shared_home())
    src_db = state_db_path(source)
    if not src_db:
        raise CodexHomeError(f"來源家目錄沒有 thread-store：{source}")
    with tempfile.TemporaryDirectory(prefix="cxmig-") as tmpdir:
        snap = _snapshot_state_db(src_db, tmpdir)
        conn = sqlite3.connect(snap)
        try:
            rows = select_threads(conn, recent=limit,
                                  include_archived=include_archived)
            return [{"id": r["id"], "name": r["name"] or "",
                     "title": (r["title"] or "").replace("\n", " ")[:60],
                     "cwd": r["cwd"], "archived": r["archived"],
                     "updated_at_ms": r["updated_at_ms"],
                     "rollout_path": r["rollout_path"],
                     "rollout_exists": os.path.exists(r["rollout_path"] or "")}
                    for r in rows]
        finally:
            conn.close()


def migrate_threads(thread_ids=None, recent: int = 0, source: str | None = None,
                    target: str | None = None, apply: bool = False,
                    include_archived: bool = False) -> dict:
    """把指定 thread 從共用家目錄搬進隔離家目錄。

    * **來源全程唯讀**：state db 先快照再讀，逐字稿只讀不寫。
    * **可重複執行**：目標已經有同一個 thread id 就跳過（`already-present`）。
    * `apply=False`（預設）只做預演，不寫任何東西到目標。
    """
    source = os.path.expanduser(source or shared_home())
    target = os.path.expanduser(target or isolated_home())
    if os.path.realpath(source) == os.path.realpath(target):
        raise CodexHomeError("來源與目標是同一個家目錄，沒有東西要搬")
    src_db = state_db_path(source)
    if not src_db:
        raise CodexHomeError(f"來源家目錄沒有 thread-store：{source}")

    result = {"source": source, "target": target, "apply": apply,
              "migrated": [], "skipped": [], "bytes": 0}
    with tempfile.TemporaryDirectory(prefix="cxmig-") as tmpdir:
        snap = _snapshot_state_db(src_db, tmpdir)
        src_conn = sqlite3.connect(snap)
        src_conn.row_factory = sqlite3.Row
        try:
            rows = select_threads(src_conn, thread_ids, recent, include_archived)
            missing = set(thread_ids or []) - {r["id"] for r in rows}
            for tid in sorted(missing):
                result["skipped"].append({"id": tid, "reason": "not-found"})
            if not apply:
                for row in rows:
                    rollout = row["rollout_path"] or ""
                    if not rollout or not os.path.exists(rollout):
                        result["skipped"].append(
                            {"id": row["id"], "reason": "rollout-missing"})
                        continue
                    result["migrated"].append(
                        {"id": row["id"], "name": row["name"] or "",
                         "rollout": _target_rollout_path(rollout, source, target),
                         "mode": "dry-run"})
                    result["bytes"] += os.path.getsize(rollout)
                return result

            bootstrap(home=target, source=source)
            dst_db = ensure_state_db(target)
            dst_conn = sqlite3.connect(dst_db, timeout=30)
            dst_conn.row_factory = sqlite3.Row
            try:
                _assert_same_schema(src_conn, dst_conn)
                for row in rows:
                    outcome = _migrate_one(src_conn, dst_conn, row,
                                           source, target)
                    if outcome.get("reason"):
                        result["skipped"].append(outcome)
                    else:
                        result["migrated"].append(outcome)
                        result["bytes"] += outcome.get("size", 0)
                dst_conn.commit()
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
    return result


def _assert_same_schema(src_conn: sqlite3.Connection,
                        dst_conn: sqlite3.Connection) -> None:
    def version(conn):
        try:
            return conn.execute("SELECT MAX(version) FROM _sqlx_migrations").fetchone()[0]
        except sqlite3.Error:
            return None
    a, b = version(src_conn), version(dst_conn)
    if a is None or b is None or a != b:
        raise CodexHomeError(
            f"thread-store schema 版本對不上(來源 {a} / 目標 {b})；"
            "先用同一版 codex 開一次隔離家目錄再搬")


def _migrate_one(src_conn, dst_conn, row, source: str, target: str) -> dict:
    tid = row["id"]
    exists = dst_conn.execute("SELECT 1 FROM threads WHERE id=?", (tid,)).fetchone()
    if exists:
        return {"id": tid, "reason": "already-present"}
    rollout = row["rollout_path"] or ""
    if not rollout or not os.path.exists(rollout):
        return {"id": tid, "reason": "rollout-missing"}
    dst_rollout = _target_rollout_path(rollout, source, target)
    size = os.path.getsize(rollout)
    how = "reused"
    if not (os.path.exists(dst_rollout)
            and os.path.getsize(dst_rollout) == size):
        how = _copy_rollout(rollout, dst_rollout)

    cols = list(row.keys())
    values = [dst_rollout if c == "rollout_path" else row[c] for c in cols]
    # thread_sections 有外鍵，先補上被指到的那一列
    section_id = row["thread_section_id"] if "thread_section_id" in cols else None
    if section_id:
        sec = src_conn.execute("SELECT * FROM thread_sections WHERE id=?",
                               (section_id,)).fetchone()
        if sec is not None:
            dst_conn.execute(
                "INSERT OR IGNORE INTO thread_sections(id,name) VALUES(?,?)",
                (sec["id"], sec["name"]))
    dst_conn.execute(
        f"INSERT INTO threads({','.join(cols)}) "
        f"VALUES({','.join('?' for _ in cols)})", values)
    for table in _THREAD_TABLES:
        key = "thread_id" if table == "thread_dynamic_tools" else "child_thread_id"
        try:
            extra = src_conn.execute(
                f"SELECT * FROM {table} WHERE {key}=?", (tid,)).fetchall()
        except sqlite3.Error:
            continue
        for erow in extra:
            ecols = list(erow.keys())
            dst_conn.execute(
                f"INSERT OR IGNORE INTO {table}({','.join(ecols)}) "
                f"VALUES({','.join('?' for _ in ecols)})",
                [erow[c] for c in ecols])
    return {"id": tid, "name": row["name"] or "", "rollout": dst_rollout,
            "mode": how, "size": size}
