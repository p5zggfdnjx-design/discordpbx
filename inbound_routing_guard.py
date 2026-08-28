from __future__ import annotations

import logging

from workspace_service import WorkspaceService

log = logging.getLogger("discord-pbx.inbound-routing")


def apply() -> None:
    if getattr(WorkspaceService, "_inbound_route_fallback_guard", False):
        return

    old_resolve = WorkspaceService.resolve_inbound_workspaces

    async def resolve_inbound_workspaces(self, preview_statuses=None):
        selected = await old_resolve(self, preview_statuses=preview_statuses)
        if selected:
            return selected

        cfg = self.db.get_setting("inbound_routing", {}) or {}
        mode = str(cfg.get("mode", "auto") or "auto")
        if mode in {"off", "dnd", "reject"}:
            return []
        if mode not in {"manual", "ring_group"}:
            return selected

        # A deleted/disabled manual target used to produce an empty route even
        # when the routing policy explicitly configured a fallback.
        workspaces = [
            w for w in self.db.list_workspaces()
            if w.get("enabled") and w.get("accept_inbound")
        ]
        fallback = str(cfg.get("fallback", "default") or "default")
        if fallback == "none":
            return []
        if fallback == "all":
            return workspaces

        default_ws = self.default_workspace()
        if default_ws and default_ws.get("enabled") and default_ws.get("accept_inbound"):
            log.warning(
                "Inbound %s route has no valid targets; using default workspace %s",
                mode,
                default_ws.get("id"),
            )
            return [default_ws]
        return workspaces[:1]

    WorkspaceService.resolve_inbound_workspaces = resolve_inbound_workspaces
    WorkspaceService._inbound_route_fallback_guard = True
