from __future__ import annotations

import csv
import io
from typing import Any, Iterable

from aiohttp import web


def import_contact_rows_allow_global_create(
    server,
    actor: dict[str, Any],
    workspace_id: str,
    rows: Iterable[dict],
    *,
    merge: bool = True,
) -> dict[str, int]:
    """Import contacts while allowing contacts-capable users to add globals.

    A non-system administrator may create a new global contact, but may not use
    a CSV number collision to mutate an existing global contact or promote an
    existing workspace-owned contact to global scope. Validation is completed
    before writes so a forbidden row cannot leave a partially imported file.
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
                # Preserve an existing contact's ownership when the CSV does not
                # explicitly request a scope change.
                if not str(row.get("scope", "") or "").strip():
                    row.pop("scope", None)
                values = server._contact_values_v3(row, workspace_id, existing)
            else:
                values = probe
        except Exception:
            invalid += 1
            continue

        if not system_admin and existing and merge:
            existing_scope = str(existing.get("scope", "workspace") or "workspace").lower()
            requested_scope = str(values.get("scope", "workspace") or "workspace").lower()
            if existing_scope == "global" or requested_scope == "global":
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


def patch_console_html(text: str) -> str:
    """Keep the contact UI aligned with the server's global-contact policy."""
    text = str(text or "")

    # v3.3.10 disabled the Global selector for every non-system-admin. The
    # contacts API itself still requires the workspace `contacts` capability,
    # so the selector can safely remain enabled for operators who reach it.
    text = text.replace(
        "const g=$('#contactScope option[value=global]');if(g)g.disabled=!me?.system_admin;",
        "const g=$('#contactScope option[value=global]');if(g)g.disabled=false;",
    )

    # Creating a global contact is collaborative; modifying or deleting an
    # existing global entry remains system-admin-only. Do not present controls
    # that the backend will reject for ordinary operators.
    text = text.replace(
        "${canWs(contactWorkspace(c),'contacts')?`<button class=\"btn\" data-c-edit=",
        "${canWs(contactWorkspace(c),'contacts')&&(c.scope!=='global'||me?.system_admin)?`<button class=\"btn\" data-c-edit=",
    )
    return text


def _patch_web_server() -> None:
    import webui_v3

    cls = webui_v3.WebControlServer
    if getattr(cls, "_global_contact_create_applied", False):
        return

    original_index = cls.index

    async def contacts_create(self, request):
        # `_workspace(..., "contacts")` is the authorization boundary: any
        # authenticated operator with Contacts permission may add either a
        # workspace or global contact. No system-admin escalation is required.
        _, ws = await self._workspace(request, "contacts")
        try:
            values = self._contact_values_v3(await request.json(), ws["id"])
            item = self.contacts.create(**values)
            return web.json_response(
                {"ok": True, "contact": self._decorate_contact(item)},
                status=201,
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def contacts_import(self, request):
        actor, ws = await self._workspace(request, "contacts")
        try:
            body = await request.json()
            raw = str(body.get("csv", body.get("raw", "")))
            rows = list(csv.DictReader(io.StringIO(raw)))
            result = import_contact_rows_allow_global_create(
                self,
                actor,
                ws["id"],
                rows,
                merge=bool(body.get("merge", True)),
            )
            if result["forbidden"]:
                return web.json_response(
                    {
                        "ok": False,
                        "error": (
                            "Global contacts may be added by Contacts users, but "
                            "modifying an existing global contact or promoting an "
                            "existing contact to global requires system-administrator access"
                        ),
                        **result,
                    },
                    status=403,
                )
            return web.json_response({"ok": True, **result})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def index(self, request):
        response = await original_index(self, request)
        try:
            if getattr(response, "content_type", "") == "text/html" and response.text:
                response.text = patch_console_html(response.text)
        except Exception:
            pass
        return response

    cls.contacts_create = contacts_create
    cls.contacts_import = contacts_import
    cls.index = index
    cls._global_contact_create_applied = True


def apply() -> None:
    _patch_web_server()
