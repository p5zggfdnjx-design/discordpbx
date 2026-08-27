from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import time
from pathlib import Path

from aiohttp import web

from call_history import CallHistoryStore

log = logging.getLogger("discord-pbx.database-consolidation")

DB_COMPONENT_VERSION = 1
ONLINE_WINDOW_SECONDS = 120


def _sha256(path: Path) -> str:
    if not path.is_file():
        return ""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_schema(db) -> None:
    with db.transaction() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS component_schema (
                component TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS storage_migrations (
                source TEXT PRIMARY KEY,
                completed_at REAL NOT NULL,
                row_count INTEGER NOT NULL DEFAULT 0,
                source_sha256 TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS legacy_data_catalog (
                source TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT NOT NULL DEFAULT '',
                captured_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS identity_directory (
                identity_key TEXT PRIMARY KEY,
                auth_type TEXT NOT NULL,
                source_user_id TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                avatar_url TEXT NOT NULL DEFAULT '',
                is_system_admin INTEGER NOT NULL DEFAULT 0,
                first_seen REAL NOT NULL DEFAULT 0,
                last_seen REAL NOT NULL DEFAULT 0,
                last_login REAL NOT NULL DEFAULT 0,
                login_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_identity_directory_seen
                ON identity_directory(last_seen DESC);
            """
        )
        con.execute(
            """INSERT INTO component_schema(component,version,updated_at)
               VALUES('database_consolidation',?,?)
               ON CONFLICT(component) DO UPDATE SET
                 version=excluded.version,updated_at=excluded.updated_at""",
            (DB_COMPONENT_VERSION, time.time()),
        )


def _migration_done(db, source: str) -> bool:
    with db._lock, db._connect() as con:
        return bool(con.execute(
            "SELECT 1 FROM storage_migrations WHERE source=?", (source,)
        ).fetchone())


def _mark_migration(db, source: str, row_count: int, source_sha256: str) -> None:
    with db.transaction() as con:
        con.execute(
            """INSERT INTO storage_migrations(source,completed_at,row_count,source_sha256)
               VALUES(?,?,?,?)
               ON CONFLICT(source) DO UPDATE SET
                 completed_at=excluded.completed_at,
                 row_count=excluded.row_count,
                 source_sha256=excluded.source_sha256""",
            (source, time.time(), max(0, int(row_count)), str(source_sha256 or "")),
        )


def backup_and_catalog_legacy_data(db, data_dir: Path) -> None:
    """Create non-destructive rollback copies and fingerprint legacy flat stores."""
    backup_dir = data_dir / "migration-backups" / "database-consolidation-v1"
    backup_dir.mkdir(parents=True, exist_ok=True)
    names = (
        "contacts.json",
        "scheduled_calls.json",
        "operator_settings.json",
        "caller_id_pool.yaml",
        "random_call_pool.yaml",
        "caller_id_blocks.yaml",
        "random_call_blocks.yaml",
        "soundboard.json",
        "call_history.sqlite3",
    )
    now = time.time()
    with db.transaction() as con:
        for name in names:
            src = data_dir / name
            if not src.is_file():
                continue
            dst = backup_dir / name
            if not dst.exists():
                if name == "call_history.sqlite3":
                    src_con = sqlite3.connect(src)
                    dst_con = sqlite3.connect(dst)
                    try:
                        src_con.backup(dst_con)
                    finally:
                        dst_con.close()
                        src_con.close()
                else:
                    shutil.copy2(src, dst)
            con.execute(
                """INSERT INTO legacy_data_catalog(source,path,size_bytes,sha256,captured_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(source) DO UPDATE SET
                     path=excluded.path,size_bytes=excluded.size_bytes,
                     sha256=excluded.sha256,captured_at=excluded.captured_at""",
                (name, str(src), int(src.stat().st_size), _sha256(src), now),
            )


def _table_columns(con: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [str(r[1]) for r in con.execute(
        f'PRAGMA {schema}.table_info("{table}")'
    ).fetchall()]


def migrate_call_history(db, source_path: Path) -> int:
    """Move the old call-history SQLite tables into pbx_app.sqlite3 atomically."""
    ensure_schema(db)
    source_key = "sqlite:call_history.sqlite3"
    if _migration_done(db, source_key):
        return 0
    if not source_path.is_file() or source_path.resolve() == Path(db.path).resolve():
        _mark_migration(db, source_key, 0, "")
        return 0

    CallHistoryStore(str(db.path))

    con = sqlite3.connect(db.path, timeout=30, isolation_level=None)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("ATTACH DATABASE ? AS legacy_history", (str(source_path),))
        con.execute("BEGIN IMMEDIATE")
        copied = 0

        for table in ("calls", "activity", "call_events"):
            exists = con.execute(
                "SELECT 1 FROM legacy_history.sqlite_schema WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            src = set(_table_columns(con, "legacy_history", table))
            dst = _table_columns(con, "main", table)
            columns = [c for c in dst if c in src and c != "id"]
            if not columns:
                continue
            quoted = ",".join('"' + c.replace('"', '""') + '"' for c in columns)
            before = con.total_changes
            if table == "calls":
                con.execute(
                    f'INSERT OR IGNORE INTO main."{table}"({quoted}) '
                    f'SELECT {quoted} FROM legacy_history."{table}"'
                )
            else:
                con.execute(
                    f'INSERT INTO main."{table}"({quoted}) '
                    f'SELECT {quoted} FROM legacy_history."{table}"'
                )
            copied += max(0, con.total_changes - before)

        con.execute(
            """INSERT INTO storage_migrations(source,completed_at,row_count,source_sha256)
               VALUES(?,?,?,?)
               ON CONFLICT(source) DO UPDATE SET
                 completed_at=excluded.completed_at,row_count=excluded.row_count,
                 source_sha256=excluded.source_sha256""",
            (source_key, time.time(), copied, _sha256(source_path)),
        )
        con.execute("COMMIT")
        return copied
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        try:
            con.execute("DETACH DATABASE legacy_history")
        except Exception:
            pass
        con.close()


def sync_identity_directory(db) -> None:
    """Unify Discord, local-user, and break-glass identities in one directory."""
    ensure_schema(db)
    now = time.time()
    with db.transaction() as con:
        for row in con.execute("SELECT * FROM users").fetchall():
            key = str(row["discord_user_id"])
            seen = float(row["last_seen"] or 0)
            con.execute(
                """INSERT INTO identity_directory(
                     identity_key,auth_type,source_user_id,username,display_name,
                     avatar_url,is_system_admin,first_seen,last_seen,last_login,login_count
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,0)
                   ON CONFLICT(identity_key) DO UPDATE SET
                     username=excluded.username,
                     display_name=excluded.display_name,
                     avatar_url=excluded.avatar_url,
                     is_system_admin=excluded.is_system_admin,
                     last_seen=MAX(identity_directory.last_seen,excluded.last_seen)""",
                (
                    key, "discord", key, str(row["username"]), str(row["display_name"]),
                    str(row["avatar_url"]), int(bool(row["is_system_admin"])),
                    seen or now, seen, seen,
                ),
            )

        for row in con.execute("SELECT * FROM local_users").fetchall():
            source_id = str(row["id"])
            key = f"localuser:{source_id}"
            last_login = float(row["last_login"] or 0)
            con.execute(
                """INSERT INTO identity_directory(
                     identity_key,auth_type,source_user_id,username,display_name,
                     avatar_url,is_system_admin,first_seen,last_seen,last_login,login_count
                   ) VALUES(?,?,?,?,?,'',?,?,?,?,0)
                   ON CONFLICT(identity_key) DO UPDATE SET
                     username=excluded.username,
                     display_name=excluded.display_name,
                     is_system_admin=excluded.is_system_admin,
                     last_login=MAX(identity_directory.last_login,excluded.last_login)""",
                (
                    key, "local_user", source_id, str(row["username"]),
                    str(row["display_name"]), int(bool(row["is_system_admin"])),
                    float(row["created_at"] or now), last_login, last_login,
                ),
            )

        admin = con.execute(
            "SELECT username,updated_at FROM local_admin WHERE id=1"
        ).fetchone()
        if admin:
            created = float(admin["updated_at"] or now)
            con.execute(
                """INSERT INTO identity_directory(
                     identity_key,auth_type,source_user_id,username,display_name,
                     avatar_url,is_system_admin,first_seen,last_seen,last_login,login_count
                   ) VALUES('local:admin','local','local:admin',?,'Local administrator','',1,?,?,0,0)
                   ON CONFLICT(identity_key) DO UPDATE SET username=excluded.username""",
                (str(admin["username"]), created, 0.0),
            )


def record_login(db, user_id: str, auth_type: str) -> None:
    sync_identity_directory(db)
    now = time.time()
    key = str(user_id)
    with db.transaction() as con:
        row = con.execute(
            "SELECT 1 FROM identity_directory WHERE identity_key=?", (key,)
        ).fetchone()
        if row:
            con.execute(
                """UPDATE identity_directory
                   SET auth_type=?,last_seen=?,last_login=?,login_count=login_count+1
                   WHERE identity_key=?""",
                (str(auth_type), now, now, key),
            )
        else:
            con.execute(
                """INSERT INTO identity_directory(
                     identity_key,auth_type,source_user_id,username,display_name,
                     avatar_url,is_system_admin,first_seen,last_seen,last_login,login_count
                   ) VALUES(?,?,?,?,?,'',0,?,?,?,1)""",
                (key, str(auth_type), key, key, key, now, now, now),
            )


def list_online_users(db, window_seconds: int = ONLINE_WINDOW_SECONDS) -> list[dict]:
    sync_identity_directory(db)
    now = time.time()
    cutoff = now - max(30, int(window_seconds))
    with db._lock, db._connect() as con:
        rows = con.execute(
            """SELECT s.user_id,s.auth_type,MAX(s.last_seen) AS session_last_seen,
                      d.username,d.display_name,d.avatar_url,d.is_system_admin
               FROM web_sessions s
               LEFT JOIN identity_directory d ON d.identity_key=s.user_id
               WHERE s.expires_at>? AND s.last_seen>=?
               GROUP BY s.user_id,s.auth_type,d.username,d.display_name,
                        d.avatar_url,d.is_system_admin
               ORDER BY session_last_seen DESC""",
            (now, cutoff),
        ).fetchall()
    return [
        {
            "user_id": str(r["user_id"]),
            "auth_type": str(r["auth_type"]),
            "username": str(r["username"] or ""),
            "name": str(r["display_name"] or r["username"] or r["user_id"]),
            "avatar_url": str(r["avatar_url"] or ""),
            "system_admin": bool(r["is_system_admin"]),
            "last_seen": float(r["session_last_seen"] or 0),
        }
        for r in rows
    ]


def known_user_count(db) -> int:
    sync_identity_directory(db)
    with db._lock, db._connect() as con:
        return int(con.execute("SELECT COUNT(*) FROM identity_directory").fetchone()[0])


def database_counts(db) -> dict[str, int]:
    wanted = (
        "users", "local_users", "identity_directory", "web_sessions",
        "workspaces", "workspace_roles", "calls", "activity", "call_events", "audit_log",
    )
    out = {}
    with db._lock, db._connect() as con:
        existing = {
            str(r[0]) for r in con.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        for table in wanted:
            if table in existing:
                safe = table.replace('"', '""')
                out[table] = int(con.execute(
                    f'SELECT COUNT(*) FROM "{safe}"'
                ).fetchone()[0])
    return out


def _inject_online_users_ui(text: str) -> str:
    text = str(text or "")
    if 'id="pbx-online-users-script"' in text:
        return text
    addon = r"""<style id="pbx-online-users-style">
#onlineUsersPanel{margin-bottom:12px}.onlineUserGrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:8px;margin-top:10px}
.onlineUser{display:flex;align-items:center;gap:9px;padding:9px 10px;border:1px solid var(--border);border-radius:10px;background:var(--panel3)}
.onlineUser img,.onlineAvatar{width:34px;height:34px;border-radius:50%;object-fit:cover;background:var(--panel2);display:grid;place-items:center;font-weight:800}
.onlineDot{width:8px;height:8px;border-radius:50%;background:#3cff8f;box-shadow:0 0 8px #3cff8f}
</style>
<script id="pbx-online-users-script">
(()=>{'use strict';
const $=s=>document.querySelector(s);
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function mount(){const host=$('#workspaces');if(!host||$('#onlineUsersPanel'))return;const card=document.createElement('section');card.className='card';card.id='onlineUsersPanel';card.innerHTML='<div class="row"><div><h2 style="margin:0">Online users</h2><div class="muted">Active PBX panel sessions</div></div><span class="tag" id="knownUserCount" style="margin-left:auto">— users</span></div><div class="onlineUserGrid" id="onlineUserGrid"><div class="muted">Loading…</div></div><div class="muted small" id="databaseState" style="margin-top:8px"></div>';host.prepend(card)}
async function refresh(){mount();const grid=$('#onlineUserGrid');if(!grid)return;try{const r=await fetch('/api/status',{cache:'no-store',credentials:'same-origin'});if(!r.ok)return;const j=await r.json();const users=Array.isArray(j.online_users)?j.online_users:[];const count=$('#knownUserCount');if(count)count.textContent=`${j.known_user_count??users.length} known · ${users.length} online`;grid.innerHTML=users.length?users.map(u=>`<div class="onlineUser">${u.avatar_url?`<img src="${esc(u.avatar_url)}" alt="">`:`<div class="onlineAvatar">${esc((u.name||'?').slice(0,1).toUpperCase())}</div>`}<div style="min-width:0;flex:1"><b>${esc(u.name||u.username||u.user_id)}</b><div class="muted small">${esc(u.auth_type)}${u.system_admin?' · admin':''}</div></div><span class="onlineDot" title="Online"></span></div>`).join(''):'<div class="muted">Nobody else is currently active.</div>';const state=$('#databaseState');if(state&&j.database_storage?.consolidated)state.textContent=`Storage: ${j.database_storage.engine} · ${j.database_storage.file}`;}catch(e){}}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>{mount();refresh()},{once:true});else{mount();refresh()}
setInterval(refresh,20000);
})();
</script>"""
    return text.replace("</body>", addon + "</body>", 1) if "</body>" in text else text + addon


def _patch_appdb_sessions() -> None:
    from appdb import AppDatabase
    if getattr(AppDatabase, "_identity_directory_patch", False):
        return
    original = AppDatabase.create_session

    def create_session(self, user_id: str, auth_type: str, **kwargs):
        result = original(self, user_id, auth_type, **kwargs)
        try:
            ensure_schema(self)
            record_login(self, str(user_id), str(auth_type))
        except Exception:
            log.exception("Could not record user login in identity directory")
        return result

    AppDatabase.create_session = create_session
    AppDatabase._identity_directory_patch = True


def _patch_web_server() -> None:
    try:
        import webui_v3
    except ModuleNotFoundError:
        import webui as webui_v3

    cls = webui_v3.WebControlServer
    if getattr(cls, "_database_consolidation_applied", False):
        return

    original_init = cls.__init__
    original_status = cls.status
    original_index = cls.index

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        data_dir = Path(self.config.data_dir)
        try:
            ensure_schema(self.db)
            backup_and_catalog_legacy_data(self.db, data_dir)
            legacy_history = self.call_history
            source_path = Path(getattr(legacy_history, "path", data_dir / "call_history.sqlite3"))
            copied = migrate_call_history(self.db, source_path)
            self.call_history = CallHistoryStore(str(self.db.path))
            sync_identity_directory(self.db)
            self._database_consolidated = True
            self._database_history_rows_migrated = copied
        except Exception:
            self._database_consolidated = False
            self._database_history_rows_migrated = 0
            log.exception("Database consolidation failed; retaining legacy call-history store")

    async def status(self, request):
        response = await original_status(self, request)
        try:
            if int(getattr(response, "status", 200) or 200) >= 400:
                return response
            payload = json.loads(response.text)
            payload["online_users"] = list_online_users(self.db)
            payload["known_user_count"] = known_user_count(self.db)
            payload["database_storage"] = {
                "consolidated": bool(getattr(self, "_database_consolidated", False)),
                "engine": "SQLite/WAL",
                "file": "pbx_app.sqlite3",
                "component_schema": DB_COMPONENT_VERSION,
                "counts": database_counts(self.db),
            }
            return web.json_response(payload, status=response.status)
        except Exception:
            log.exception("Could not append database/user status")
            return response

    async def index(self, request):
        response = await original_index(self, request)
        try:
            if getattr(response, "content_type", "") == "text/html" and response.text:
                response.text = _inject_online_users_ui(response.text)
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        except Exception:
            pass
        return response

    cls.__init__ = init
    cls.status = status
    cls.index = index
    cls._database_consolidation_applied = True


def apply() -> None:
    _patch_appdb_sessions()
    _patch_web_server()
