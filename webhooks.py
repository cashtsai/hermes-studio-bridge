"""Webhook automations —— 讓外部事件(GitHub PR/CI、告警…)自動開一條 agent。

設計照 t3code 的 webhook automations 搬:每條 hook 一組獨立 secret,入站用
HMAC-SHA256 簽章驗證(**不走 bearer**,因為送方是 GitHub/CI,不可能帶我們的
device token);通過後依序走 去重 → 速率 → filters → coalesce → 觸發,每一步
都落庫並記在 deliveries 供事後排查。

這個模組**不 import bridge**(避免循環;同 capacity_lease/orchestrator 的分工)。
它只負責:資料庫、純邏輯(簽章/filters/去重/coalesce/速率/硬化/模板),以及把
「要不要觸發、觸發要送什麼 prompt」算清楚回給 bridge。真正「開 session 送
prompt」的動作由 bridge 注入一個 async callback 執行 —— provider 怎麼起、cwd
怎麼接,是 bridge 的事,這裡只把算好的 prompt 交出去。

威脅模型(第 8 點,最重要):入站 payload 的每個欄位都是**不可信輸入**。進
prompt 前一律包 <webhook-data> 標記,並在 `$`/`@`/行首 `/` 後插零寬字元,使
`$skill`、`@file`、`//command`、`/slash` 這類會被 agent runtime 特別解讀的
前綴**當場失效** —— 攻擊者就算能發出通過簽章的 payload(例如拿到 secret),
也沒辦法用欄位內容把 agent 導去跑別的技能/讀別的檔/下控制指令。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


# ── 常數 ────────────────────────────────────────────────────────────────────
WEBHOOKS_DB = os.environ.get("POCKET_WEBHOOKS_DB") \
    or os.path.expanduser("~/.local/share/pocket-agent/webhooks.db")

SIGNATURE_HEADER = "x-hub-signature-256"   # GitHub 慣例;sha256=<hex>
SIGNATURE_PREFIX = "sha256="

FILTER_OPS = ("equals", "is-one-of", "exists")
PROVIDERS = ("codex", "cc2")

DEDUP_RETENTION_SECS = 7 * 24 * 3600       # 去重紀錄保留 7 天(規格第 4 點)
DELIVERY_RETENTION_SECS = 7 * 24 * 3600    # 投遞日誌同樣 7 天
COALESCE_MIN_WINDOW = 10                   # 規格第 5 點:10~3600 秒
COALESCE_MAX_WINDOW = 3600
RATE_DEFAULT = 60                          # 每 hook 每分鐘預設上限
RATE_WINDOW_SECS = 60

# 第 8 點硬化用的零寬字元(ZERO WIDTH SPACE)。插在危險前綴**之後**,
# 讓 `$skill` 變成 `$<zwsp>skill`:顯示上幾乎一樣,但 tokenizer/指令解析器
# 看到的是「`$` 後面不是字母」→ 當成普通字元,技能/檔案/指令前綴失效。
ZWSP = "​"
WEBHOOK_DATA_OPEN = "<webhook-data>"
WEBHOOK_DATA_CLOSE = "</webhook-data>"

# {{ json.path }} 佔位符;path 走 dot + [] 通配,同 filters。
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


class WebhookError(ValueError):
    """設定/輸入不合法 —— 由管理 API 轉成 400。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ── 純邏輯:JSON path ────────────────────────────────────────────────────────
def resolve_path(payload: Any, path: str) -> list:
    """把 dot-path 解析成**值的列表**(`[]` 通配會展開陣列,故可能多值)。

    `commits[].author.name` → 每個 commit 的 author.name。任何一段對不上(鍵
    缺、型別不符)就在那條分支止步,不會丟例外 —— webhook payload 形狀不受我們
    控制,解不出來等於「這個欄位不存在」,交給上層的 exists/equals 判定。
    """
    tokens = [t for t in str(path or "").split(".") if t != ""]
    current: list = [payload]
    for tok in tokens:
        wild = tok.endswith("[]")
        key = tok[:-2] if wild else tok
        nxt: list = []
        for node in current:
            if key:
                if isinstance(node, dict) and key in node:
                    val = node[key]
                else:
                    continue
            else:
                val = node               # 純 `[]`:對當前節點本身展開
            if wild:
                if isinstance(val, list):
                    nxt.extend(val)
                # 非陣列卻用 [] → 不匹配(止步)
            else:
                nxt.append(val)
        current = nxt
    return current


def match_filter(payload: Any, flt: dict) -> bool:
    """單條 filter。op ∈ {equals, is-one-of, exists}。多值(通配)時**任一**命中即可。"""
    op = flt.get("op")
    resolved = resolve_path(payload, flt.get("path", ""))
    if op == "exists":
        want = flt.get("value")
        want = want if isinstance(want, bool) else True
        return (len(resolved) > 0) == want
    if op == "equals":
        target = flt.get("value")
        return any(r == target for r in resolved)
    if op == "is-one-of":
        opts = flt.get("value")
        opts = opts if isinstance(opts, list) else [opts]
        return any(r in opts for r in resolved)
    return False


def evaluate_filters(payload: Any, filters: Iterable[dict]) -> tuple[bool, str]:
    """全部 filter 皆過(AND)才觸發。回 (passed, reason)。空清單 → 無條件過。"""
    for flt in filters or ():
        if not match_filter(payload, flt):
            return False, f"filter_unmatched:{flt.get('path')} {flt.get('op')}"
    return True, ""


# ── 純邏輯:簽章 ─────────────────────────────────────────────────────────────
def expected_signature(secret: str, raw_body: bytes) -> str:
    mac = hmac.new((secret or "").encode("utf-8"), raw_body or b"", hashlib.sha256)
    return SIGNATURE_PREFIX + mac.hexdigest()


def verify_signature(secret: str, raw_body: bytes, header_value: str | None) -> str:
    """三態:'valid' / 'invalid' / 'missing'。缺 header 與簽錯分開 —— 兩種都
    401,但 deliveries 要能分辨(排查「對方根本沒簽」vs「secret 不對」)。"""
    if not header_value:
        return "missing"
    expected = expected_signature(secret, raw_body)
    return "valid" if hmac.compare_digest(expected, str(header_value)) else "invalid"


# ── 純邏輯:外部資料硬化(第 8 點)─────────────────────────────────────────────
def harden_value(value: str) -> str:
    """把 payload 欄位值裡會被 agent runtime 特別解讀的前綴中性化。

    - `$` 後插 ZWSP → `$skill` 失效
    - `@` 後插 ZWSP → `@file` 失效
    - **行首** `/` 後插 ZWSP → `/command`、`//command` 失效(`//` 會被切成
      `/<zwsp>/`,兩個斜線都中性化)
    - 順手把 `<webhook-data>`/`</webhook-data>` 標記本身打斷,避免欄位值偽造
      收尾標籤「跳出」不可信區塊(tag breakout)。

    刻意只插零寬字元、不刪字:值仍可讀、長度幾乎不變,但危險語意沒了。
    """
    s = str(value)
    s = s.replace("$", "$" + ZWSP).replace("@", "@" + ZWSP)
    # 行首斜線:^ 或換行後緊接的 `/`。用 re 處理每一行的第一個 `/`。
    s = re.sub(r"(?m)^(/)", r"\1" + ZWSP, s)
    # tag breakout 防線:把標記關鍵字裡插 ZWSP,文字照讀、不再是有效標籤。
    s = re.sub(r"<(/?)(webhook-data)", lambda m: "<" + m.group(1) + ZWSP + m.group(2),
               s, flags=re.IGNORECASE)
    return s


def wrap_untrusted(value: Any) -> str:
    """硬化 + 包不可信標記。所有進 prompt 的 payload 值都要走這支。"""
    return WEBHOOK_DATA_OPEN + harden_value(_stringify(value)) + WEBHOOK_DATA_CLOSE


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def render_prompt(template: str, payload: Any) -> str:
    """把 `{{ json.path }}` 代換成 payload 值 —— **每個值都包 <webhook-data> +
    硬化**。多值(通配)用換行串接;路徑不存在 → 空的不可信區塊(不漏成字面
    `{{...}}`,那會讓 agent 看到原始佔位符而困惑)。模板本身(hook 作者寫的)
    是可信的,不硬化。"""
    def _sub(m: re.Match) -> str:
        values = resolve_path(payload, m.group(1))
        if not values:
            return wrap_untrusted("")
        return "\n".join(wrap_untrusted(v) for v in values)
    return _PLACEHOLDER_RE.sub(_sub, str(template or ""))


def coalesce_key(template: str, payload: Any) -> str:
    """coalesce key 也可用 `{{path}}`;這裡**不硬化**(key 不進 prompt,只做歸併
    分組),但缺值要能得出穩定字串,否則整個歸併失效。"""
    if not template:
        return ""
    def _sub(m: re.Match) -> str:
        vals = resolve_path(payload, m.group(1))
        return _stringify(vals[0]) if vals else ""
    return _PLACEHOLDER_RE.sub(_sub, str(template))


def delivery_key(dedup_source: str, headers: dict, raw_body: bytes) -> str:
    """去重鍵:優先用設定的 header(如 GitHub 的 `x-github-delivery`),沒有就
    退回 payload 雜湊。headers 以小寫鍵傳入。"""
    src = (dedup_source or "").strip().lower()
    if src:
        val = (headers or {}).get(src)
        if val:
            return "id:" + str(val)
    return "hash:" + hashlib.sha256(raw_body or b"").hexdigest()


# ── 設定驗證 ─────────────────────────────────────────────────────────────────
def _validate_filters(raw: Any) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise WebhookError("BAD_FILTERS", "filters 必須是陣列")
    out = []
    for i, flt in enumerate(raw):
        if not isinstance(flt, dict):
            raise WebhookError("BAD_FILTERS", f"filters[{i}] 必須是物件")
        op = flt.get("op")
        path = str(flt.get("path") or "").strip()
        if op not in FILTER_OPS:
            raise WebhookError("BAD_FILTERS",
                               f"filters[{i}].op 必須是 {list(FILTER_OPS)} 之一")
        if not path:
            raise WebhookError("BAD_FILTERS", f"filters[{i}].path 不可空")
        clean = {"op": op, "path": path}
        if op != "exists":
            if "value" not in flt:
                raise WebhookError("BAD_FILTERS",
                                   f"filters[{i}] 的 {op} 需要 value")
            clean["value"] = flt["value"]
        elif "value" in flt:
            clean["value"] = bool(flt["value"])
        out.append(clean)
    return out


def _validate_coalesce(raw: Any) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise WebhookError("BAD_COALESCE", "coalesce 必須是物件或 null")
    try:
        window = int(raw.get("window_seconds"))
    except (TypeError, ValueError):
        raise WebhookError("BAD_COALESCE", "coalesce.window_seconds 必須是整數")
    if not COALESCE_MIN_WINDOW <= window <= COALESCE_MAX_WINDOW:
        raise WebhookError(
            "BAD_COALESCE",
            f"coalesce.window_seconds 必須在 {COALESCE_MIN_WINDOW}~{COALESCE_MAX_WINDOW} 秒之間")
    return {"key": str(raw.get("key") or "").strip(), "window_seconds": window}


def validate_hook_config(body: dict, *, partial: bool = False) -> dict:
    """管理 API 建立/更新用的欄位驗證。partial=True 時只驗出現的欄位(PATCH)。"""
    out: dict = {}
    b = body or {}

    def _present(k):
        return k in b

    if not partial or _present("name"):
        name = str(b.get("name") or "").strip()
        if not name:
            raise WebhookError("BAD_NAME", "name 不可空")
        out["name"] = name[:120]
    if not partial or _present("provider"):
        provider = str(b.get("provider") or "").strip()
        if provider not in PROVIDERS:
            raise WebhookError("BAD_PROVIDER",
                               f"provider 必須是 {list(PROVIDERS)} 之一")
        out["provider"] = provider
    if not partial or _present("cwd"):
        cwd = str(b.get("cwd") or "").strip()
        if not cwd:
            raise WebhookError("BAD_CWD", "cwd 不可空")
        out["cwd"] = cwd
    if not partial or _present("prompt_template"):
        tmpl = str(b.get("prompt_template") or "")
        if not tmpl.strip():
            raise WebhookError("BAD_PROMPT", "prompt_template 不可空")
        out["prompt_template"] = tmpl
    if _present("filters") or not partial:
        out["filters"] = _validate_filters(b.get("filters"))
    if _present("coalesce") or not partial:
        out["coalesce"] = _validate_coalesce(b.get("coalesce"))
    if _present("dedup_source") or not partial:
        out["dedup_source"] = str(b.get("dedup_source") or "").strip().lower()
    if _present("rate_per_minute") or not partial:
        try:
            rate = int(b.get("rate_per_minute") if b.get("rate_per_minute") is not None
                       else RATE_DEFAULT)
        except (TypeError, ValueError):
            raise WebhookError("BAD_RATE", "rate_per_minute 必須是整數")
        if rate < 1:
            raise WebhookError("BAD_RATE", "rate_per_minute 至少 1")
        out["rate_per_minute"] = rate
    if _present("enabled") or not partial:
        out["enabled"] = bool(b.get("enabled", True))
    return out


# ── 評估結果 ─────────────────────────────────────────────────────────────────
@dataclass
class Decision:
    action: str                 # trigger | coalesced | duplicate | filtered |
                                # bad_signature | missing_signature | rate_limited |
                                # disabled | not_found
    status: int                 # 建議的 HTTP 狀態碼
    reason: str = ""
    delivery_id: str = ""
    prompt: str = ""
    provider: str = ""
    cwd: str = ""
    extra: dict = field(default_factory=dict)

    def response(self) -> dict:
        out = {"ok": self.action in ("trigger", "coalesced", "duplicate", "filtered"),
               "action": self.action}
        if self.reason:
            out["reason"] = self.reason
        if self.delivery_id:
            out["delivery_id"] = self.delivery_id
        out.update(self.extra)
        return out


# ── Store ────────────────────────────────────────────────────────────────────
class WebhookStore:
    """全部落庫,去重與 coalesce 重啟後仍有效(規格第 4、5 點)。

    速率限制**刻意留在記憶體**:它的語意是「此刻這一分鐘」,重啟歸零是可接受
    的(重啟後本來就沒有進行中的突發),落庫反而要處理時鐘漂移與清理,不划算。
    """

    def __init__(self, db_path: str | None = None, *,
                 id_factory: Callable[[], str] | None = None,
                 secret_factory: Callable[[], str] | None = None):
        self.db_path = db_path or WEBHOOKS_DB
        self._id_factory = id_factory or (lambda: "wh_" + secrets.token_hex(8))
        self._secret_factory = secret_factory or (lambda: secrets.token_urlsafe(32))
        self._rate: dict[str, list[float]] = {}
        self._init_db()

    # —— DB plumbing ——
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        con = self._connect()
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("""CREATE TABLE IF NOT EXISTS webhooks (
                id TEXT PRIMARY KEY, name TEXT, provider TEXT, cwd TEXT,
                prompt_template TEXT, filters_json TEXT, coalesce_json TEXT,
                dedup_source TEXT, rate_per_minute INTEGER, enabled INTEGER,
                secret TEXT, created_at REAL, updated_at REAL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                id TEXT PRIMARY KEY, hook_id TEXT, received_at REAL, source TEXT,
                delivery_key TEXT, triggered INTEGER, reason TEXT, session_id TEXT,
                error TEXT)""")
            con.execute("""CREATE INDEX IF NOT EXISTS idx_deliveries_hook
                ON deliveries (hook_id, received_at)""")
            con.execute("""CREATE TABLE IF NOT EXISTS dedup (
                hook_id TEXT, delivery_key TEXT, seen_at REAL,
                PRIMARY KEY (hook_id, delivery_key))""")
            con.execute("""CREATE TABLE IF NOT EXISTS coalesce_windows (
                hook_id TEXT, ckey TEXT, opened_at REAL, fire_at REAL,
                count INTEGER, payload_json TEXT, source TEXT,
                PRIMARY KEY (hook_id, ckey))""")
            con.commit()
        finally:
            con.close()

    # —— CRUD ——
    def create_hook(self, cfg: dict, *, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        clean = validate_hook_config(cfg, partial=False)
        hook_id = self._id_factory()
        secret = self._secret_factory()
        con = self._connect()
        try:
            con.execute(
                """INSERT INTO webhooks (id, name, provider, cwd, prompt_template,
                   filters_json, coalesce_json, dedup_source, rate_per_minute,
                   enabled, secret, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (hook_id, clean["name"], clean["provider"], clean["cwd"],
                 clean["prompt_template"], json.dumps(clean["filters"]),
                 json.dumps(clean["coalesce"]), clean["dedup_source"],
                 clean["rate_per_minute"], 1 if clean["enabled"] else 0,
                 secret, now, now))
            con.commit()
        finally:
            con.close()
        row = self.get_hook(hook_id)
        # secret 只在建立(或輪替)時回一次 —— 之後所有讀取一律 redact。
        public = self._public(row)
        public["secret"] = secret
        return public

    def update_hook(self, hook_id: str, cfg: dict, *,
                    rotate_secret: bool = False, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        row = self.get_hook(hook_id)
        if row is None:
            return None
        clean = validate_hook_config(cfg, partial=True)
        sets, params = [], []
        colmap = {
            "name": "name", "provider": "provider", "cwd": "cwd",
            "prompt_template": "prompt_template", "dedup_source": "dedup_source",
            "rate_per_minute": "rate_per_minute",
        }
        for key, col in colmap.items():
            if key in clean:
                sets.append(f"{col}=?")
                params.append(clean[key])
        if "filters" in clean:
            sets.append("filters_json=?")
            params.append(json.dumps(clean["filters"]))
        if "coalesce" in clean:
            sets.append("coalesce_json=?")
            params.append(json.dumps(clean["coalesce"]))
        if "enabled" in clean:
            sets.append("enabled=?")
            params.append(1 if clean["enabled"] else 0)
        new_secret = None
        if rotate_secret:
            new_secret = self._secret_factory()
            sets.append("secret=?")
            params.append(new_secret)
        sets.append("updated_at=?")
        params.append(now)
        params.append(hook_id)
        con = self._connect()
        try:
            con.execute(f"UPDATE webhooks SET {', '.join(sets)} WHERE id=?", params)
            con.commit()
        finally:
            con.close()
        public = self._public(self.get_hook(hook_id))
        if new_secret:
            public["secret"] = new_secret
        return public

    def delete_hook(self, hook_id: str) -> bool:
        con = self._connect()
        try:
            cur = con.execute("DELETE FROM webhooks WHERE id=?", (hook_id,))
            con.execute("DELETE FROM dedup WHERE hook_id=?", (hook_id,))
            con.execute("DELETE FROM coalesce_windows WHERE hook_id=?", (hook_id,))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()

    def get_hook(self, hook_id: str) -> dict | None:
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM webhooks WHERE id=?", (hook_id,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def list_hooks(self) -> list[dict]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT * FROM webhooks ORDER BY created_at DESC").fetchall()
            return [self._public(dict(r)) for r in rows]
        finally:
            con.close()

    def _public(self, row: dict | None) -> dict:
        if not row:
            return {}
        return {
            "id": row["id"], "name": row["name"], "provider": row["provider"],
            "cwd": row["cwd"], "prompt_template": row["prompt_template"],
            "filters": json.loads(row["filters_json"] or "[]"),
            "coalesce": json.loads(row["coalesce_json"] or "null"),
            "dedup_source": row["dedup_source"] or "",
            "rate_per_minute": row["rate_per_minute"],
            "enabled": bool(row["enabled"]),
            "has_secret": bool(row["secret"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "inbound_path": f"/webhook/{row['id']}",
        }

    # —— 速率(記憶體)——
    def _rate_ok(self, hook_id: str, limit: int, now: float) -> bool:
        window = self._rate.setdefault(hook_id, [])
        cutoff = now - RATE_WINDOW_SECS
        window[:] = [t for t in window if t > cutoff]
        if len(window) >= limit:
            return False
        window.append(now)
        return True

    # —— 去重 ——
    def _dedup_check(self, con, hook_id: str, key: str, now: float) -> bool:
        """只**查**不寫 —— 標記延後到確定要處理這筆(見 evaluate)。順手把超過
        保留期(7 天)的舊紀錄清掉。"""
        con.execute("DELETE FROM dedup WHERE seen_at < ?", (now - DEDUP_RETENTION_SECS,))
        row = con.execute(
            "SELECT seen_at FROM dedup WHERE hook_id=? AND delivery_key=?",
            (hook_id, key)).fetchone()
        return row is not None

    def _dedup_mark(self, con, hook_id: str, key: str, now: float) -> None:
        con.execute(
            "INSERT OR REPLACE INTO dedup (hook_id, delivery_key, seen_at) VALUES (?,?,?)",
            (hook_id, key, now))

    # —— deliveries ——
    def _record(self, con, hook_id: str, *, source: str, key: str,
                triggered: bool, reason: str, now: float,
                session_id: str = "", error: str = "") -> str:
        did = "dl_" + secrets.token_hex(8)
        con.execute(
            """INSERT INTO deliveries (id, hook_id, received_at, source,
               delivery_key, triggered, reason, session_id, error)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (did, hook_id, now, source, key, 1 if triggered else 0, reason,
             session_id, error))
        con.execute("DELETE FROM deliveries WHERE received_at < ?",
                    (now - DELIVERY_RETENTION_SECS,))
        return did

    def list_deliveries(self, hook_id: str, *, limit: int = 100) -> list[dict]:
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT * FROM deliveries WHERE hook_id=?
                   ORDER BY received_at DESC LIMIT ?""",
                (hook_id, int(limit))).fetchall()
            return [{
                "id": r["id"], "received_at": r["received_at"],
                "source": r["source"], "delivery_key": r["delivery_key"],
                "triggered": bool(r["triggered"]), "reason": r["reason"],
                "session_id": r["session_id"] or "", "error": r["error"] or "",
            } for r in rows]
        finally:
            con.close()

    def complete_delivery(self, delivery_id: str, *, session_id: str = "",
                          error: str = "") -> None:
        con = self._connect()
        try:
            con.execute(
                "UPDATE deliveries SET session_id=?, error=?, triggered=? WHERE id=?",
                (session_id, error, 0 if error else 1, delivery_id))
            con.commit()
        finally:
            con.close()

    # —— 核心:入站評估 ——
    def evaluate(self, hook_id: str, raw_body: bytes, headers: dict,
                 source: str, *, now: float | None = None) -> Decision:
        """對一筆入站做完整判定並落庫(trigger 以外都是終態)。

        順序:存在/啟用 → 簽章 → 去重 → 速率 → filters → coalesce → 觸發。
        簽章排在去重/速率之前 —— 沒通過簽章的請求連「算不算一次投遞」都不該
        影響速率預算,但**仍要記一筆** deliveries(規格第 2 點「失敗並記錄」)。
        """
        now = time.time() if now is None else now
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        row = self.get_hook(hook_id)
        if row is None:
            return Decision("not_found", 404, reason="hook_not_found")
        if not row["enabled"]:
            # 存在但停用:記一筆,回 404(對外不洩漏「停用」細節)。
            con = self._connect()
            try:
                did = self._record(con, hook_id, source=source, key="",
                                   triggered=False, reason="disabled", now=now)
                con.commit()
            finally:
                con.close()
            return Decision("disabled", 404, reason="disabled", delivery_id=did)

        sig_state = verify_signature(row["secret"], raw_body,
                                     headers.get(SIGNATURE_HEADER))
        if sig_state != "valid":
            reason = "missing_signature" if sig_state == "missing" else "bad_signature"
            con = self._connect()
            try:
                did = self._record(con, hook_id, source=source, key="",
                                   triggered=False, reason=reason, now=now)
                con.commit()
            finally:
                con.close()
            action = "missing_signature" if sig_state == "missing" else "bad_signature"
            return Decision(action, 401, reason=reason, delivery_id=did)

        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}

        dkey = delivery_key(row["dedup_source"], headers, raw_body)

        con = self._connect()
        try:
            if self._dedup_check(con, hook_id, dkey, now):
                did = self._record(con, hook_id, source=source, key=dkey,
                                   triggered=False, reason="duplicate", now=now)
                con.commit()
                return Decision("duplicate", 200, reason="duplicate",
                                delivery_id=did)

            # 速率排在標記去重**之前**:超速回 429 不佔去重格,送方(GitHub)
            # 重投時才能在額度回來後真的被處理 —— 否則重投一律被當重複吃掉。
            if not self._rate_ok(hook_id, row["rate_per_minute"], now):
                did = self._record(con, hook_id, source=source, key=dkey,
                                   triggered=False, reason="rate_limited", now=now)
                con.commit()
                return Decision("rate_limited", 429, reason="rate_limited",
                                delivery_id=did)

            # 到這裡代表這筆真的要被評估/處理 —— 標記去重(filter 沒過也算處理過,
            # 同一筆重投不必再跑一次)。
            self._dedup_mark(con, hook_id, dkey, now)

            filters = json.loads(row["filters_json"] or "[]")
            passed, reason = evaluate_filters(payload, filters)
            if not passed:
                did = self._record(con, hook_id, source=source, key=dkey,
                                   triggered=False, reason=reason, now=now)
                con.commit()
                return Decision("filtered", 200, reason=reason, delivery_id=did)

            coalesce = json.loads(row["coalesce_json"] or "null")
            if coalesce:
                ckey = coalesce_key(coalesce.get("key", ""), payload)
                window = int(coalesce.get("window_seconds") or COALESCE_MIN_WINDOW)
                self._open_or_extend_window(con, hook_id, ckey, window, payload,
                                            source, now)
                did = self._record(con, hook_id, source=source, key=dkey,
                                   triggered=False, reason="coalesced", now=now)
                con.commit()
                return Decision("coalesced", 202, reason="coalesced",
                                delivery_id=did,
                                extra={"coalesce_key": ckey})

            prompt = render_prompt(row["prompt_template"], payload)
            did = self._record(con, hook_id, source=source, key=dkey,
                               triggered=True, reason="triggered", now=now)
            con.commit()
            return Decision("trigger", 200, reason="triggered", delivery_id=did,
                            prompt=prompt, provider=row["provider"], cwd=row["cwd"])
        finally:
            con.close()

    # —— coalesce 視窗 ——
    def _open_or_extend_window(self, con, hook_id: str, ckey: str, window: int,
                               payload: Any, source: str, now: float) -> None:
        """同一 (hook, key) 在視窗內的後續投遞都併進既有視窗 —— **不重設**
        fire_at(第一筆開窗就定了觸發時刻),只累加計數並留最新 payload。
        不重設是刻意的:否則持續的事件流會把視窗無限往後推,永遠不觸發。"""
        row = con.execute(
            "SELECT count FROM coalesce_windows WHERE hook_id=? AND ckey=?",
            (hook_id, ckey)).fetchone()
        if row is None:
            con.execute(
                """INSERT INTO coalesce_windows (hook_id, ckey, opened_at, fire_at,
                   count, payload_json, source) VALUES (?,?,?,?,?,?,?)""",
                (hook_id, ckey, now, now + window, 1,
                 json.dumps(payload, ensure_ascii=False), source))
        else:
            con.execute(
                """UPDATE coalesce_windows SET count=count+1, payload_json=?
                   WHERE hook_id=? AND ckey=?""",
                (json.dumps(payload, ensure_ascii=False), hook_id, ckey))

    def pop_due_windows(self, *, now: float | None = None) -> list[dict]:
        """原子取出並刪除所有到期視窗,回每個的觸發資料(hook 設定 + 併攏後的
        payload + 已算好的 prompt)。重啟後仍能跑 —— 視窗在庫裡。"""
        now = time.time() if now is None else now
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT * FROM coalesce_windows WHERE fire_at <= ?", (now,)).fetchall()
            out = []
            for w in rows:
                hook = self.get_hook(w["hook_id"])
                con.execute(
                    "DELETE FROM coalesce_windows WHERE hook_id=? AND ckey=?",
                    (w["hook_id"], w["ckey"]))
                if hook is None or not hook["enabled"]:
                    continue
                try:
                    payload = json.loads(w["payload_json"] or "{}")
                except ValueError:
                    payload = {}
                out.append({
                    "hook_id": w["hook_id"], "provider": hook["provider"],
                    "cwd": hook["cwd"], "ckey": w["ckey"], "count": w["count"],
                    "source": (w["source"] or "coalesce") + "+coalesce",
                    "prompt": render_prompt(hook["prompt_template"], payload),
                })
            con.commit()
            return out
        finally:
            con.close()

    def record_coalesce_fire(self, hook_id: str, *, source: str, session_id: str = "",
                             error: str = "", count: int = 0,
                             now: float | None = None) -> str:
        now = time.time() if now is None else now
        con = self._connect()
        try:
            did = self._record(con, hook_id, source=source,
                               key=f"coalesce:{count}",
                               triggered=not error, reason="triggered" if not error
                               else "trigger_error", now=now,
                               session_id=session_id, error=error)
            con.commit()
            return did
        finally:
            con.close()
