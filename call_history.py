from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class CallHistoryStore:
    """Durable call history plus per-call state timeline.

    v3 keeps compatibility with the v2.x calls/activity tables and migrates new
    attribution/workspace columns in place, so copying the old data directory is safe.
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=15.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def _columns(self, con, table: str) -> set[str]:
        return {str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()}

    def _init_db(self) -> None:
        with self._lock, self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uuid TEXT NOT NULL UNIQUE,
                    direction TEXT NOT NULL DEFAULT '',
                    number TEXT NOT NULL DEFAULT '',
                    caller_id TEXT NOT NULL DEFAULT '',
                    contact_name TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL DEFAULT 0,
                    connected_at REAL NOT NULL DEFAULT 0,
                    ended_at REAL NOT NULL DEFAULT 0,
                    duration REAL NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT '',
                    disposition TEXT NOT NULL DEFAULT '',
                    diagnostic TEXT NOT NULL DEFAULT '',
                    retry_of TEXT NOT NULL DEFAULT '',
                    retry_index INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_calls_created ON calls(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_calls_number ON calls(number);
                CREATE INDEX IF NOT EXISTS idx_calls_outcome ON calls(outcome);
                CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    uuid TEXT NOT NULL DEFAULT '',
                    number TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_activity_created ON activity(created_at DESC);
                CREATE TABLE IF NOT EXISTS call_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_uuid TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    event TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT '',
                    actor_user_id TEXT NOT NULL DEFAULT '',
                    actor_name TEXT NOT NULL DEFAULT '',
                    workspace_id TEXT NOT NULL DEFAULT '',
                    detail_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_call_events_uuid ON call_events(call_uuid, id);
                """
            )
            cols = self._columns(con, "calls")
            additions = {
                "workspace_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "operator_user_id": "TEXT NOT NULL DEFAULT ''",
                "operator_name": "TEXT NOT NULL DEFAULT ''",
                "answered_by_user_id": "TEXT NOT NULL DEFAULT ''",
                "answered_by_name": "TEXT NOT NULL DEFAULT ''",
                "route_reason": "TEXT NOT NULL DEFAULT ''",
            }
            for name, ddl in additions.items():
                if name not in cols:
                    con.execute(f"ALTER TABLE calls ADD COLUMN {name} {ddl}")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        try:
            d["workspace_ids"] = json.loads(d.get("workspace_ids_json", "[]"))
        except Exception:
            d["workspace_ids"] = []
        d.pop("workspace_ids_json", None)
        return d

    def start_call(
        self, *, uuid: str, direction: str, number: str = "", caller_id: str = "",
        contact_name: str = "", source: str = "", state: str = "starting",
        retry_of: str = "", retry_index: int = 0, workspace_ids: list[str] | None = None,
        operator_user_id: str = "", operator_name: str = "", route_reason: str = "",
    ) -> None:
        now = time.time()
        ws_json = json.dumps(list(workspace_ids or []), separators=(",", ":"))
        with self._lock, self._connect() as con:
            con.execute(
                """
                INSERT INTO calls(uuid,direction,number,caller_id,contact_name,source,state,outcome,created_at,retry_of,retry_index,workspace_ids_json,operator_user_id,operator_name,route_reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(uuid) DO UPDATE SET
                    direction=excluded.direction,
                    number=CASE WHEN excluded.number<>'' THEN excluded.number ELSE calls.number END,
                    caller_id=CASE WHEN excluded.caller_id<>'' THEN excluded.caller_id ELSE calls.caller_id END,
                    contact_name=CASE WHEN excluded.contact_name<>'' THEN excluded.contact_name ELSE calls.contact_name END,
                    source=CASE WHEN excluded.source<>'' THEN excluded.source ELSE calls.source END,
                    state=excluded.state,
                    retry_of=CASE WHEN excluded.retry_of<>'' THEN excluded.retry_of ELSE calls.retry_of END,
                    retry_index=MAX(calls.retry_index, excluded.retry_index),
                    workspace_ids_json=CASE WHEN excluded.workspace_ids_json<>'[]' THEN excluded.workspace_ids_json ELSE calls.workspace_ids_json END,
                    operator_user_id=CASE WHEN excluded.operator_user_id<>'' THEN excluded.operator_user_id ELSE calls.operator_user_id END,
                    operator_name=CASE WHEN excluded.operator_name<>'' THEN excluded.operator_name ELSE calls.operator_name END,
                    route_reason=CASE WHEN excluded.route_reason<>'' THEN excluded.route_reason ELSE calls.route_reason END
                """,
                (uuid, direction, number, caller_id, contact_name, source, state, "", now, retry_of, int(retry_index), ws_json, operator_user_id, operator_name, route_reason),
            )
        self.event(uuid, "created", state=state, actor_user_id=operator_user_id, actor_name=operator_name, detail={"direction": direction, "source": source, "workspaces": workspace_ids or []})

    def set_state(self, uuid: str, state: str, *, detail: Any = None) -> None:
        state = str(state)[:120]
        with self._lock, self._connect() as con:
            con.execute("UPDATE calls SET state=? WHERE uuid=?", (state, uuid))
        self.event(uuid, "state", state=state, detail=detail or {})

    def set_workspaces(self, uuid: str, workspace_ids: list[str], route_reason: str = "") -> None:
        with self._lock, self._connect() as con:
            con.execute("UPDATE calls SET workspace_ids_json=?, route_reason=CASE WHEN ?<>'' THEN ? ELSE route_reason END WHERE uuid=?", (json.dumps(workspace_ids, separators=(",", ":")), route_reason, route_reason[:300], uuid))
        self.event(uuid, "routing", detail={"workspace_ids": workspace_ids, "reason": route_reason})

    def set_answered_by(self, uuid: str, user_id: str, name: str, workspace_id: str = "") -> None:
        with self._lock, self._connect() as con:
            con.execute("UPDATE calls SET answered_by_user_id=?,answered_by_name=? WHERE uuid=?", (str(user_id), str(name)[:120], uuid))
        self.event(uuid, "claimed", actor_user_id=str(user_id), actor_name=name, workspace_id=workspace_id)

    def connected(self, uuid: str, *, direction: str = "", number: str = "", caller_id: str = "", contact_name: str = "", source: str = "", workspace_ids: list[str] | None = None, operator_user_id: str = "", operator_name: str = "") -> None:
        now = time.time()
        ws_json = json.dumps(list(workspace_ids or []), separators=(",", ":"))
        with self._lock, self._connect() as con:
            con.execute(
                """
                INSERT INTO calls(uuid,direction,number,caller_id,contact_name,source,state,outcome,created_at,connected_at,workspace_ids_json,operator_user_id,operator_name)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(uuid) DO UPDATE SET
                    direction=CASE WHEN excluded.direction<>'' THEN excluded.direction ELSE calls.direction END,
                    number=CASE WHEN excluded.number<>'' THEN excluded.number ELSE calls.number END,
                    caller_id=CASE WHEN excluded.caller_id<>'' THEN excluded.caller_id ELSE calls.caller_id END,
                    contact_name=CASE WHEN excluded.contact_name<>'' THEN excluded.contact_name ELSE calls.contact_name END,
                    source=CASE WHEN excluded.source<>'' THEN excluded.source ELSE calls.source END,
                    state='connected', outcome='answered', connected_at=?,
                    workspace_ids_json=CASE WHEN excluded.workspace_ids_json<>'[]' THEN excluded.workspace_ids_json ELSE calls.workspace_ids_json END,
                    operator_user_id=CASE WHEN excluded.operator_user_id<>'' THEN excluded.operator_user_id ELSE calls.operator_user_id END,
                    operator_name=CASE WHEN excluded.operator_name<>'' THEN excluded.operator_name ELSE calls.operator_name END
                """,
                (uuid, direction, number, caller_id, contact_name, source, "connected", "answered", now, now, ws_json, operator_user_id, operator_name, now),
            )
        self.event(uuid, "connected", state="connected", actor_user_id=operator_user_id, actor_name=operator_name, detail={"workspaces": workspace_ids or []})

    def finish(self, uuid: str, *, outcome: str = "ended", duration: float = 0.0, diagnostic: str = "") -> None:
        now = time.time()
        with self._lock, self._connect() as con:
            con.execute(
                "UPDATE calls SET state='ended', outcome=?, ended_at=?, duration=?, diagnostic=CASE WHEN ?<>'' THEN ? ELSE diagnostic END WHERE uuid=?",
                (str(outcome)[:64], now, max(0.0, float(duration or 0)), diagnostic, diagnostic[:800], uuid),
            )
        self.event(uuid, "ended", state="ended", detail={"outcome": outcome, "duration": duration, "diagnostic": diagnostic[:500]})

    def fail(self, uuid: str, *, outcome: str = "failed", diagnostic: str = "") -> None:
        now = time.time()
        with self._lock, self._connect() as con:
            con.execute("UPDATE calls SET state='failed', outcome=?, ended_at=?, diagnostic=? WHERE uuid=?", (str(outcome)[:64], now, str(diagnostic or "")[:800], uuid))
        self.event(uuid, "failed", state="failed", detail={"outcome": outcome, "diagnostic": diagnostic[:500]})

    def update_notes(self, uuid: str, *, notes: str | None = None, disposition: str | None = None) -> bool:
        sets: list[str] = []
        vals: list[Any] = []
        if notes is not None:
            sets.append("notes=?")
            vals.append(str(notes)[:2000])
        if disposition is not None:
            sets.append("disposition=?")
            vals.append(str(disposition)[:80])
        if not sets:
            return False
        vals.append(uuid)
        with self._lock, self._connect() as con:
            cur = con.execute(f"UPDATE calls SET {', '.join(sets)} WHERE uuid=?", vals)
        if cur.rowcount:
            self.event(uuid, "notes", detail={"notes": notes if notes is not None else None, "disposition": disposition if disposition is not None else None})
        return cur.rowcount > 0

    def event(self, uuid: str, event: str, *, state: str = "", actor_user_id: str = "", actor_name: str = "", workspace_id: str = "", detail: Any = None) -> None:
        try:
            detail_json = json.dumps(detail if detail is not None else {}, separators=(",", ":"), sort_keys=True)[:8000]
        except Exception:
            detail_json = "{}"
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO call_events(call_uuid,created_at,event,state,actor_user_id,actor_name,workspace_id,detail_json) VALUES(?,?,?,?,?,?,?,?)",
                (uuid, time.time(), str(event)[:120], str(state)[:120], str(actor_user_id)[:80], str(actor_name)[:120], str(workspace_id)[:80], detail_json),
            )

    def timeline(self, uuid: str, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock, self._connect() as con:
            rows = con.execute("SELECT * FROM call_events WHERE call_uuid=? ORDER BY id ASC LIMIT ?", (uuid, min(2000, max(1, int(limit))))).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d.pop("detail_json"))
            except Exception:
                d["detail"] = {}
            out.append(d)
        return out

    def migrate_legacy_workspace(self, workspace_id: str) -> int:
        """Scope v2 call history/events to the initial v3 workspace.

        Pre-v3 rows have no workspace attribution. Once multiple guilds exist, those
        rows must belong to the migrated/default workspace rather than disappearing
        from filtered history or leaking across workspace boundaries.
        """
        workspace_id = str(workspace_id or "").strip()
        if not workspace_id:
            return 0
        ws_json = json.dumps([workspace_id], separators=(",", ":"))
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT uuid,workspace_ids_json FROM calls WHERE workspace_ids_json IS NULL OR TRIM(workspace_ids_json) IN ('', '[]')"
            ).fetchall()
            uuids = [str(r["uuid"]) for r in rows]
            if uuids:
                con.execute(
                    "UPDATE calls SET workspace_ids_json=? WHERE workspace_ids_json IS NULL OR TRIM(workspace_ids_json) IN ('', '[]')",
                    (ws_json,),
                )
                # Old call events had no workspace field. Only attach events belonging
                # to the migrated calls so newer explicitly-global/system events are
                # not rewritten accidentally.
                for call_uuid in uuids:
                    con.execute(
                        "UPDATE call_events SET workspace_id=? WHERE call_uuid=? AND (workspace_id IS NULL OR TRIM(workspace_id)='')",
                        (workspace_id, call_uuid),
                    )
            return len(uuids)

    def get_by_uuid(self, uuid: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as con:
            return self._row(con.execute("SELECT * FROM calls WHERE uuid=?", (uuid,)).fetchone())

    def get_by_id(self, row_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as con:
            return self._row(con.execute("SELECT * FROM calls WHERE id=?", (int(row_id),)).fetchone())

    def list_calls(self, *, limit: int = 250, offset: int = 0, q: str = "", direction: str = "", outcome: str = "", answered: bool = False, missed: bool = False, workspace_id: str = "", workspace_ids: list[str] | None = None, operator_user_id: str = "") -> dict[str, Any]:
        limit = min(500, max(1, int(limit))); offset = max(0, int(offset))
        clauses: list[str] = []; vals: list[Any] = []
        if q:
            clauses.append("(number LIKE ? OR contact_name LIKE ? OR caller_id LIKE ? OR notes LIKE ? OR disposition LIKE ? OR operator_name LIKE ? OR answered_by_name LIKE ?)")
            needle = f"%{q}%"; vals.extend([needle] * 7)
        if direction:
            clauses.append("direction=?"); vals.append(direction)
        if outcome:
            clauses.append("outcome=?"); vals.append(outcome)
        if answered:
            clauses.append("connected_at>0 AND outcome<>'voicemail'")
        if missed:
            clauses.append("outcome IN ('missed','no answer','timeout')")
        if workspace_id:
            clauses.append("workspace_ids_json LIKE ?"); vals.append(f'%"{workspace_id}"%')
        elif workspace_ids:
            ids = [str(x) for x in workspace_ids if str(x)]
            if ids:
                clauses.append("(" + " OR ".join(["workspace_ids_json LIKE ?"] * len(ids)) + ")")
                vals.extend([f'%"{wid}"%' for wid in ids])
        if operator_user_id:
            clauses.append("operator_user_id=?"); vals.append(operator_user_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock, self._connect() as con:
            total = int(con.execute("SELECT COUNT(*) FROM calls" + where, vals).fetchone()[0])
            rows = con.execute("SELECT * FROM calls" + where + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", [*vals, limit, offset]).fetchall()
        return {"calls": [self._row(r) for r in rows], "total": total, "limit": limit, "offset": offset}

    def stats(self, workspace_id: str = "") -> dict[str, Any]:
        now = time.time(); lt = time.localtime(now)
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst))
        where = "created_at>=?"; vals: list[Any] = [midnight]
        if workspace_id:
            where += " AND workspace_ids_json LIKE ?"; vals.append(f'%"{workspace_id}"%')
        with self._lock, self._connect() as con:
            row = con.execute(f"""
                SELECT COUNT(*) total,
                  SUM(CASE WHEN (outcome='answered' OR connected_at>0) AND outcome<>'voicemail' THEN 1 ELSE 0 END) answered,
                  SUM(CASE WHEN outcome IN ('missed','no answer','timeout') THEN 1 ELSE 0 END) missed,
                  SUM(CASE WHEN outcome='busy' THEN 1 ELSE 0 END) busy,
                  SUM(CASE WHEN direction='inbound' THEN 1 ELSE 0 END) inbound,
                  SUM(CASE WHEN direction='outbound' THEN 1 ELSE 0 END) outbound,
                  AVG(CASE WHEN duration>0 THEN duration END) avg_duration
                FROM calls WHERE {where}
            """, vals).fetchone()
        return {"today_total": int(row["total"] or 0), "today_answered": int(row["answered"] or 0), "today_missed": int(row["missed"] or 0), "today_busy": int(row["busy"] or 0), "today_inbound": int(row["inbound"] or 0), "today_outbound": int(row["outbound"] or 0), "avg_duration": round(float(row["avg_duration"] or 0), 1)}

    # v2 activity feed retained as a compatibility/debug stream. v3's authoritative
    # administrator audit trail is appdb.audit_log and includes authenticated identity.
    def log_activity(self, action: str, detail: str = "", *, uuid: str = "", number: str = "") -> None:
        with self._lock, self._connect() as con:
            con.execute("INSERT INTO activity(created_at,action,detail,uuid,number) VALUES(?,?,?,?,?)", (time.time(), str(action)[:120], str(detail)[:1000], str(uuid)[:80], str(number)[:80]))
            con.execute("DELETE FROM activity WHERE id NOT IN (SELECT id FROM activity ORDER BY id DESC LIMIT 3000)")

    def activity(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock, self._connect() as con:
            rows = con.execute("SELECT * FROM activity ORDER BY created_at DESC,id DESC LIMIT ?", (min(500, max(1, int(limit))),)).fetchall()
        return [dict(r) for r in rows]

    def prune(self, call_days: int = 365, event_days: int = 365, activity_days: int = 90) -> dict[str, int]:
        now = time.time(); out = {}
        with self._lock, self._connect() as con:
            cur = con.execute("DELETE FROM call_events WHERE created_at<?", (now - max(1, event_days) * 86400,)); out["events"] = cur.rowcount
            cur = con.execute("DELETE FROM activity WHERE created_at<?", (now - max(1, activity_days) * 86400,)); out["activity"] = cur.rowcount
            # Calls are kept longer; deleting a call also leaves no sensitive content in call_events after its separate prune.
            cur = con.execute("DELETE FROM calls WHERE created_at<?", (now - max(1, call_days) * 86400,)); out["calls"] = cur.rowcount
        return out
