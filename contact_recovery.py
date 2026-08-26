from __future__ import annotations

import logging
import time

log = logging.getLogger("discord-pbx.contacts.recovery")


def contact_ownership_summary(server) -> dict:
    """Return non-sensitive contact ownership diagnostics for support/health checks."""
    valid_rows = list(server.db.list_workspaces() or [])
    valid_ids = {str(row.get("id", "") or "") for row in valid_rows}
    valid_ids.discard("")

    summary = {
        "total": 0,
        "global": 0,
        "assigned": 0,
        "unassigned": 0,
        "orphaned": 0,
        "orphaned_workspace_ids": [],
    }
    orphaned_ids: set[str] = set()
    store = server.contacts
    with store._lock:
        contacts = store._read()
        summary["total"] = len(contacts)
        for contact in contacts:
            scope = str(contact.get("scope", "workspace") or "workspace").lower()
            if scope == "global":
                summary["global"] += 1
                continue
            workspace_id = str(contact.get("workspace_id", "") or "")
            if not workspace_id:
                summary["unassigned"] += 1
            elif workspace_id in valid_ids:
                summary["assigned"] += 1
            else:
                summary["orphaned"] += 1
                orphaned_ids.add(workspace_id)
    summary["orphaned_workspace_ids"] = sorted(orphaned_ids)
    return summary


def repair_contact_ownership(
    server,
    preferred_workspace_id: str = "",
    *,
    repair_orphans: bool = False,
) -> int:
    """Repair contact workspace ownership without silently crossing tenants.

    Blank workspace IDs are true pre-v3 legacy rows and may be assigned to the
    preferred/default workspace. A non-empty workspace ID is durable ownership
    metadata, even when that workspace is temporarily missing from the local DB.
    Such orphaned rows are preserved by default so restoring the workspace makes
    the contacts visible again. Explicit tooling may opt into ``repair_orphans``.
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
    if not fallback and len(valid_ids) == 1:
        fallback = next(iter(valid_ids))
    if not fallback:
        # With multiple workspaces and no valid preference/default, guessing would
        # risk moving a legacy contact into the wrong Discord tenant.
        return 0

    store = server.contacts
    changed = 0
    orphaned = 0
    orphaned_ids: set[str] = set()
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
            if workspace_id and not repair_orphans:
                orphaned += 1
                orphaned_ids.add(workspace_id)
                continue
            contact["workspace_id"] = fallback
            contact["scope"] = "workspace"
            contact["updated_at"] = now
            changed += 1
        if changed:
            store._write(contacts)

    if changed:
        log.warning(
            "Recovered %d unassigned contact(s) into %s",
            changed,
            fallback,
        )

    signature = (orphaned, tuple(sorted(orphaned_ids))) if orphaned else None
    previous = getattr(server, "_contact_orphan_warning_signature", None)
    if signature != previous:
        server._contact_orphan_warning_signature = signature
        if orphaned:
            log.warning(
                "Preserving %d contact(s) owned by missing workspace(s): %s. "
                "Restore the workspace or explicitly reassign/globalize those contacts.",
                orphaned,
                ", ".join(sorted(orphaned_ids)),
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
        # Re-run the cheap ownership check on reads for newly imported legacy rows,
        # but never adopt a remembered orphan workspace merely because it is absent.
        preferred = str(request.headers.get("X-PBX-Workspace", "") or "")
        repair_contact_ownership(self, preferred)
        return await original_contacts_list(self, request)

    cls.__init__ = init
    cls.contacts_list = contacts_list
    cls._contact_recovery_applied = True
