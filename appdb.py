from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 4

CAPABILITIES = {
    "panel_access",
    "dial",
    "receive_inbound",
    "contacts",
    "schedule",
    "bridge",
    "workspace_admin",
    "routing",
    "history",
    "audit",
    "settings",
}

DEFAULT_OPERATOR_CAPS = {"panel_access", "dial", "receive_inbound", "contacts", "history", "bridge"}
DEFAULT_ADMIN_CAPS = set(CAPABILITIES)


def now_ts() -> float:
    return time.time()


def json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


class AppDatabase:
    """Persistent v3 application database.

    This owns settings, workspaces, RBAC, web sessions, routing state, API tokens,
    webhooks and the tamper-resistant operator audit trail. Telephony history stays
    in call_history.sqlite3 so existing v2.x data can be migrated without loss.
    """

    def __init__(self, path: str = "/app/data/pbx_app.sqlite3"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    @contextmanager
    def transaction(self):
        with self._lock:
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                yield con
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                con.close()

    def _init_db(self) -> None:
        with self._lock, self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS config_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    actor_user_id TEXT NOT NULL DEFAULT '',
                    actor_name TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    snapshot_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_config_revisions_created ON config_revisions(created_at DESC);

                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL UNIQUE,
                    alias TEXT NOT NULL,
                    voice_channel_id TEXT NOT NULL DEFAULT '',
                    text_channel_id TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    accept_inbound INTEGER NOT NULL DEFAULT 1,
                    allow_outbound INTEGER NOT NULL DEFAULT 1,
                    auto_route INTEGER NOT NULL DEFAULT 1,
                    priority INTEGER NOT NULL DEFAULT 100,
                    max_calls INTEGER NOT NULL DEFAULT 15,
                    presence_grace_seconds REAL NOT NULL DEFAULT 4,
                    ring_mode TEXT NOT NULL DEFAULT 'auto',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_workspaces_priority ON workspaces(priority, alias);

                CREATE TABLE IF NOT EXISTS workspace_roles (
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    role_id TEXT NOT NULL,
                    role_name TEXT NOT NULL DEFAULT '',
                    capability TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(workspace_id, role_id, capability)
                );
                CREATE INDEX IF NOT EXISTS idx_workspace_roles_role ON workspace_roles(role_id);

                CREATE TABLE IF NOT EXISTS users (
                    discord_user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    avatar_url TEXT NOT NULL DEFAULT '',
                    is_system_admin INTEGER NOT NULL DEFAULT 0,
                    last_seen REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS web_sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    auth_type TEXT NOT NULL DEFAULT 'discord',
                    csrf_token TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    ip_hash TEXT NOT NULL DEFAULT '',
                    user_agent_hash TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_web_sessions_user ON web_sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_web_sessions_expiry ON web_sessions(expires_at);

                CREATE TABLE IF NOT EXISTS oauth_states (
                    state TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    return_to TEXT NOT NULL DEFAULT '/'
                );

                CREATE TABLE IF NOT EXISTS local_admin (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    username TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS local_users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    is_system_admin INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_login REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_local_users_username ON local_users(username COLLATE NOCASE);

                CREATE TABLE IF NOT EXISTS local_user_workspaces (
                    user_id TEXT NOT NULL REFERENCES local_users(id) ON DELETE CASCADE,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    capabilities_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(user_id, workspace_id)
                );
                CREATE INDEX IF NOT EXISTS idx_local_user_workspaces_workspace ON local_user_workspaces(workspace_id);

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    actor_user_id TEXT NOT NULL DEFAULT '',
                    actor_name TEXT NOT NULL DEFAULT '',
                    auth_type TEXT NOT NULL DEFAULT '',
                    workspace_id TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL DEFAULT '',
                    entity_id TEXT NOT NULL DEFAULT '',
                    call_uuid TEXT NOT NULL DEFAULT '',
                    number TEXT NOT NULL DEFAULT '',
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    ip_hash TEXT NOT NULL DEFAULT '',
                    prev_hash TEXT NOT NULL DEFAULT '',
                    event_hash TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_workspace ON audit_log(workspace_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_call ON audit_log(call_uuid, created_at DESC);

                CREATE TABLE IF NOT EXISTS api_tokens (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    workspace_id TEXT NOT NULL DEFAULT '',
                    capabilities_json TEXT NOT NULL DEFAULT '[]',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    last_used REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS webhooks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    secret TEXT NOT NULL DEFAULT '',
                    workspace_id TEXT NOT NULL DEFAULT '',
                    events_json TEXT NOT NULL DEFAULT '[]',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS dnc_numbers (
                    number TEXT PRIMARY KEY,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    actor_user_id TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS parked_calls (
                    slot INTEGER PRIMARY KEY,
                    call_uuid TEXT NOT NULL UNIQUE,
                    workspace_id TEXT NOT NULL DEFAULT '',
                    parked_at REAL NOT NULL,
                    parked_by TEXT NOT NULL DEFAULT ''
                );
                """
            )
            version = int((con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() or {"value": 0})["value"])
            if version < 1:
                con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version','1')")
                version = 1
            # Future-safe explicit migrations. Current CREATE TABLE statements are idempotent,
            # but recording versions lets upgrades add data transforms safely.
            if version < 2:
                con.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
                version = 2
            if version < SCHEMA_VERSION:
                con.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    # ---------- settings / revisions ----------
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value_json"])
        except Exception:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        with self.transaction() as con:
            con.execute(
                "INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                (key, json_dumps(value), now_ts()),
            )

    def all_settings(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        with self._lock, self._connect() as con:
            rows = con.execute("SELECT key,value_json FROM settings ORDER BY key").fetchall()
        for row in rows:
            try:
                out[row["key"]] = json.loads(row["value_json"])
            except Exception:
                pass
        return out

    def config_snapshot(self) -> dict[str, Any]:
        return {
            "settings": self.all_settings(),
            "workspaces": self.list_workspaces(include_roles=True),
        }

    def save_revision(self, reason: str, actor_user_id: str = "", actor_name: str = "") -> int:
        snapshot = self.config_snapshot()
        with self.transaction() as con:
            cur = con.execute(
                "INSERT INTO config_revisions(created_at,actor_user_id,actor_name,reason,snapshot_json) VALUES(?,?,?,?,?)",
                (now_ts(), actor_user_id, actor_name, str(reason)[:200], json_dumps(snapshot)),
            )
            return int(cur.lastrowid)

    def list_revisions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT id,created_at,actor_user_id,actor_name,reason FROM config_revisions ORDER BY id DESC LIMIT ?",
                (min(200, max(1, int(limit))),),
            ).fetchall()
        return [dict(r) for r in rows]

    def restore_revision(self, revision_id: int) -> bool:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT snapshot_json FROM config_revisions WHERE id=?", (int(revision_id),)).fetchone()
        if not row:
            return False
        snapshot = json.loads(row["snapshot_json"])
        settings = snapshot.get("settings", {}) if isinstance(snapshot, dict) else {}
        workspaces = snapshot.get("workspaces", []) if isinstance(snapshot, dict) else []
        with self.transaction() as con:
            con.execute("DELETE FROM settings")
            for key, value in settings.items():
                con.execute("INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)", (str(key), json_dumps(value), now_ts()))
            con.execute("DELETE FROM workspace_roles")
            con.execute("DELETE FROM workspaces")
            for ws in workspaces:
                now = now_ts()
                con.execute("""INSERT INTO workspaces(id,guild_id,alias,voice_channel_id,text_channel_id,enabled,accept_inbound,allow_outbound,auto_route,priority,max_calls,presence_grace_seconds,ring_mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (ws["id"],str(ws["guild_id"]),ws.get("alias","Workspace"),str(ws.get("voice_channel_id", "")),str(ws.get("text_channel_id", "")),int(bool(ws.get("enabled",True))),int(bool(ws.get("accept_inbound",True))),int(bool(ws.get("allow_outbound",True))),int(bool(ws.get("auto_route",True))),int(ws.get("priority",100)),int(ws.get("max_calls",15)),float(ws.get("presence_grace_seconds",4)),str(ws.get("ring_mode","auto")),float(ws.get("created_at",now) or now),now))
                for role in ws.get("roles", []):
                    for cap in role.get("capabilities", []):
                        if cap in CAPABILITIES:
                            con.execute("INSERT INTO workspace_roles(workspace_id,role_id,role_name,capability,created_at) VALUES(?,?,?,?,?)", (ws["id"],str(role.get("role_id","")),str(role.get("role_name","")),cap,now))
        return True

    # ---------- workspaces / RBAC ----------
    def upsert_workspace(self, data: dict[str, Any]) -> dict[str, Any]:
        guild_id = str(data.get("guild_id", "")).strip()
        if not guild_id.isdigit():
            raise ValueError("guild_id must be a Discord guild ID")
        workspace_id = str(data.get("id") or f"ws_{guild_id}")[:80]
        alias = str(data.get("alias") or f"Guild {guild_id}")[:80]
        voice_channel_id = str(data.get("voice_channel_id", "")).strip()
        text_channel_id = str(data.get("text_channel_id", "")).strip()
        if voice_channel_id and not voice_channel_id.isdigit():
            raise ValueError("voice_channel_id must be a Discord channel ID")
        if text_channel_id and not text_channel_id.isdigit():
            raise ValueError("text_channel_id must be a Discord channel ID")
        now = now_ts()
        with self.transaction() as con:
            con.execute(
                """
                INSERT INTO workspaces(id,guild_id,alias,voice_channel_id,text_channel_id,enabled,accept_inbound,allow_outbound,auto_route,priority,max_calls,presence_grace_seconds,ring_mode,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  guild_id=excluded.guild_id,alias=excluded.alias,voice_channel_id=excluded.voice_channel_id,
                  text_channel_id=excluded.text_channel_id,enabled=excluded.enabled,accept_inbound=excluded.accept_inbound,
                  allow_outbound=excluded.allow_outbound,auto_route=excluded.auto_route,priority=excluded.priority,
                  max_calls=excluded.max_calls,presence_grace_seconds=excluded.presence_grace_seconds,
                  ring_mode=excluded.ring_mode,updated_at=excluded.updated_at
                """,
                (
                    workspace_id, guild_id, alias, voice_channel_id, text_channel_id,
                    int(bool(data.get("enabled", True))), int(bool(data.get("accept_inbound", True))),
                    int(bool(data.get("allow_outbound", True))), int(bool(data.get("auto_route", True))),
                    max(1, min(999, int(data.get("priority", 100) or 100))),
                    max(1, min(100, int(data.get("max_calls", 15) or 15))),
                    max(0.0, min(60.0, float(data.get("presence_grace_seconds", 4) or 4))),
                    str(data.get("ring_mode", "auto"))[:30], now, now,
                ),
            )
        return self.get_workspace(workspace_id) or {}

    def delete_workspace(self, workspace_id: str) -> bool:
        with self.transaction() as con:
            cur = con.execute("DELETE FROM workspaces WHERE id=?", (workspace_id,))
            return cur.rowcount > 0

    def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
        return self._workspace_row(row) if row else None

    def get_workspace_by_guild(self, guild_id: int | str) -> dict[str, Any] | None:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT * FROM workspaces WHERE guild_id=?", (str(guild_id),)).fetchone()
        return self._workspace_row(row) if row else None

    @staticmethod
    def _workspace_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for key in ("enabled", "accept_inbound", "allow_outbound", "auto_route"):
            d[key] = bool(d.get(key))
        return d

    def list_workspaces(self, include_roles: bool = False) -> list[dict[str, Any]]:
        with self._lock, self._connect() as con:
            rows = con.execute("SELECT * FROM workspaces ORDER BY priority,alias COLLATE NOCASE").fetchall()
        out = [self._workspace_row(r) for r in rows]
        if include_roles:
            for item in out:
                item["roles"] = self.list_workspace_roles(item["id"])
        return out

    def replace_role_capabilities(self, workspace_id: str, role_id: str, role_name: str, capabilities: Iterable[str]) -> None:
        caps = sorted({str(x) for x in capabilities if str(x) in CAPABILITIES})
        with self.transaction() as con:
            con.execute("DELETE FROM workspace_roles WHERE workspace_id=? AND role_id=?", (workspace_id, str(role_id)))
            for cap in caps:
                con.execute(
                    "INSERT INTO workspace_roles(workspace_id,role_id,role_name,capability,created_at) VALUES(?,?,?,?,?)",
                    (workspace_id, str(role_id), str(role_name)[:120], cap, now_ts()),
                )

    def remove_workspace_role(self, workspace_id: str, role_id: str) -> bool:
        with self.transaction() as con:
            cur = con.execute("DELETE FROM workspace_roles WHERE workspace_id=? AND role_id=?", (workspace_id, str(role_id)))
            return cur.rowcount > 0

    def list_workspace_roles(self, workspace_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT role_id,MAX(role_name) role_name,GROUP_CONCAT(capability) caps FROM workspace_roles WHERE workspace_id=? GROUP BY role_id ORDER BY role_name",
                (workspace_id,),
            ).fetchall()
        return [
            {"role_id": r["role_id"], "role_name": r["role_name"], "capabilities": sorted((r["caps"] or "").split(","))}
            for r in rows
        ]

    def capabilities_for_roles(self, workspace_id: str, role_ids: Iterable[int | str]) -> set[str]:
        ids = [str(x) for x in role_ids]
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._connect() as con:
            rows = con.execute(
                f"SELECT DISTINCT capability FROM workspace_roles WHERE workspace_id=? AND role_id IN ({placeholders})",
                [workspace_id, *ids],
            ).fetchall()
        return {str(r["capability"]) for r in rows}

    # ---------- users / sessions ----------
    def upsert_user(self, user_id: str, username: str, display_name: str = "", avatar_url: str = "") -> dict[str, Any]:
        with self.transaction() as con:
            con.execute(
                """
                INSERT INTO users(discord_user_id,username,display_name,avatar_url,last_seen) VALUES(?,?,?,?,?)
                ON CONFLICT(discord_user_id) DO UPDATE SET username=excluded.username,display_name=excluded.display_name,
                avatar_url=excluded.avatar_url,last_seen=excluded.last_seen
                """,
                (str(user_id), username[:120], display_name[:120], avatar_url[:500], now_ts()),
            )
        return self.get_user(str(user_id)) or {}

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT * FROM users WHERE discord_user_id=?", (str(user_id),)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["is_system_admin"] = bool(d.get("is_system_admin"))
        return d

    def set_system_admin(self, user_id: str, enabled: bool = True) -> None:
        with self.transaction() as con:
            con.execute("UPDATE users SET is_system_admin=? WHERE discord_user_id=?", (int(bool(enabled)), str(user_id)))

    @staticmethod
    def _fingerprint(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:32] if value else ""

    def create_session(self, user_id: str, auth_type: str, *, ip: str = "", user_agent: str = "", ttl_seconds: int = 86400) -> dict[str, str]:
        sid = secrets.token_urlsafe(36)
        csrf = secrets.token_urlsafe(24)
        now = now_ts()
        with self.transaction() as con:
            con.execute(
                "INSERT INTO web_sessions(session_id,user_id,auth_type,csrf_token,created_at,last_seen,expires_at,ip_hash,user_agent_hash) VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, str(user_id), auth_type, csrf, now, now, now + max(900, ttl_seconds), self._fingerprint(ip), self._fingerprint(user_agent)),
            )
        return {"session_id": sid, "csrf_token": csrf}

    def get_session(self, sid: str, *, touch: bool = True) -> dict[str, Any] | None:
        now = now_ts()
        with self.transaction() as con:
            con.execute("DELETE FROM web_sessions WHERE expires_at<?", (now,))
            row = con.execute("SELECT * FROM web_sessions WHERE session_id=? AND expires_at>?", (sid, now)).fetchone()
            if row and touch:
                con.execute("UPDATE web_sessions SET last_seen=? WHERE session_id=?", (now, sid))
        return dict(row) if row else None

    def delete_session(self, sid: str) -> None:
        with self.transaction() as con:
            con.execute("DELETE FROM web_sessions WHERE session_id=?", (sid,))

    def delete_user_sessions(self, user_id: str) -> int:
        with self.transaction() as con:
            cur = con.execute("DELETE FROM web_sessions WHERE user_id=?", (str(user_id),))
            return cur.rowcount

    # ---------- local break-glass admin ----------
    @staticmethod
    def _hash_password(password: str, salt_hex: str) -> str:
        salt = bytes.fromhex(salt_hex)
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000).hex()

    def set_local_admin(self, username: str, password: str) -> None:
        if len(password) < 12:
            raise ValueError("local admin password must be at least 12 characters")
        salt = os.urandom(16).hex()
        digest = self._hash_password(password, salt)
        with self.transaction() as con:
            con.execute(
                "INSERT INTO local_admin(id,username,password_hash,salt,updated_at) VALUES(1,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET username=excluded.username,password_hash=excluded.password_hash,salt=excluded.salt,updated_at=excluded.updated_at",
                (username[:120], digest, salt, now_ts()),
            )

    def local_admin_configured(self) -> bool:
        with self._lock, self._connect() as con:
            return con.execute("SELECT 1 FROM local_admin WHERE id=1").fetchone() is not None

    def local_admin_info(self) -> dict[str, Any]:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT username,updated_at FROM local_admin WHERE id=1").fetchone()
        return dict(row) if row else {"username": "", "updated_at": 0.0}

    def verify_local_admin(self, username: str, password: str) -> bool:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT username,password_hash,salt FROM local_admin WHERE id=1").fetchone()
        if not row or not secrets.compare_digest(str(row["username"]), str(username)):
            return False
        digest = self._hash_password(password, row["salt"])
        return secrets.compare_digest(digest, row["password_hash"])

    # ---------- managed local accounts ----------
    @staticmethod
    def _normalize_local_username(username: str) -> str:
        username = str(username or "").strip()
        if not username or len(username) > 80:
            raise ValueError("username must be 1-80 characters")
        if not all(ch.isalnum() or ch in "._-@" for ch in username):
            raise ValueError("username may contain letters, numbers, dot, dash, underscore and @")
        return username

    def _local_user_payload(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        d = dict(row)
        d["enabled"] = bool(d.get("enabled"))
        d["is_system_admin"] = bool(d.get("is_system_admin"))
        d.pop("password_hash", None)
        d.pop("salt", None)
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT workspace_id,capabilities_json FROM local_user_workspaces WHERE user_id=? ORDER BY workspace_id",
                (d["id"],),
            ).fetchall()
        access=[]
        for x in rows:
            try:
                caps=[c for c in json.loads(x["capabilities_json"] or "[]") if c in CAPABILITIES]
            except Exception:
                caps=[]
            access.append({"workspace_id": x["workspace_id"], "capabilities": sorted(set(caps))})
        d["workspace_access"] = access
        return d

    def get_local_user(self, user_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT * FROM local_users WHERE id=?", (str(user_id),)).fetchone()
        return self._local_user_payload(row) if row else None

    def get_local_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT * FROM local_users WHERE username=? COLLATE NOCASE", (str(username).strip(),)).fetchone()
        return self._local_user_payload(row) if row else None

    def list_local_users(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as con:
            rows = con.execute("SELECT * FROM local_users ORDER BY username COLLATE NOCASE").fetchall()
        return [self._local_user_payload(r) for r in rows]

    def save_local_user(self, *, user_id: str = "", username: str, display_name: str = "", password: str = "",
                        enabled: bool = True, is_system_admin: bool = False, workspace_access=None) -> dict[str, Any]:
        username = self._normalize_local_username(username)
        display_name = str(display_name or username).strip()[:120] or username
        now = now_ts()
        existing = self.get_local_user(str(user_id)) if user_id else None
        if not existing and len(password) < 12:
            raise ValueError("password must be at least 12 characters")
        if password and len(password) < 12:
            raise ValueError("password must be at least 12 characters")
        if not user_id:
            user_id = "lu_" + secrets.token_hex(10)
        access=[]
        seen=set()
        for item in workspace_access or []:
            wid=str((item or {}).get("workspace_id", "")).strip()
            if not wid or wid in seen or not self.get_workspace(wid):
                continue
            caps=sorted({str(c) for c in ((item or {}).get("capabilities") or []) if str(c) in CAPABILITIES})
            if caps:
                access.append((wid, json_dumps(caps)))
                seen.add(wid)
        if not bool(is_system_admin) and not access:
            raise ValueError("assign at least one workspace to a non-admin local account")
        try:
            with self.transaction() as con:
                if existing:
                    if password:
                        salt=os.urandom(16).hex(); digest=self._hash_password(password, salt)
                        con.execute("UPDATE local_users SET username=?,display_name=?,password_hash=?,salt=?,enabled=?,is_system_admin=?,updated_at=? WHERE id=?",
                                    (username,display_name,digest,salt,int(bool(enabled)),int(bool(is_system_admin)),now,user_id))
                    else:
                        con.execute("UPDATE local_users SET username=?,display_name=?,enabled=?,is_system_admin=?,updated_at=? WHERE id=?",
                                    (username,display_name,int(bool(enabled)),int(bool(is_system_admin)),now,user_id))
                else:
                    salt=os.urandom(16).hex(); digest=self._hash_password(password, salt)
                    con.execute("INSERT INTO local_users(id,username,display_name,password_hash,salt,enabled,is_system_admin,created_at,updated_at,last_login) VALUES(?,?,?,?,?,?,?,?,?,0)",
                                (user_id,username,display_name,digest,salt,int(bool(enabled)),int(bool(is_system_admin)),now,now))
                con.execute("DELETE FROM local_user_workspaces WHERE user_id=?", (user_id,))
                con.executemany("INSERT INTO local_user_workspaces(user_id,workspace_id,capabilities_json) VALUES(?,?,?)",
                                [(user_id,wid,caps) for wid,caps in access])
                if existing and (password or not enabled):
                    con.execute("DELETE FROM web_sessions WHERE user_id=?", (f"localuser:{user_id}",))
        except sqlite3.IntegrityError as exc:
            if "username" in str(exc).lower() or "unique" in str(exc).lower():
                raise ValueError("that username already exists") from exc
            raise
        return self.get_local_user(user_id) or {}

    def verify_local_user(self, username: str, password: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT * FROM local_users WHERE username=? COLLATE NOCASE AND enabled=1", (str(username).strip(),)).fetchone()
        if not row:
            return None
        digest=self._hash_password(str(password), row["salt"])
        if not secrets.compare_digest(digest, row["password_hash"]):
            return None
        with self.transaction() as con:
            con.execute("UPDATE local_users SET last_login=? WHERE id=?", (now_ts(), row["id"]))
        return self.get_local_user(row["id"])

    def local_user_capabilities(self, user_id: str, workspace_id: str) -> set[str]:
        with self._lock, self._connect() as con:
            row=con.execute("SELECT capabilities_json FROM local_user_workspaces WHERE user_id=? AND workspace_id=?", (str(user_id),str(workspace_id))).fetchone()
        if not row:
            return set()
        try:
            return {str(c) for c in json.loads(row["capabilities_json"] or "[]") if str(c) in CAPABILITIES}
        except Exception:
            return set()

    def local_user_workspaces(self, user_id: str) -> list[dict[str, Any]]:
        user=self.get_local_user(user_id)
        if not user or not user.get("enabled"):
            return []
        out=[]
        for item in user.get("workspace_access", []):
            ws=self.get_workspace(item["workspace_id"])
            if ws:
                ws["capabilities"]=list(item.get("capabilities", []))
                out.append(ws)
        return out

    def delete_local_user(self, user_id: str) -> bool:
        with self.transaction() as con:
            con.execute("DELETE FROM web_sessions WHERE user_id=?", (f"localuser:{user_id}",))
            cur=con.execute("DELETE FROM local_users WHERE id=?", (str(user_id),))
            return cur.rowcount > 0

    # ---------- OAuth state ----------
    def create_oauth_state(self, return_to: str = "/") -> str:
        state = secrets.token_urlsafe(32)
        now = now_ts()
        with self.transaction() as con:
            con.execute("DELETE FROM oauth_states WHERE expires_at<?", (now,))
            con.execute("INSERT INTO oauth_states(state,created_at,expires_at,return_to) VALUES(?,?,?,?)", (state, now, now + 600, return_to[:300]))
        return state

    def consume_oauth_state(self, state: str) -> str | None:
        now = now_ts()
        with self.transaction() as con:
            row = con.execute("SELECT return_to FROM oauth_states WHERE state=? AND expires_at>?", (state, now)).fetchone()
            con.execute("DELETE FROM oauth_states WHERE state=?", (state,))
        return str(row["return_to"]) if row else None

    # ---------- audit ----------
    def audit(self, action: str, *, actor_user_id: str = "", actor_name: str = "", auth_type: str = "", workspace_id: str = "", entity_type: str = "", entity_id: str = "", call_uuid: str = "", number: str = "", detail: Any = None, ip: str = "") -> int:
        detail_json = json_dumps(detail if detail is not None else {})[:12000]
        created = now_ts()
        ip_hash = self._fingerprint(ip)
        with self.transaction() as con:
            prev = con.execute("SELECT event_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
            prev_hash = str(prev["event_hash"]) if prev else ""
            canonical = "|".join([
                f"{created:.6f}", actor_user_id, actor_name, auth_type, workspace_id, action,
                entity_type, entity_id, call_uuid, number, detail_json, ip_hash, prev_hash,
            ])
            event_hash = hashlib.sha256(canonical.encode()).hexdigest()
            cur = con.execute(
                """
                INSERT INTO audit_log(created_at,actor_user_id,actor_name,auth_type,workspace_id,action,entity_type,entity_id,call_uuid,number,detail_json,ip_hash,prev_hash,event_hash)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (created, actor_user_id, actor_name, auth_type, workspace_id, action[:120], entity_type[:80], entity_id[:180], call_uuid[:80], number[:80], detail_json, ip_hash, prev_hash, event_hash),
            )
            return int(cur.lastrowid)

    def audit_list(self, *, limit: int = 200, offset: int = 0, q: str = "", actor: str = "", workspace_id: str = "", action: str = "", call_uuid: str = "") -> dict[str, Any]:
        limit = min(500, max(1, int(limit)))
        offset = max(0, int(offset))
        clauses: list[str] = []
        vals: list[Any] = []
        if q:
            needle = f"%{q}%"
            clauses.append("(actor_name LIKE ? OR action LIKE ? OR number LIKE ? OR detail_json LIKE ?)")
            vals.extend([needle] * 4)
        if actor:
            clauses.append("(actor_user_id=? OR actor_name LIKE ?)")
            vals.extend([actor, f"%{actor}%"])
        if workspace_id:
            clauses.append("workspace_id=?")
            vals.append(workspace_id)
        if action:
            clauses.append("action LIKE ?")
            vals.append(f"%{action}%")
        if call_uuid:
            clauses.append("call_uuid=?")
            vals.append(call_uuid)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock, self._connect() as con:
            total = int(con.execute("SELECT COUNT(*) FROM audit_log" + where, vals).fetchone()[0])
            rows = con.execute("SELECT * FROM audit_log" + where + " ORDER BY id DESC LIMIT ? OFFSET ?", [*vals, limit, offset]).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            try:
                d["detail"] = json.loads(d.pop("detail_json"))
            except Exception:
                d["detail"] = {}
            out.append(d)
        return {"events": out, "total": total, "limit": limit, "offset": offset}

    def verify_audit_chain(self, limit: int = 100000) -> dict[str, Any]:
        with self._lock, self._connect() as con:
            rows = con.execute("SELECT * FROM audit_log ORDER BY id ASC LIMIT ?", (limit,)).fetchall()
        prev_hash = ""
        checked = 0
        for row in rows:
            canonical = "|".join([
                f"{float(row['created_at']):.6f}", row["actor_user_id"], row["actor_name"], row["auth_type"], row["workspace_id"], row["action"],
                row["entity_type"], row["entity_id"], row["call_uuid"], row["number"], row["detail_json"], row["ip_hash"], prev_hash,
            ])
            expected = hashlib.sha256(canonical.encode()).hexdigest()
            if row["prev_hash"] != prev_hash or row["event_hash"] != expected:
                return {"ok": False, "checked": checked, "failed_id": row["id"]}
            prev_hash = row["event_hash"]
            checked += 1
        return {"ok": True, "checked": checked, "head": prev_hash}


    # ---------- API tokens ----------
    def create_api_token(self, name: str, workspace_id: str = "", capabilities=None) -> tuple[dict[str, Any], str]:
        token_id = "tok_" + secrets.token_hex(8)
        raw = "pbx_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        caps = sorted({str(x) for x in (capabilities or []) if str(x) in CAPABILITIES})
        with self.transaction() as con:
            con.execute("INSERT INTO api_tokens(id,name,token_hash,workspace_id,capabilities_json,enabled,created_at,last_used) VALUES(?,?,?,?,?,1,?,0)",
                        (token_id, str(name)[:120], digest, str(workspace_id or ""), json_dumps(caps), now_ts()))
        return {"id": token_id, "name": str(name)[:120], "workspace_id": str(workspace_id or ""), "capabilities": caps, "enabled": True}, raw

    def verify_api_token(self, raw: str) -> dict[str, Any] | None:
        if not raw or not raw.startswith("pbx_"):
            return None
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.transaction() as con:
            row = con.execute("SELECT * FROM api_tokens WHERE token_hash=? AND enabled=1", (digest,)).fetchone()
            if row:
                con.execute("UPDATE api_tokens SET last_used=? WHERE id=?", (now_ts(), row["id"]))
        if not row:
            return None
        d = dict(row)
        try:
            d["capabilities"] = json.loads(d.pop("capabilities_json"))
        except Exception:
            d["capabilities"] = []
        d["enabled"] = bool(d.get("enabled"))
        d.pop("token_hash", None)
        return d

    def list_api_tokens(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as con:
            rows = con.execute("SELECT id,name,workspace_id,capabilities_json,enabled,created_at,last_used FROM api_tokens ORDER BY created_at DESC").fetchall()
        out=[]
        for r in rows:
            d=dict(r); d["enabled"]=bool(d["enabled"])
            try: d["capabilities"]=json.loads(d.pop("capabilities_json"))
            except Exception: d["capabilities"]=[]
            out.append(d)
        return out

    def revoke_api_token(self, token_id: str) -> bool:
        with self.transaction() as con:
            cur=con.execute("UPDATE api_tokens SET enabled=0 WHERE id=?", (token_id,))
            return cur.rowcount>0

    # ---------- webhooks ----------
    def list_webhooks(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as con:
            rows=con.execute("SELECT * FROM webhooks ORDER BY name").fetchall()
        out=[]
        for r in rows:
            d=dict(r); d["enabled"]=bool(d["enabled"])
            try: d["events"]=json.loads(d.pop("events_json"))
            except Exception: d["events"]=[]
            out.append(d)
        return out

    def upsert_webhook(self, data: dict[str, Any]) -> dict[str, Any]:
        webhook_id=str(data.get("id") or ("wh_"+secrets.token_hex(8)))
        now=now_ts(); events=[str(x)[:80] for x in data.get("events", []) if str(x)][:50]
        with self.transaction() as con:
            con.execute("""INSERT INTO webhooks(id,name,url,secret,workspace_id,events_json,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url,secret=excluded.secret,workspace_id=excluded.workspace_id,events_json=excluded.events_json,enabled=excluded.enabled,updated_at=excluded.updated_at""",
            (webhook_id,str(data.get("name") or "Webhook")[:120],str(data.get("url") or "")[:1000],str(data.get("secret") or "")[:300],str(data.get("workspace_id") or ""),json_dumps(events),int(bool(data.get("enabled",True))),now,now))
        return next((x for x in self.list_webhooks() if x["id"]==webhook_id), {})

    def delete_webhook(self, webhook_id: str) -> bool:
        with self.transaction() as con:
            cur=con.execute("DELETE FROM webhooks WHERE id=?", (webhook_id,)); return cur.rowcount>0

    # ---------- DNC / policy ----------
    def dnc_add(self, number: str, reason: str = "", actor_user_id: str = "") -> None:
        with self.transaction() as con:
            con.execute("INSERT OR REPLACE INTO dnc_numbers(number,reason,created_at,actor_user_id) VALUES(?,?,?,?)", (number, reason[:300], now_ts(), actor_user_id))

    def dnc_remove(self, number: str) -> bool:
        with self.transaction() as con:
            cur = con.execute("DELETE FROM dnc_numbers WHERE number=?", (number,))
            return cur.rowcount > 0

    def dnc_contains(self, number: str) -> bool:
        with self._lock, self._connect() as con:
            return con.execute("SELECT 1 FROM dnc_numbers WHERE number=?", (number,)).fetchone() is not None

    def dnc_list(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as con:
            rows = con.execute("SELECT * FROM dnc_numbers ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    # ---------- parked calls ----------
    def park_call(self, call_uuid: str, workspace_id: str, actor: str = "") -> int:
        with self.transaction() as con:
            existing = con.execute("SELECT slot FROM parked_calls WHERE call_uuid=?", (call_uuid,)).fetchone()
            if existing:
                return int(existing["slot"])
            used = {int(r["slot"]) for r in con.execute("SELECT slot FROM parked_calls").fetchall()}
            slot = next((n for n in range(1, 100) if n not in used), 0)
            if not slot:
                raise RuntimeError("no park slots available")
            con.execute("INSERT INTO parked_calls(slot,call_uuid,workspace_id,parked_at,parked_by) VALUES(?,?,?,?,?)", (slot, call_uuid, workspace_id, now_ts(), actor[:120]))
            return slot

    def unpark_call(self, slot: int) -> dict[str, Any] | None:
        with self.transaction() as con:
            row = con.execute("SELECT * FROM parked_calls WHERE slot=?", (int(slot),)).fetchone()
            if row:
                con.execute("DELETE FROM parked_calls WHERE slot=?", (int(slot),))
        return dict(row) if row else None

    def remove_parked_call(self, call_uuid: str) -> None:
        with self.transaction() as con:
            con.execute("DELETE FROM parked_calls WHERE call_uuid=?", (call_uuid,))

    def list_parked(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as con:
            rows = con.execute("SELECT * FROM parked_calls ORDER BY slot").fetchall()
        return [dict(r) for r in rows]
