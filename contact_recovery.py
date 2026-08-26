from __future__ import annotations

import logging
import time

log = logging.getLogger("discord-pbx.contacts.recovery")


def repair_contact_ownership(server, preferred_workspace_id: str = "") -> int:
    """Repair legacy/orphaned contact workspace ownership without crossing valid tenants.

    Only non-global contacts whose workspace is blank or references a workspace that
    no longer exists are moved. Contacts belonging to another valid workspace are
    never reassigned.
    """
    valid_rows = list(server.db.list_workspaces() or [])
    valid_ids = {str(row.get("id", "") or "") for row in valid_rows}
    valid_ids.discard("")
    if not valid_ids:
        return 0

    preferred = str(preferred_workspace_id or "").strip()
    fallback = preferred if preferred in valid_ids else ""
    if not fallback:
        try:
            default = server.workspaces.default_workspace() or {}
        except Exception:
            default = {}
        candidate = str(default.get("id", "") or "")
        if candidate in valid_ids:
            fallback = candidate
    if not fallback:
        fallback = sorted(valid_ids)[0]

    store = server.contacts
    changed = 0
    now = time.time()
    with store._lock:
        contacts = store._read()
        for contact in contacts:
            scope = str(contact.get("scope", "workspace") or "workspace").lower()
            if scope == "global":
                continue
            workspace_id = str(contact.get("workspace_id", "") or "")
            if workspace_id and workspace_id in valid_ids:
                continue
            contact["workspace_id"] = fallback
            contact["scope"] = "workspace"
            contact["updated_at"] = now
            changed += 1
        if changed:
            store._write(contacts)

    if changed:
        log.warning(
            "Recovered %d contact(s) with missing/orphaned workspace ownership into %s",
            changed,
            fallback,
        )
    return changed


def apply() -> None:
    """Make contact/speed-dial data self-healing before the web server is instantiated."""
    import webui_v3

    cls = webui_v3.WebControlServer
    if getattr(cls, "_contact_recovery_applied", False):
        return

    original_init = cls.__init__
    original_contacts_list = cls.contacts_list

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        repair_contact_ownership(self)

    async def contacts_list(self, request):
        # Re-run the cheap ownership check on reads so a workspace deleted/changed
        # while the process is running cannot make its contacts silently disappear.
        preferred = str(request.headers.get("X-PBX-Workspace", "") or "")
        repair_contact_ownership(self, preferred)
        return await original_contacts_list(self, request)

    cls.__init__ = init
    cls.contacts_list = contacts_list
    cls._contact_recovery_applied = True
