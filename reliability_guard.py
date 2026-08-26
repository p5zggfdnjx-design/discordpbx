from __future__ import annotations

import csv
import io
import json
import logging
import re
from typing import Any, Iterable

from aiohttp import web

log = logging.getLogger("discord-pbx.reliability")


def clear_workspace_capability_cache(server, workspace_id: str = "") -> int:
    """Invalidate cached Discord role capabilities after RBAC/workspace changes."""
    cache = getattr(getattr(server, "workspaces", None), "_member_caps_cache", None)
    if not isinstance(cache, dict):
        return 0
    workspace_id = str(workspace_id or "")
    if not workspace_id:
        count = len(cache)
        cache.clear()
        return count
    keys = [key for key in cache if isinstance(key, tuple) and len(key) >= 2 and str(key[1]) == workspace_id]
    for key in keys:
        cache.pop(key, None)
    return len(keys)


def import_contact_rows(server, actor: dict[str, Any], workspace_id: str, rows: Iterable[dict], *, merge: bool = True) -> dict[str, int]:
    """Import contact rows without allowing tenant/global ownership escalation.

    Non-system administrators may import contacts only into their current workspace.
    They may not create global contacts or mutate an existing global contact through
    a number collision. Validation happens before mutation so a forbidden row cannot
    leave a partially imported file behind.
    """
    workspace_id = str(workspace_id or "")
    system_admin = bool(actor.get("system_admin"))
    prepared: list[tuple[dict, dict | None]] = []
    invalid = 0
    forbidden = 0

    for raw in rows:
        row = dict(raw or {})
        try:
            probe = server._contact_values_v3(row, workspace_id)
            existing = server.contacts.find_by_number(probe["number"], workspace_id)
            if existing:
                # Preserve existing ownership when the CSV does not explicitly
                # provide a scope value. This is especially important for global
                # contacts exported by older builds.
                if not str(row.get("scope", "") or "").strip():
                    row.pop("scope", None)
                values = server._contact_values_v3(row, workspace_id, existing)
            else:
                values = probe
        except Exception:
            invalid += 1
            continue

        if not system_admin and values.get("scope") == "global":
            forbidden += 1
            continue
        if not system_admin and existing and existing.get("scope") == "global" and merge:
            forbidden += 1
            continue
        prepared.append((values, existing))

    if forbidden:
        return {"added": 0, "updated": 0, "invalid": invalid, "forbidden": forbidden}

    added = 0
    updated = 0
    for values, existing in prepared:
        try:
            if existing and merge:
                server.contacts.update(existing["id"], **values)
                updated += 1
            else:
                server.contacts.create(**values)
                added += 1
        except Exception:
            invalid += 1
    return {"added": added, "updated": updated, "invalid": invalid, "forbidden": 0}


def _patch_appdb_revision_restore() -> None:
    from appdb import AppDatabase

    if getattr(AppDatabase, "_reliability_restore_patch", False):
        return
    original = AppDatabase.restore_revision

    def restore_revision(self, revision_id: int) -> bool:
        # Restoring a workspace revision deletes/reinserts workspaces. Because
        # local_user_workspaces has an ON DELETE CASCADE FK, preserve those access
        # mappings explicitly so a configuration rollback cannot lock out local
        # operators that still belong to a restored workspace.
        with self._lock, self._connect() as con:
            mappings = [dict(row) for row in con.execute(
                "SELECT user_id,workspace_id,capabilities_json FROM local_user_workspaces"
            ).fetchall()]
        ok = original(self, revision_id)
        if not ok or not mappings:
            return ok
        restored_ids = {str(row.get("id", "")) for row in self.list_workspaces()}
        with self.transaction() as con:
            for row in mappings:
                if str(row.get("workspace_id", "")) not in restored_ids:
                    continue
                user_exists = con.execute("SELECT 1 FROM local_users WHERE id=?", (row["user_id"],)).fetchone()
                if not user_exists:
                    continue
                con.execute(
                    "INSERT INTO local_user_workspaces(user_id,workspace_id,capabilities_json) VALUES(?,?,?) "
                    "ON CONFLICT(user_id,workspace_id) DO UPDATE SET capabilities_json=excluded.capabilities_json",
                    (row["user_id"], row["workspace_id"], row["capabilities_json"]),
                )
        return ok

    AppDatabase.restore_revision = restore_revision
    AppDatabase._reliability_restore_patch = True


def _patch_web_server() -> None:
    import webui_v3
    from contact_recovery import contact_ownership_summary

    cls = webui_v3.WebControlServer
    if getattr(cls, "_reliability_guard_applied", False):
        return

    original_workspace_create = cls.workspace_create
    original_workspace_update = cls.workspace_update
    original_workspace_delete = cls.workspace_delete
    original_role_update = cls.workspace_role_update
    original_role_delete = cls.workspace_role_delete
    original_contacts_import = cls.contacts_import
    original_revision_restore = cls.revision_restore
    original_diagnostics = cls.diagnostics_v3
    original_index = cls.index

    async def workspace_create(self, request):
        response = await original_workspace_create(self, request)
        if int(getattr(response, "status", 200) or 200) < 400:
            clear_workspace_capability_cache(self)
        return response

    async def workspace_update(self, request):
        response = await original_workspace_update(self, request)
        if int(getattr(response, "status", 200) or 200) < 400:
            clear_workspace_capability_cache(self)
        return response

    async def workspace_delete(self, request):
        actor = await self._system_admin(request)
        wid = str(request.match_info["workspace_id"])
        existing = self.db.get_workspace(wid)
        if existing:
            self.db.save_revision(
                f"before workspace {existing.get('alias', wid)} delete",
                str(actor.get("user_id", "")),
                str(actor.get("name", "")),
            )
        response = await original_workspace_delete(self, request)
        if int(getattr(response, "status", 200) or 200) < 400:
            clear_workspace_capability_cache(self)
        return response

    async def workspace_role_update(self, request):
        wid = str(request.match_info["workspace_id"])
        actor, ws = await self._workspace(request, "workspace_admin", explicit=wid)
        self.db.save_revision(
            f"before workspace {ws.get('alias', wid)} role update",
            str(actor.get("user_id", "")),
            str(actor.get("name", "")),
        )
        response = await original_role_update(self, request)
        if int(getattr(response, "status", 200) or 200) < 400:
            clear_workspace_capability_cache(self, wid)
        return response

    async def workspace_role_delete(self, request):
        wid = str(request.match_info["workspace_id"])
        actor, ws = await self._workspace(request, "workspace_admin", explicit=wid)
        self.db.save_revision(
            f"before workspace {ws.get('alias', wid)} role delete",
            str(actor.get("user_id", "")),
            str(actor.get("name", "")),
        )
        response = await original_role_delete(self, request)
        if int(getattr(response, "status", 200) or 200) < 400:
            clear_workspace_capability_cache(self, wid)
        return response

    async def contacts_import(self, request):
        actor, ws = await self._workspace(request, "contacts")
        try:
            body = await request.json()
            raw = str(body.get("csv", body.get("raw", "")))
            rows = list(csv.DictReader(io.StringIO(raw)))
            result = import_contact_rows(self, actor, ws["id"], rows, merge=bool(body.get("merge", True)))
            if result["forbidden"]:
                return web.json_response(
                    {
                        "ok": False,
                        "error": "CSV import cannot create or modify global contacts without system-administrator access",
                        **result,
                    },
                    status=403,
                )
            return web.json_response({"ok": True, **result})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def revision_restore(self, request):
        response = await original_revision_restore(self, request)
        if int(getattr(response, "status", 200) or 200) < 400:
            clear_workspace_capability_cache(self)
        return response

    async def diagnostics_v3(self, request):
        response = await original_diagnostics(self, request)
        actor = request.get("actor") or {}
        if not actor.get("system_admin") or int(getattr(response, "status", 200) or 200) >= 400:
            return response
        try:
            payload = json.loads(response.text)
            configured = {str(w.get("guild_id", "")) for w in self.db.list_workspaces()}
            connected = [
                {"guild_id": str(g.id), "name": str(g.name)}
                for g in getattr(self.bot, "guilds", [])
            ]
            payload["contact_ownership"] = contact_ownership_summary(self)
            payload["workspace_integrity"] = {
                "configured_count": len(configured),
                "discord_connected_count": len(connected),
                "unconfigured_connected_guilds": [g for g in connected if g["guild_id"] not in configured],
            }
            return web.json_response(payload)
        except Exception:
            log.exception("Could not append reliability diagnostics")
            return response

    async def index(self, request):
        response = await original_index(self, request)
        try:
            if getattr(response, "content_type", "") == "text/html" and response.text:
                version = str(getattr(self.config, "version", "") or "")
                if version:
                    response.text = re.sub(
                        r"Discord ↔ FreePBX operator console · v[0-9A-Za-z._-]+",
                        f"Discord ↔ FreePBX operator console · v{version}",
                        response.text,
                        count=1,
                    )
        except Exception:
            pass
        return response

    cls.workspace_create = workspace_create
    cls.workspace_update = workspace_update
    cls.workspace_delete = workspace_delete
    cls.workspace_role_update = workspace_role_update
    cls.workspace_role_delete = workspace_role_delete
    cls.contacts_import = contacts_import
    cls.revision_restore = revision_restore
    cls.diagnostics_v3 = diagnostics_v3
    cls.index = index
    cls._reliability_guard_applied = True


def apply() -> None:
    _patch_appdb_revision_restore()
    _patch_web_server()
