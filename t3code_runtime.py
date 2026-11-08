"""Small, durable primitives for the T3Code follow-up contract.

The bridge remains the compatibility surface.  This module owns only the new
tables and pure-ish operations so migrations, redaction, and idempotency can be
tested without importing the full FastAPI application.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any


SECRET_KEYS = frozenset({
    "api_key", "apikey", "authorization", "cookie", "password", "secret",
    "token", "access_token", "refresh_token", "private_key", "credential",
})
_SECRET_WORD = re.compile(
    r"(?:api[_-]?key|authorization|cookie|password|secret|access[_-]?token|"
    r"refresh[_-]?token|private[_-]?key|credential)\s*[:=]",
    re.IGNORECASE,
)


class T3CodeError(ValueError):
    """A client-correctable T3Code contract error."""


class BindingConflict(T3CodeError):
    """The caller must refresh its stable thread or binding identity."""


BINDING_STATES = frozenset({"active", "dead", "detached"})
# When an active binding is replaced, the outgoing row must land in a
# terminal, auditable state: `dead` (the provider-native session is gone or
# wedged — e.g. the CX writer-lock saga) or `detached` (deliberate hand-off,
# the native session still exists but no longer backs this conversation).
BINDING_RETIRE_STATES = frozenset({"dead", "detached"})

CONTEXT_TRANSITIONS = {
    "dry_run": {"approved", "expired"},
    "approved": {"target_created", "failed", "expired"},
    "target_created": {"applied", "failed", "expired"},
    "applied": set(),
    "failed": set(),
    "expired": set(),
}


def _now() -> float:
    return time.time()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def ensure_schema(db_path: str) -> None:
    """Create the additive T3Code schema without touching legacy tables."""
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(db_path, timeout=30)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("""CREATE TABLE IF NOT EXISTS app_threads(
            id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            version INTEGER NOT NULL DEFAULT 1)""")
        con.execute("""CREATE TABLE IF NOT EXISTS provider_threads(
            id TEXT PRIMARY KEY,
            app_thread_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            native_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            capabilities TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(provider, native_id),
            FOREIGN KEY(app_thread_id) REFERENCES app_threads(id))""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_provider_thread_app "
                    "ON provider_threads(app_thread_id, updated_at)")
        # AppThread ≠ ProviderThread dual-id binding ledger (t3code
        # core-graph).  One row per (app thread ⇄ provider-native session)
        # attachment; rebinding never rewrites history — the old row is
        # flipped to dead/detached and a fresh active row is appended, so the
        # full chain stays auditable.  `provider_thread_id` here is the
        # bridge-native session id the app already speaks (codex:xxx /
        # claude_code:name / …), which keeps this layer purely additive on
        # top of the legacy session surface.
        con.execute("""CREATE TABLE IF NOT EXISTS app_thread_bindings(
            id TEXT PRIMARY KEY,
            app_thread_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            provider_thread_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',
            reason TEXT,
            bound_at REAL NOT NULL,
            unbound_at REAL,
            FOREIGN KEY(app_thread_id) REFERENCES app_threads(id))""")
        # Invariants are enforced at the storage layer, not only in Python:
        # at most one active binding per app thread, and one provider-native
        # thread can be actively owned by at most one app thread.
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_binding_active_thread "
                    "ON app_thread_bindings(app_thread_id) WHERE state='active'")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_binding_active_provider "
                    "ON app_thread_bindings(provider, provider_thread_id) "
                    "WHERE state='active'")
        con.execute("CREATE INDEX IF NOT EXISTS idx_binding_thread_time "
                    "ON app_thread_bindings(app_thread_id, bound_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_binding_provider_history "
                    "ON app_thread_bindings(provider, provider_thread_id, app_thread_id)")
        con.execute("""CREATE TABLE IF NOT EXISTS context_transfers(
            id TEXT PRIMARY KEY,
            app_thread_id TEXT NOT NULL,
            source_provider_thread_id TEXT NOT NULL,
            target_provider TEXT NOT NULL,
            target_provider_thread_id TEXT,
            envelope TEXT NOT NULL,
            envelope_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'dry_run',
            created_at REAL NOT NULL,
            expires_at REAL,
            FOREIGN KEY(app_thread_id) REFERENCES app_threads(id))""")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_transfer_hash "
                    "ON context_transfers(id, envelope_hash)")
        con.execute("""CREATE TABLE IF NOT EXISTS checkpoints(
            id TEXT PRIMARY KEY,
            app_thread_id TEXT NOT NULL,
            provider_thread_id TEXT,
            cwd TEXT NOT NULL,
            head TEXT,
            dirty_summary TEXT NOT NULL DEFAULT '',
            artifact_manifest_hash TEXT NOT NULL,
            manifest TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'dry_run',
            created_at REAL NOT NULL,
            FOREIGN KEY(app_thread_id) REFERENCES app_threads(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS usage_samples(
            id TEXT PRIMARY KEY,
            session TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            total_tokens INTEGER,
            estimated_cost_usd REAL,
            source TEXT NOT NULL,
            dedupe_key TEXT UNIQUE,
            recorded_at REAL NOT NULL)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_usage_session_time "
                    "ON usage_samples(session, recorded_at)")
        con.execute("""CREATE TABLE IF NOT EXISTS live_activity_tokens(
            session_id TEXT PRIMARY KEY,
            token TEXT NOT NULL,
            bundle_id TEXT NOT NULL,
            updated_at REAL NOT NULL)""")
        # 第二期 ActivityKit push:一顆 Live Activity 一列(同一 session 可能
        # 同時有多顆 activity/多台裝置)。token 會輪替,同 activity_id 重報
        # 即覆蓋(冪等);410/turn end 由 bridge 清列。
        con.execute("""CREATE TABLE IF NOT EXISTS activity_tokens(
            activity_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            push_token TEXT NOT NULL,
            device_env TEXT NOT NULL DEFAULT 'prod',
            bundle_id TEXT NOT NULL,
            updated_at REAL NOT NULL)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_activity_tokens_session "
                    "ON activity_tokens(session_id)")
        con.execute("""CREATE TABLE IF NOT EXISTS credential_handoffs(
            id TEXT PRIMARY KEY,
            source_host_id TEXT NOT NULL,
            target_host_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            audience TEXT NOT NULL,
            nonce_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'issued',
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            consumed_at REAL,
            last_error TEXT)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_credential_handoffs_expiry "
                    "ON credential_handoffs(status, expires_at)")
        con.execute("""CREATE TABLE IF NOT EXISTS t3code_meta(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL)""")
        _ensure_columns(con, "context_transfers", {
            "approval_id": "TEXT", "last_error": "TEXT", "updated_at": "REAL",
        })
        _ensure_columns(con, "checkpoints", {
            "ref_name": "TEXT", "approval_id": "TEXT", "restore_status": "TEXT",
            "restored_at": "REAL", "last_error": "TEXT",
        })
        con.commit()
    finally:
        con.close()


def _ensure_columns(con: sqlite3.Connection, table: str,
                    columns: dict[str, str]) -> None:
    present = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in present:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return con


def create_app_thread(db_path: str, owner: str, title: str,
                      thread_id: str | None = None, *,
                      initial_binding: tuple[str, str] | None = None) -> dict:
    ensure_schema(db_path)
    owner = str(owner or "").strip()
    title = str(title or "").strip()[:200]
    if not owner or not title:
        raise T3CodeError("owner and title are required")
    tid = thread_id or _id("app")
    now = _now()
    con = _connect(db_path)
    try:
        target = _binding_target(*initial_binding) if initial_binding else None
        con.execute("BEGIN IMMEDIATE")
        if target:
            _assert_target_free(con, *target, tid)
        con.execute("INSERT INTO app_threads(id,owner,title,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (tid, owner, title, "active", now, now))
        if target:
            _insert_binding(con, _id("bind"), tid, *target, now)
        con.commit()
        return {"id": tid, "owner": owner, "title": title, "status": "active",
                "created_at": now, "updated_at": now, "version": 1,
                "provider_threads": []}
    except sqlite3.IntegrityError as exc:
        raise T3CodeError("app thread or binding already exists") from exc
    finally:
        con.close()


def link_provider_thread(db_path: str, app_thread_id: str, provider: str,
                         native_id: str, capabilities: dict | None = None,
                         provider_thread_id: str | None = None) -> dict:
    ensure_schema(db_path)
    provider = str(provider or "").strip().lower()
    native_id = str(native_id or "").strip()
    if not provider or not native_id:
        raise T3CodeError("provider and native_id are required")
    now = _now()
    pid = provider_thread_id or _id("provider")
    caps = capabilities if isinstance(capabilities, dict) else {}
    con = _connect(db_path)
    try:
        if not con.execute("SELECT 1 FROM app_threads WHERE id=?",
                           (app_thread_id,)).fetchone():
            raise T3CodeError("unknown app_thread")
        existing = con.execute(
            "SELECT * FROM provider_threads WHERE provider=? AND native_id=?",
            (provider, native_id)).fetchone()
        if existing:
            if existing["app_thread_id"] != app_thread_id:
                raise T3CodeError("native provider thread already belongs to another app_thread")
            pid = existing["id"]
            con.execute("UPDATE provider_threads SET status='active', capabilities=?, "
                        "updated_at=? WHERE id=?", (_json(caps), now, pid))
        else:
            con.execute("""INSERT INTO provider_threads
                (id,app_thread_id,provider,native_id,status,capabilities,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                        (pid, app_thread_id, provider, native_id, "active",
                         _json(caps), now, now))
        con.commit()
        row = con.execute("SELECT * FROM provider_threads WHERE id=?", (pid,)).fetchone()
        out = _row(row) or {}
        out["capabilities"] = json.loads(out.get("capabilities") or "{}")
        return out
    finally:
        con.close()


def get_app_thread(db_path: str, thread_id: str, *, with_bindings: bool = False) -> dict | None:
    ensure_schema(db_path)
    con = _connect(db_path)
    try:
        con.execute("BEGIN")
        row = con.execute("SELECT * FROM app_threads WHERE id=?", (thread_id,)).fetchone()
        if row is None:
            return None
        out = _row(row) or {}
        children = con.execute(
            "SELECT * FROM provider_threads WHERE app_thread_id=? ORDER BY created_at",
            (thread_id,)).fetchall()
        out["provider_threads"] = []
        for child in children:
            item = _row(child) or {}
            item["capabilities"] = json.loads(item.get("capabilities") or "{}")
            out["provider_threads"].append(item)
        if with_bindings:
            chain = con.execute("SELECT * FROM app_thread_bindings WHERE app_thread_id=? "
                                "ORDER BY rowid", (thread_id,)).fetchall()
            out["bindings"] = [dict(r) for r in chain]
            out["binding"] = next((r for r in out["bindings"] if r["state"] == "active"), None)
        return out
    finally:
        con.close()


def _binding_target(provider: Any, provider_thread_id: Any) -> tuple[str, str]:
    provider = str(provider or "").strip().lower()
    provider_thread_id = str(provider_thread_id or "").strip()
    if not provider or not provider_thread_id:
        raise T3CodeError("provider and provider_thread_id are required")
    return provider, provider_thread_id


def _require_app_thread(con: sqlite3.Connection, app_thread_id: str) -> None:
    if not con.execute("SELECT 1 FROM app_threads WHERE id=?",
                       (app_thread_id,)).fetchone():
        raise T3CodeError("unknown app_thread")


def _active_binding(con: sqlite3.Connection, app_thread_id: str) -> dict | None:
    return _row(con.execute(
        "SELECT * FROM app_thread_bindings WHERE app_thread_id=? AND state='active'",
        (app_thread_id,)).fetchone())


def _insert_binding(con, bid, app_thread_id, provider, provider_thread_id, now):
    con.execute("""INSERT INTO app_thread_bindings
        (id,app_thread_id,provider,provider_thread_id,state,bound_at)
        VALUES(?,?,?,?,'active',?)""", (bid, app_thread_id, provider, provider_thread_id, now))


def resolve_provider_binding(db_path: str, provider: str, provider_thread_id: str) -> dict | None:
    """Read-only lookup, including retired bindings; never guess between owners."""
    target = _binding_target(provider, provider_thread_id)
    path = Path(db_path).resolve()
    if not path.exists():
        return None
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN")
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                           "AND name='app_thread_bindings'").fetchone():
            return None
        owners = con.execute("SELECT DISTINCT app_thread_id FROM app_thread_bindings "
                             "WHERE provider=? AND provider_thread_id=? LIMIT 2", target).fetchall()
        if not owners:
            return None
        if len(owners) != 1:
            raise BindingConflict("provider session was reused; a stable app thread id is required")
        tid = owners[0]["app_thread_id"]
        return {"app_thread_id": tid, "binding": _active_binding(con, tid)}
    finally:
        con.close()


def _assert_target_free(con: sqlite3.Connection, provider: str,
                        provider_thread_id: str, app_thread_id: str) -> None:
    other = con.execute(
        "SELECT app_thread_id FROM app_thread_bindings "
        "WHERE provider=? AND provider_thread_id=? AND state='active'",
        (provider, provider_thread_id)).fetchone()
    if other and other["app_thread_id"] != app_thread_id:
        raise T3CodeError(
            "provider thread is actively bound to another app_thread")


def active_binding_owner(db_path: str, provider: str,
                         provider_thread_id: str) -> str | None:
    """Return the app_thread_id that actively owns a provider thread, if any.

    Lets callers pre-validate a bind target before creating an app thread so
    a rejected bind does not leave an orphan thread behind.
    """
    ensure_schema(db_path)
    provider, provider_thread_id = _binding_target(provider, provider_thread_id)
    con = _connect(db_path)
    try:
        row = con.execute(
            "SELECT app_thread_id FROM app_thread_bindings "
            "WHERE provider=? AND provider_thread_id=? AND state='active'",
            (provider, provider_thread_id)).fetchone()
        return row["app_thread_id"] if row else None
    finally:
        con.close()


def bind_app_thread(db_path: str, app_thread_id: str, provider: str,
                    provider_thread_id: str,
                    binding_id: str | None = None) -> dict:
    """Attach a provider-native thread as the live backing of an app thread.

    Idempotent when the same target is already the active binding.  A
    different active binding must go through :func:`rebind_app_thread` so the
    retirement state (dead/detached) and reason land in the audit chain.
    """
    ensure_schema(db_path)
    provider, provider_thread_id = _binding_target(provider, provider_thread_id)
    now = _now()
    con = _connect(db_path)
    try:
        _require_app_thread(con, app_thread_id)
        current = _active_binding(con, app_thread_id)
        if current:
            if (current["provider"] == provider
                    and current["provider_thread_id"] == provider_thread_id):
                return current
            raise T3CodeError(
                "app_thread already has an active binding; use rebind")
        _assert_target_free(con, provider, provider_thread_id, app_thread_id)
        bid = binding_id or _id("bind")
        try:
            _insert_binding(con, bid, app_thread_id, provider, provider_thread_id, now)
        except sqlite3.IntegrityError as exc:
            # Lost a race against another writer: the partial unique indexes
            # are the authoritative guard, surface it as a contract error.
            raise T3CodeError("binding conflicts with an existing active binding") from exc
        con.execute("UPDATE app_threads SET updated_at=? WHERE id=?",
                    (now, app_thread_id))
        con.commit()
        return _row(con.execute(
            "SELECT * FROM app_thread_bindings WHERE id=?", (bid,)).fetchone()) or {}
    finally:
        con.close()


def current_binding(db_path: str, app_thread_id: str) -> dict | None:
    """Return the single active binding of an app thread, if any."""
    ensure_schema(db_path)
    con = _connect(db_path)
    try:
        return _active_binding(con, app_thread_id)
    finally:
        con.close()


def list_bindings(db_path: str, app_thread_id: str) -> list[dict]:
    """Return the full binding chain, oldest first (audit view)."""
    ensure_schema(db_path)
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT * FROM app_thread_bindings WHERE app_thread_id=? "
            "ORDER BY bound_at, rowid", (app_thread_id,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def rebind_app_thread(db_path: str, app_thread_id: str, provider: str,
                      provider_thread_id: str, reason: str = "",
                      prior_state: str = "dead",
                      binding_id: str | None = None, *, expected_binding_id: str | None = None) -> dict:
    """Swap the live provider thread under a stable app conversation.

    The outgoing active row is retired to ``prior_state`` (dead|detached)
    with ``reason`` and ``unbound_at`` recorded; a fresh active row is
    appended.  History is never rewritten.  Rebinding to the current target
    is a no-op (``changed: False``); rebinding when no binding is active
    simply binds.  Both halves commit in one transaction so the chain can
    never be observed with zero or two active rows.
    """
    ensure_schema(db_path)
    provider, provider_thread_id = _binding_target(provider, provider_thread_id)
    prior_state = str(prior_state or "dead").strip().lower()
    if prior_state not in BINDING_RETIRE_STATES:
        raise T3CodeError("prior_state must be one of: dead, detached")
    reason = str(reason or "").strip()[:500]
    now = _now()
    con = _connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        _require_app_thread(con, app_thread_id)
        current = _active_binding(con, app_thread_id)
        if current and (current["provider"] == provider
                        and current["provider_thread_id"] == provider_thread_id):
            return {"changed": False, "binding": current, "previous": None}
        if expected_binding_id is not None and (not current or current["id"] != expected_binding_id):
            raise BindingConflict("active binding changed; refresh before rebinding")
        _assert_target_free(con, provider, provider_thread_id, app_thread_id)
        previous = None
        if current:
            con.execute("""UPDATE app_thread_bindings
                SET state=?, reason=?, unbound_at=? WHERE id=? AND state='active'""",
                        (prior_state, reason or None, now, current["id"]))
            previous = _row(con.execute(
                "SELECT * FROM app_thread_bindings WHERE id=?",
                (current["id"],)).fetchone())
        bid = binding_id or _id("bind")
        try:
            _insert_binding(con, bid, app_thread_id, provider, provider_thread_id, now)
        except sqlite3.IntegrityError as exc:
            raise T3CodeError("binding conflicts with an existing active binding") from exc
        con.execute("UPDATE app_threads SET updated_at=? WHERE id=?",
                    (now, app_thread_id))
        con.commit()
        binding = _row(con.execute(
            "SELECT * FROM app_thread_bindings WHERE id=?", (bid,)).fetchone()) or {}
        return {"changed": True, "binding": binding, "previous": previous}
    finally:
        con.close()


def project_legacy_delegations(db_path: str, rows: list[dict]) -> dict:
    """Project legacy delegation handles into stable v3 ids.

    This is deliberately additive.  The v1 delegation registry remains the
    source of operational truth until the client cutover; rerunning the
    projection is deterministic and never deletes an AppThread.
    """
    ensure_schema(db_path)
    projected = skipped = 0
    now = _now()
    con = _connect(db_path)
    try:
        for raw in rows or []:
            row = dict(raw or {})
            provider = str(row.get("provider") or "").strip().lower()
            native = str(row.get("provider_session_id") or
                         row.get("codex_thread_id") or
                         row.get("cc_session_name") or "").strip()
            if not provider or not native:
                skipped += 1
                continue
            owner = str(row.get("parent_session") or
                        row.get("parent_persona") or "unknown").strip()
            work_order = str(row.get("work_order") or row.get("id") or "").strip()
            app_key = f"{owner}:{work_order}"
            app_id = "app_legacy_" + hashlib.sha256(app_key.encode()).hexdigest()[:24]
            title = str(row.get("title") or row.get("objective") or work_order
                        or "Legacy thread").strip()[:200]
            if not con.execute("SELECT 1 FROM app_threads WHERE id=?", (app_id,)).fetchone():
                con.execute("""INSERT INTO app_threads
                    (id,owner,title,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?)""",
                            (app_id, owner, title, "active", now, now))
            else:
                con.execute("UPDATE app_threads SET title=?,updated_at=? WHERE id=?",
                            (title, now, app_id))
            existing = con.execute(
                "SELECT id,app_thread_id FROM provider_threads WHERE provider=? AND native_id=?",
                (provider, native)).fetchone()
            if existing and existing["app_thread_id"] != app_id:
                skipped += 1
                continue
            child_status = "detached" if str(row.get("status") or "").lower() in {
                "done", "failed", "archived", "cancelled", "expired"
            } else "active"
            pid = existing["id"] if existing else (
                "provider_legacy_" + hashlib.sha256(
                    f"{provider}:{native}".encode()).hexdigest()[:24])
            if existing:
                con.execute("UPDATE provider_threads SET status=?,updated_at=? WHERE id=?",
                            (child_status, now, pid))
            else:
                con.execute("""INSERT INTO provider_threads
                    (id,app_thread_id,provider,native_id,status,capabilities,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                            (pid, app_id, provider, native, child_status,
                             _json({"projected_from": "delegation",
                                    "delegation_id": row.get("id")}), now, now))
            projected += 1
        con.execute("""INSERT INTO t3code_meta(key,value,updated_at)
            VALUES('legacy_projection_version','1',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                    (now,))
        con.commit()
        return {"projected": projected, "skipped": skipped, "version": 1}
    finally:
        con.close()


def _contains_secret(value: Any, path: str = "") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key).strip().lower().replace("-", "_")
            if key_text in SECRET_KEYS or _SECRET_WORD.search(key_text + ":"):
                return f"{path}.{key}".strip(".")
            found = _contains_secret(child, f"{path}.{key}".strip("."))
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _contains_secret(child, f"{path}[{index}]")
            if found:
                return found
    elif isinstance(value, str) and _SECRET_WORD.search(value):
        return path or "value"
    return None


_HANDOFF_FIELDS = frozenset({
    "version", "handoff_id", "source_host_id", "target_host_id", "provider",
    "audience", "expires_at", "nonce", "ttl_secs",
})


def validate_credential_handoff_payload(value: Any) -> dict:
    """Validate metadata-only handoff input and reject secret material.

    This is intentionally separate from context transfer.  The returned value
    contains no credential payload and is safe to pass to the ticket layer.
    Unknown non-secret fields are ignored so clients can add display metadata
    without making older bridges fail; secret-looking fields are never ignored.
    """
    if not isinstance(value, dict):
        raise T3CodeError("credential handoff body must be an object")
    secret_path = _contains_secret(value)
    if secret_path:
        raise T3CodeError(f"credential handoff contains secret material at {secret_path}")
    return {str(key): child for key, child in value.items()
            if str(key) in _HANDOFF_FIELDS}


def _handoff_text(value: Any, name: str, limit: int = 200) -> str:
    text = str(value or "").strip()
    if not text:
        raise T3CodeError(f"{name} is required")
    if len(text) > limit:
        raise T3CodeError(f"{name} is too long")
    return text


def create_credential_handoff(db_path: str, source_host_id: str,
                             target_host_id: str, provider: str,
                             audience: str, ttl_secs: int = 300,
                             handoff_id: str | None = None) -> dict:
    """Issue a one-time metadata ticket without storing a credential.

    The returned nonce is the handoff capability and must only travel over the
    already-authenticated host-to-host channel.  The database stores its hash,
    never the nonce or an API key.
    """
    ensure_schema(db_path)
    source = _handoff_text(source_host_id, "source_host_id")
    target = _handoff_text(target_host_id, "target_host_id")
    provider = _handoff_text(provider, "provider", 80).lower()
    audience = _handoff_text(audience, "audience", 300)
    try:
        ttl = int(ttl_secs)
    except (TypeError, ValueError) as exc:
        raise T3CodeError("ttl_secs must be an integer") from exc
    ttl = max(30, min(ttl, 900))
    hid = _handoff_text(handoff_id or _id("handoff"), "handoff_id", 120)
    nonce = secrets.token_urlsafe(32)
    nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
    now = _now()
    expires = now + ttl
    con = _connect(db_path)
    try:
        existing = con.execute("SELECT * FROM credential_handoffs WHERE id=?",
                               (hid,)).fetchone()
        if existing:
            raise T3CodeError("handoff_id already exists")
        con.execute("""INSERT INTO credential_handoffs
            (id,source_host_id,target_host_id,provider,audience,nonce_hash,
             status,created_at,expires_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
                    (hid, source, target, provider, audience, nonce_hash,
                     "issued", now, expires))
        con.commit()
        return {"version": 1, "handoff_id": hid,
                "source_host_id": source, "target_host_id": target,
                "provider": provider, "audience": audience,
                "expires_at": expires, "nonce": nonce,
                "status": "issued"}
    finally:
        con.close()


def get_credential_handoff(db_path: str, handoff_id: str) -> dict | None:
    """Return public ticket metadata; never return nonce_hash."""
    ensure_schema(db_path)
    con = _connect(db_path)
    try:
        row = _row(con.execute(
            "SELECT id,source_host_id,target_host_id,provider,audience,status,"
            "created_at,expires_at,consumed_at,last_error "
            "FROM credential_handoffs WHERE id=?", (str(handoff_id),)).fetchone())
        if not row:
            return None
        return {"version": 1, "handoff_id": row["id"],
                "source_host_id": row["source_host_id"],
                "target_host_id": row["target_host_id"],
                "provider": row["provider"], "audience": row["audience"],
                "status": row["status"], "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "consumed_at": row["consumed_at"],
                "last_error": row["last_error"]}
    finally:
        con.close()


def consume_credential_handoff(db_path: str, handoff_id: str, nonce: str,
                               target_host_id: str, provider: str,
                               audience: str, now: float | None = None) -> dict:
    """Consume a ticket once after checking every target binding."""
    ensure_schema(db_path)
    hid = _handoff_text(handoff_id, "handoff_id", 120)
    nonce = _handoff_text(nonce, "nonce", 200)
    target = _handoff_text(target_host_id, "target_host_id")
    provider = _handoff_text(provider, "provider", 80).lower()
    audience = _handoff_text(audience, "audience", 300)
    current_time = _now() if now is None else float(now)
    con = _connect(db_path)
    try:
        row = con.execute("SELECT * FROM credential_handoffs WHERE id=?",
                          (hid,)).fetchone()
        if row is None:
            raise T3CodeError("unknown credential handoff")
        if row["status"] != "issued":
            raise T3CodeError("credential handoff is no longer available")
        if current_time >= float(row["expires_at"]):
            con.execute("UPDATE credential_handoffs SET status='expired',last_error=? "
                        "WHERE id=?", ("expired", hid))
            con.commit()
            raise T3CodeError("credential handoff has expired")
        if (row["target_host_id"] != target or row["provider"] != provider
                or row["audience"] != audience):
            raise T3CodeError("credential handoff target binding mismatch")
        supplied_hash = hashlib.sha256(nonce.encode()).hexdigest()
        if not hmac.compare_digest(supplied_hash, row["nonce_hash"]):
            raise T3CodeError("credential handoff nonce mismatch")
        con.execute("UPDATE credential_handoffs SET status='consumed',consumed_at=? "
                    "WHERE id=? AND status='issued'", (current_time, hid))
        if con.total_changes != 1:
            raise T3CodeError("credential handoff was already consumed")
        con.commit()
        return {"version": 1, "handoff_id": hid, "target_host_id": target,
                "provider": provider, "audience": audience,
                "status": "consumed", "consumed_at": current_time}
    finally:
        con.close()


def _normalise_transfer(value: Any, name: str) -> Any:
    if value is None:
        return [] if name in {"decisions", "artifacts", "pending"} else ""
    if name == "summary":
        return str(value).strip()[:12000]
    if not isinstance(value, list):
        raise T3CodeError(f"{name} must be a list")
    return value[:100]


def create_context_transfer(db_path: str, app_thread_id: str,
                            source_provider_thread_id: str,
                            target_provider: str, summary: str,
                            decisions: list | None = None,
                            artifacts: list | None = None,
                            pending: list | None = None,
                            transfer_id: str | None = None,
                            expires_at: float | None = None) -> dict:
    ensure_schema(db_path)
    payload = {
        "version": 1,
        "source_provider_thread_id": source_provider_thread_id,
        "target_provider": str(target_provider or "").strip().lower(),
        "summary": _normalise_transfer(summary, "summary"),
        "decisions": _normalise_transfer(decisions, "decisions"),
        "artifacts": _normalise_transfer(artifacts, "artifacts"),
        "pending": _normalise_transfer(pending, "pending"),
    }
    if not payload["source_provider_thread_id"] or not payload["target_provider"]:
        raise T3CodeError("source_provider_thread_id and target_provider are required")
    secret_path = _contains_secret(payload)
    if secret_path:
        raise T3CodeError(f"context transfer contains secret material at {secret_path}")
    envelope = _json(payload)
    digest = hashlib.sha256(envelope.encode()).hexdigest()
    transfer_id = transfer_id or _id("ctx")
    now = _now()
    con = _connect(db_path)
    try:
        if not con.execute("SELECT 1 FROM app_threads WHERE id=?", (app_thread_id,)).fetchone():
            raise T3CodeError("unknown app_thread")
        if not con.execute("SELECT 1 FROM provider_threads WHERE id=? AND app_thread_id=?",
                           (source_provider_thread_id, app_thread_id)).fetchone():
            raise T3CodeError("source provider thread is not attached to app_thread")
        existing = con.execute("SELECT * FROM context_transfers WHERE id=?",
                               (transfer_id,)).fetchone()
        if existing:
            if existing["envelope_hash"] != digest:
                raise T3CodeError("transfer_id already exists with different content")
            out = _row(existing) or {}
            out["envelope"] = json.loads(out["envelope"])
            return out
        con.execute("""INSERT INTO context_transfers
            (id,app_thread_id,source_provider_thread_id,target_provider,envelope,
             envelope_hash,status,created_at,expires_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (transfer_id, app_thread_id, source_provider_thread_id,
                     payload["target_provider"], envelope, digest, "dry_run",
                     now, expires_at, now))
        con.commit()
        return {"id": transfer_id, "app_thread_id": app_thread_id,
                "source_provider_thread_id": source_provider_thread_id,
                "target_provider": payload["target_provider"],
                "envelope": payload, "envelope_hash": digest,
                "status": "dry_run", "created_at": now, "expires_at": expires_at}
    finally:
        con.close()


def get_context_transfer(db_path: str, transfer_id: str) -> dict | None:
    ensure_schema(db_path)
    con = _connect(db_path)
    try:
        row = _row(con.execute("SELECT * FROM context_transfers WHERE id=?",
                              (transfer_id,)).fetchone())
        if not row:
            return None
        row["envelope"] = json.loads(row.get("envelope") or "{}")
        return row
    finally:
        con.close()


def transition_context_transfer(db_path: str, transfer_id: str,
                                next_status: str, approval_id: str | None = None,
                                target_provider_thread_id: str | None = None,
                                error: str | None = None) -> dict:
    ensure_schema(db_path)
    next_status = str(next_status or "").strip().lower()
    if next_status not in CONTEXT_TRANSITIONS:
        raise T3CodeError("unsupported context transfer status")
    now = _now()
    con = _connect(db_path)
    try:
        current = con.execute("SELECT * FROM context_transfers WHERE id=?",
                              (transfer_id,)).fetchone()
        if current is None:
            raise T3CodeError("unknown context transfer")
        old_status = str(current["status"] or "dry_run")
        if next_status not in CONTEXT_TRANSITIONS.get(old_status, set()):
            if old_status == next_status:
                return get_context_transfer(db_path, transfer_id) or {}
            raise T3CodeError(f"invalid context transfer transition: {old_status}->{next_status}")
        if next_status == "approved" and not str(approval_id or "").strip():
            raise T3CodeError("approval_id is required before applying a transfer")
        if next_status in {"target_created", "applied"} and not str(target_provider_thread_id or "").strip():
            raise T3CodeError("target_provider_thread_id is required")
        con.execute("""UPDATE context_transfers SET status=?,approval_id=?,
            target_provider_thread_id=COALESCE(?,target_provider_thread_id),
            last_error=?,updated_at=? WHERE id=?""",
                    (next_status, approval_id, target_provider_thread_id,
                     error, now, transfer_id))
        con.commit()
        return get_context_transfer(db_path, transfer_id) or {}
    finally:
        con.close()


# ── CC↔CX 接力 v1:交接摘要萃取(2026-11-06)────────────────────────────────
# 來源真相是 event_log 的持久里程碑(card.milestone,carddigest P3-lite 只落
# final=True 的終態卡)。v1 刻意**不呼叫 LLM 總結** —— 原文拼接,省一跳、零
# 幻覺;LLM 總結列 v2。這層只讀 DB、純函式,bridge 端點只負責 spawn + rebind。

TRANSFER_PROMPT_MAX_CHARS = 8000
TRANSFER_TURN_TEXT_MAX = 2000
TRANSFER_MAX_TURNS = 6


def extract_transfer_turns(db_path: str, session: str, max_turns: int = TRANSFER_MAX_TURNS,
                           scan_limit: int = 400) -> list[dict]:
    """把 session 的持久里程碑整理成「回合」:使用者請求 + 最終回覆。

    一個 user 卡開新回合;回合內最後一張 assistant 終態卡就是「最終回覆」
    (中途的工具摘要卡會被後到的覆蓋,正是契約要的語意)。同文字的連續
    user 卡(input.accepted 回聲 + transcript 合併可能各落一筆)去重。
    event_log 不存在(純 t3 schema 的新庫)→ 無歷史,回空list,不炸。
    """
    con = _connect(db_path)
    try:
        try:
            rows = con.execute(
                "SELECT id,payload FROM event_log "
                "WHERE session=? AND type='card.milestone' "
                "ORDER BY id DESC LIMIT ?",
                (str(session), max(1, min(int(scan_limit), 2000)))).fetchall()
        except sqlite3.OperationalError:
            return []
    finally:
        con.close()
    turns: list[dict] = []
    for row in reversed(rows):          # 回到時間序
        try:
            card = (json.loads(row["payload"] or "{}") or {}).get("card") or {}
        except (TypeError, ValueError):
            continue
        role = card.get("role")
        body = card.get("body") or {}
        text = str(body.get("text") or body.get("fallback_text") or "").strip()
        if role not in ("user", "assistant") or not text:
            continue
        if role == "user":
            if turns and turns[-1]["user"] == text and not turns[-1]["reply"]:
                continue                # 同文字回聲卡,不開假回合
            turns.append({"user": text, "reply": ""})
        else:
            if not turns:
                turns.append({"user": "", "reply": text})
            else:
                turns[-1]["reply"] = text
    return turns[-max(1, int(max_turns)):]


def build_transfer_prompt(db_path: str, session: str, source_provider: str,
                          max_turns: int = TRANSFER_MAX_TURNS,
                          max_chars: int = TRANSFER_PROMPT_MAX_CHARS) -> dict:
    """組繁中交接 prompt(目標會話的首訊)。

    長度上限 ~max_chars:先掐單段(每段 TRANSFER_TURN_TEXT_MAX),再從最舊
    的回合開始丟;任何截斷都在 prompt 本文留痕(「…[截斷]」/「較早內容已
    截斷」)並以 truncated=True 回報,呼叫端落進 thread.transferred 事件。
    無里程碑 → 註明「無歷史脈絡」照樣轉(graceful,契約拍板)。
    """
    max_chars = max(1000, min(int(max_chars), 16000))
    turns = extract_transfer_turns(db_path, session, max_turns=max_turns)
    header = (f"以下是先前與另一個 agent 的工作脈絡交接(來源 provider:"
              f"{str(source_provider or '').strip() or '未知'},session:{session})。"
              "你是接手的新會話,請先消化以下脈絡,接續執行未完成的工作,"
              "不要重做已完成的部分。")
    footer = "── 交接脈絡結束 ──\n請接續以上工作;若脈絡不足,先向使用者確認再動手。"
    if not turns:
        prompt = (header + "\n\n(無歷史脈絡:來源會話沒有可萃取的持久里程碑,"
                  "請等待使用者的下一步指示。)\n\n" + footer)
        return {"prompt": prompt, "turns": 0, "truncated": False,
                "source_session": session, "prompt_chars": len(prompt)}
    truncated = False

    def _clip(text: str) -> str:
        nonlocal truncated
        if len(text) > TRANSFER_TURN_TEXT_MAX:
            truncated = True
            return text[:TRANSFER_TURN_TEXT_MAX] + "…[截斷]"
        return text

    blocks = []
    for turn in turns:
        lines = []
        if turn["user"]:
            lines.append("使用者:" + _clip(turn["user"]))
        if turn["reply"]:
            lines.append("回覆:" + _clip(turn["reply"]))
        blocks.append("\n".join(lines))
    # 從最新的回合往回填預算 —— 交接價值越新越高,擠不下就犧牲最舊的。
    overhead = len(header) + len(footer) + 120
    kept: list[str] = []
    used = overhead
    for block in reversed(blocks):
        cost = len(block) + 24          # 回合標頭 + 空行
        if kept and used + cost > max_chars:
            truncated = True            # 較舊回合整段被丟,留痕
            break
        if not kept and used + cost > max_chars:
            block = block[:max(200, max_chars - used - 24)] + "…[截斷]"
            truncated = True
        kept.append(block)
        used += len(block) + 24
    kept.reverse()
    numbered = [f"【回合 {i + 1}】\n{b}" for i, b in enumerate(kept)]
    note = ",較早內容已截斷" if truncated else ""
    prompt = (header + f"\n\n── 工作脈絡(最近 {len(kept)} 回合{note})──\n\n"
              + "\n\n".join(numbered) + "\n\n" + footer)
    return {"prompt": prompt, "turns": len(kept), "truncated": truncated,
            "source_session": session, "prompt_chars": len(prompt)}


def _git(cwd: str, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                                text=True, timeout=5, check=False)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def build_checkpoint_manifest(cwd: str, app_thread_id: str,
                              provider_thread_id: str | None = None,
                              artifact_paths: list[str] | None = None) -> dict:
    path = os.path.realpath(os.path.expanduser(cwd))
    if not os.path.isdir(path):
        raise T3CodeError("cwd must be an existing directory")
    status = _git(path, "status", "--short")
    head = _git(path, "rev-parse", "HEAD") or None
    artifacts = []
    for raw in (artifact_paths or [])[:100]:
        item = os.path.realpath(os.path.join(path, str(raw)))
        if not item.startswith(path + os.sep) and item != path:
            raise T3CodeError("artifact path must stay inside cwd")
        if os.path.isfile(item):
            data = Path(item).read_bytes()
            artifacts.append({"path": os.path.relpath(item, path),
                              "size": len(data),
                              "sha256": hashlib.sha256(data).hexdigest()})
    manifest = {"version": 1, "app_thread_id": app_thread_id,
                "provider_thread_id": provider_thread_id, "cwd": path,
                "head": head, "dirty": bool(status),
                "dirty_summary": status[:8000], "artifacts": artifacts,
                "read_only": True, "created_at": _now()}
    encoded = _json(manifest)
    manifest["artifact_manifest_hash"] = hashlib.sha256(encoded.encode()).hexdigest()
    return manifest


def create_checkpoint(db_path: str, manifest: dict,
                      checkpoint_id: str | None = None) -> dict:
    ensure_schema(db_path)
    if manifest.get("read_only") is not True:
        raise T3CodeError("checkpoint manifest must be read-only")
    cid = checkpoint_id or _id("checkpoint")
    now = _now()
    encoded = _json(manifest)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    con = _connect(db_path)
    try:
        existing = con.execute("SELECT * FROM checkpoints WHERE id=?", (cid,)).fetchone()
        if existing:
            existing_manifest = existing["manifest"] or ""
            if existing_manifest != encoded:
                raise T3CodeError("checkpoint_id already exists with different content")
            out = _row(existing) or {}
            out["manifest"] = json.loads(out["manifest"] or "{}")
            out["status"] = out.get("status") or "dry_run"
            out["manifest_hash"] = digest
            return out
        con.execute("""INSERT INTO checkpoints
            (id,app_thread_id,provider_thread_id,cwd,head,dirty_summary,
             artifact_manifest_hash,manifest,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (cid, manifest["app_thread_id"], manifest.get("provider_thread_id"),
                     manifest["cwd"], manifest.get("head"),
                     manifest.get("dirty_summary") or "",
                     manifest.get("artifact_manifest_hash") or digest,
                     encoded, "dry_run", now))
        con.commit()
        return {"id": cid, "status": "dry_run", "manifest": manifest,
                "manifest_hash": digest, "created_at": now}
    finally:
        con.close()


def _checkpoint_ref(checkpoint_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]", "-", str(checkpoint_id or ""))[:80]
    if not clean:
        raise T3CodeError("checkpoint id is required")
    return f"refs/pocket/checkpoints/{clean}"


def _git_run(cwd: str, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                              text=True, timeout=10, check=check)
    except (OSError, subprocess.SubprocessError) as exc:
        raise T3CodeError(f"git operation failed: {type(exc).__name__}") from exc


def _approval_is_ready(con: sqlite3.Connection, approval_id: str | None) -> bool:
    if not str(approval_id or "").strip():
        return False
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "approvals" not in tables:
        return True
    row = con.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()
    return bool(row and str(row[0]).lower() in {"approved", "approved_for_session"})


def materialize_checkpoint_ref(db_path: str, checkpoint_id: str,
                               approval_id: str) -> dict:
    """Create a hidden git ref only after an explicit approved action."""
    ensure_schema(db_path)
    con = _connect(db_path)
    try:
        row = con.execute("SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
        if row is None:
            raise T3CodeError("unknown checkpoint")
        if not _approval_is_ready(con, approval_id):
            raise T3CodeError("checkpoint ref requires an approved approval")
        cwd = str(row["cwd"])
        head = _git_run(cwd, "rev-parse", "HEAD").stdout.strip()
        if not head:
            raise T3CodeError("checkpoint cwd has no git HEAD")
        ref = _checkpoint_ref(checkpoint_id)
        _git_run(cwd, "update-ref", ref, head, check=True)
        now = _now()
        con.execute("""UPDATE checkpoints SET ref_name=?,approval_id=?,
            restore_status='available',status='materialized',last_error=NULL WHERE id=?""",
                    (ref, approval_id, checkpoint_id))
        con.commit()
        out = _row(con.execute("SELECT * FROM checkpoints WHERE id=?",
                               (checkpoint_id,)).fetchone()) or {}
        out["manifest"] = json.loads(out.get("manifest") or "{}")
        out["materialized_at"] = now
        return out
    finally:
        con.close()


def restore_checkpoint(db_path: str, checkpoint_id: str, approval_id: str,
                       confirm: bool) -> dict:
    """Restore a clean worktree from a materialized ref after confirmation."""
    if not confirm:
        raise T3CodeError("restore requires confirm=true")
    ensure_schema(db_path)
    con = _connect(db_path)
    try:
        row = con.execute("SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
        if row is None:
            raise T3CodeError("unknown checkpoint")
        if not row["ref_name"]:
            raise T3CodeError("checkpoint ref is not materialized")
        if not _approval_is_ready(con, approval_id):
            raise T3CodeError("restore requires an approved approval")
        cwd = str(row["cwd"])
        if _git_run(cwd, "status", "--porcelain").stdout.strip():
            raise T3CodeError("refusing restore on a dirty worktree")
        _git_run(cwd, "read-tree", "--reset", "-u", str(row["ref_name"]), check=True)
        now = _now()
        con.execute("""UPDATE checkpoints SET restore_status='restored',
            approval_id=?,restored_at=?,status='restored',last_error=NULL WHERE id=?""",
                    (approval_id, now, checkpoint_id))
        con.commit()
        out = _row(con.execute("SELECT * FROM checkpoints WHERE id=?",
                               (checkpoint_id,)).fetchone()) or {}
        out["manifest"] = json.loads(out.get("manifest") or "{}")
        return out
    finally:
        con.close()


def record_usage_sample(db_path: str, session: str, provider: str,
                        usage: dict, model: str | None = None,
                        source: str = "provider", dedupe_key: str | None = None) -> dict | None:
    ensure_schema(db_path)
    if not isinstance(usage, dict):
        return None
    inner = usage.get("tokenUsage") if isinstance(usage.get("tokenUsage"), dict) else usage
    if isinstance(inner.get("total"), dict):
        inner = inner["total"]

    def integer(*keys: str) -> int | None:
        for key in keys:
            if inner.get(key) is not None:
                try:
                    return max(0, int(inner[key]))
                except (TypeError, ValueError):
                    return None
        return None
    inp = integer("input_tokens", "inputTokens")
    out = integer("output_tokens", "outputTokens")
    cache_read = integer("cache_read_tokens", "cachedInputTokens") or 0
    cache_write = integer("cache_creation_input_tokens", "cacheWriteInputTokens") or 0
    total = integer("total_tokens", "totalTokens")
    if total is None:
        total = sum(v or 0 for v in (inp, out, cache_read, cache_write)) or None
    if total is None:
        return None
    key = dedupe_key or hashlib.sha256(
        _json([session, provider, model, inp, out, cache_read, cache_write, total]).encode()
    ).hexdigest()
    now = _now()
    con = _connect(db_path)
    try:
        con.execute("""INSERT OR IGNORE INTO usage_samples
            (id,session,provider,model,input_tokens,output_tokens,cache_read_tokens,
             cache_write_tokens,total_tokens,estimated_cost_usd,source,dedupe_key,recorded_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (_id("usage"), str(session), str(provider), model, inp, out,
                     cache_read, cache_write, total,
                     usage.get("cost_usd") or inner.get("cost_usd"),
                     source, key, now))
        con.commit()
        row = con.execute("SELECT * FROM usage_samples WHERE dedupe_key=?", (key,)).fetchone()
        return _row(row)
    finally:
        con.close()


def usage_summary(db_path: str, cutoff: float) -> dict:
    ensure_schema(db_path)
    con = _connect(db_path)
    try:
        rows = con.execute("""SELECT provider, COUNT(*),
            COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
            COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(cache_write_tokens),0),
            COALESCE(SUM(total_tokens),0), COALESCE(SUM(estimated_cost_usd),0)
            FROM usage_samples WHERE recorded_at>? GROUP BY provider""", (cutoff,)).fetchall()
        return {str(row[0]): {"provider": str(row[0]), "samples": int(row[1]),
                              "tokens_in": int(row[2]), "tokens_out": int(row[3]),
                              "cache_read_tokens": int(row[4]),
                              "cache_write_tokens": int(row[5]),
                              "total_tokens": int(row[6]),
                              "est_cost_usd": float(row[7])}
                for row in rows}
    finally:
        con.close()


def register_live_activity(db_path: str, session_id: str, token: str,
                           bundle_id: str) -> dict:
    ensure_schema(db_path)
    session_id, token, bundle_id = (str(session_id or "").strip(),
                                    str(token or "").strip(),
                                    str(bundle_id or "").strip())
    if not session_id or not token or not bundle_id:
        raise T3CodeError("session_id, token and bundle_id are required")
    now = _now()
    con = _connect(db_path)
    try:
        con.execute("INSERT OR REPLACE INTO live_activity_tokens(session_id,token,bundle_id,updated_at) "
                    "VALUES(?,?,?,?)", (session_id, token, bundle_id, now))
        con.commit()
        return {"session_id": session_id, "bundle_id": bundle_id,
                "token_set": True, "updated_at": now}
    finally:
        con.close()


def get_live_activity(db_path: str, session_id: str) -> dict | None:
    ensure_schema(db_path)
    con = _connect(db_path)
    try:
        return _row(con.execute("SELECT * FROM live_activity_tokens WHERE session_id=?",
                               (session_id,)).fetchone())
    finally:
        con.close()


def remove_live_activity(db_path: str, session_id: str) -> bool:
    ensure_schema(db_path)
    con = _connect(db_path)
    try:
        cur = con.execute("DELETE FROM live_activity_tokens WHERE session_id=?", (session_id,))
        con.commit()
        return bool(cur.rowcount)
    finally:
        con.close()


# ── 第二期 ActivityKit push:activity_tokens(一顆 activity 一列)──────────

ACTIVITY_DEVICE_ENVS = frozenset({"dev", "prod"})


def register_activity_token(db_path: str, activity_id: str, session_id: str,
                            push_token: str, device_env: str = "prod",
                            bundle_id: str = "") -> dict:
    """Upsert one Live Activity push token; same activity_id is idempotent."""
    ensure_schema(db_path)
    activity_id = str(activity_id or "").strip()
    session_id = str(session_id or "").strip()
    push_token = str(push_token or "").strip()
    device_env = str(device_env or "prod").strip().lower()
    bundle_id = str(bundle_id or "").strip()
    if not activity_id or not session_id or not push_token or not bundle_id:
        raise T3CodeError(
            "activity_id, session_id, push_token and bundle_id are required")
    if max(len(activity_id), len(session_id), len(bundle_id)) > 240 or \
            len(push_token) > 2048:
        raise T3CodeError("activity token field exceeds length limit")
    if device_env not in ACTIVITY_DEVICE_ENVS:
        raise T3CodeError("device_env must be 'dev' or 'prod'")
    now = _now()
    con = _connect(db_path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO activity_tokens"
            "(activity_id,session_id,push_token,device_env,bundle_id,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (activity_id, session_id, push_token, device_env, bundle_id, now))
        con.commit()
        return {"activity_id": activity_id, "session_id": session_id,
                "device_env": device_env, "bundle_id": bundle_id,
                "token_set": True, "updated_at": now}
    finally:
        con.close()


def session_activity_tokens(db_path: str, session_id: str) -> list[dict]:
    ensure_schema(db_path)
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT * FROM activity_tokens WHERE session_id=?",
            (session_id,)).fetchall()
        return [_row(r) for r in rows]
    finally:
        con.close()


def activity_token_sessions(db_path: str) -> set:
    """Session ids with at least one live activity registered (hot-path gate)."""
    ensure_schema(db_path)
    con = _connect(db_path)
    try:
        rows = con.execute("SELECT DISTINCT session_id FROM activity_tokens").fetchall()
        return {r[0] for r in rows}
    finally:
        con.close()


def remove_activity_token(db_path: str, activity_id: str) -> bool:
    ensure_schema(db_path)
    con = _connect(db_path)
    try:
        cur = con.execute("DELETE FROM activity_tokens WHERE activity_id=?",
                          (str(activity_id or "").strip(),))
        con.commit()
        return bool(cur.rowcount)
    finally:
        con.close()


def remove_session_activity_tokens(db_path: str, session_id: str) -> int:
    ensure_schema(db_path)
    con = _connect(db_path)
    try:
        cur = con.execute("DELETE FROM activity_tokens WHERE session_id=?",
                          (str(session_id or "").strip(),))
        con.commit()
        return int(cur.rowcount or 0)
    finally:
        con.close()


def event_snapshot(db_path: str, session: str, limit: int = 100) -> dict:
    """Return a durable message snapshot plus the event cursor it represents."""
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT id,role,content,attachments,created_at,status,client_id "
            "FROM messages WHERE session=? ORDER BY created_at DESC LIMIT ?",
            (session, max(1, min(int(limit), 500)))).fetchall()
        messages = []
        for row in reversed(rows):
            item = _row(row) or {}
            try:
                item["attachments"] = json.loads(item.get("attachments") or "[]")
            except (TypeError, ValueError):
                item["attachments"] = []
            messages.append(item)
        seq = con.execute("SELECT COALESCE(MAX(id),0) FROM event_log WHERE session=?",
                          (session,)).fetchone()[0]
        return {"session": session, "messages": messages,
                "snapshot_seq": int(seq or 0), "generated_at": _now()}
    finally:
        con.close()


def events_after(db_path: str, session: str, after_seq: int = 0,
                 limit: int = 500) -> list[dict]:
    """Read durable events strictly after a client cursor."""
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT id,type,payload,created_at FROM event_log "
            "WHERE session=? AND id>? ORDER BY id LIMIT ?",
            (session, max(0, int(after_seq)), max(1, min(int(limit), 500)))).fetchall()
        out = []
        for row in rows:
            try:
                payload = json.loads(row[2] or "{}")
            except (TypeError, ValueError):
                payload = {}
            out.append({"seq": int(row[0]), "type": row[1], "data": payload,
                        "ts": row[3]})
        return out
    finally:
        con.close()
