from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import discord

from appdb import AppDatabase, CAPABILITIES, DEFAULT_ADMIN_CAPS, DEFAULT_OPERATOR_CAPS

log = logging.getLogger("discord-pbx.workspaces")


class WorkspaceService:
    def __init__(self, bot, db: AppDatabase, config):
        self.bot = bot
        self.db = db
        self.config = config
        self._presence_since: dict[str, float] = {}
        self._presence_last_count: dict[str, int] = {}
        self._member_caps_cache: dict[tuple[str, str], tuple[float, set[str], list[str]]] = {}
        self.bootstrap_legacy()

    def bootstrap_legacy(self) -> None:
        if self.db.list_workspaces() or not self.config.guild_id:
            return
        ws = self.db.upsert_workspace({
            "id": f"ws_{self.config.guild_id}",
            "guild_id": str(self.config.guild_id),
            "alias": "Main",
            "voice_channel_id": str(self.config.voice_channel_id or ""),
            "text_channel_id": str(self.config.text_channel_id or ""),
            "priority": 1,
            "enabled": True,
            "accept_inbound": True,
            "allow_outbound": True,
            "auto_route": True,
            "max_calls": self.config.max_simultaneous_calls,
        })
        for role_id in sorted(self.config.pbx_role_ids):
            self.db.replace_role_capabilities(ws["id"], str(role_id), f"Legacy role {role_id}", DEFAULT_ADMIN_CAPS)
        self.db.set_setting("default_workspace_id", ws["id"])
        self.db.set_setting("inbound_routing", {
            "mode": "auto",
            "targets": [],
            "fallback": "default",
            "override_expires": 0,
            "all_occupied": False,
        })
        log.info("Migrated legacy single-guild configuration into workspace %s", ws["id"])

    def _system_admin_ids(self) -> set[int]:
        ids = set(getattr(self.config, "bot_owner_ids", set()))
        saved = self.db.get_setting("system_admin_discord_ids", []) or []
        if isinstance(saved, str):
            saved = [x.strip() for x in saved.replace("\n", ",").split(",") if x.strip()]
        for value in saved if isinstance(saved, (list, tuple, set)) else []:
            try:
                ids.add(int(value))
            except (TypeError, ValueError):
                pass
        return ids

    def default_workspace(self) -> dict[str, Any] | None:
        default_id = str(self.db.get_setting("default_workspace_id", "") or "")
        if default_id:
            ws = self.db.get_workspace(default_id)
            if ws and ws.get("enabled"):
                return ws
        return next((w for w in self.db.list_workspaces() if w.get("enabled")), None)

    def workspace_for_guild(self, guild_id: int | str) -> dict[str, Any] | None:
        return self.db.get_workspace_by_guild(guild_id)

    async def discord_catalog(self) -> list[dict[str, Any]]:
        out = []
        for guild in sorted(getattr(self.bot, "guilds", []), key=lambda g: g.name.lower()):
            voice_channels = [
                {"id": str(c.id), "name": c.name, "type": "voice"}
                for c in guild.voice_channels
            ]
            text_channels = [
                {"id": str(c.id), "name": c.name, "type": "text"}
                for c in guild.text_channels
                if c.permissions_for(guild.me).send_messages if guild.me
            ]
            roles = [
                {"id": str(r.id), "name": r.name, "position": r.position, "managed": r.managed}
                for r in sorted(guild.roles, key=lambda x: x.position, reverse=True)
                if not r.is_default()
            ]
            out.append({
                "guild_id": str(guild.id), "name": guild.name,
                "icon_url": str(guild.icon.url) if guild.icon else "",
                "voice_channels": voice_channels, "text_channels": text_channels, "roles": roles,
            })
        return out

    async def member_capabilities(self, user_id: int | str, workspace_id: str, force: bool = False) -> tuple[set[str], list[str]]:
        user_id_s = str(user_id)
        if user_id_s.isdigit() and int(user_id_s) in self.config.bot_owner_ids:
            return set(CAPABILITIES), []
        user = self.db.get_user(user_id_s)
        if user and user.get("is_system_admin"):
            return set(CAPABILITIES), []
        key = (user_id_s, workspace_id)
        cached = self._member_caps_cache.get(key)
        if cached and not force and time.monotonic() - cached[0] < 20:
            return set(cached[1]), list(cached[2])
        ws = self.db.get_workspace(workspace_id)
        if not ws or not ws.get("enabled"):
            return set(), []
        guild = self.bot.get_guild(int(ws["guild_id"])) if getattr(self.bot, "is_ready", lambda: False)() else None
        if guild is None:
            return set(), []
        member = guild.get_member(int(user_id_s)) if user_id_s.isdigit() else None
        if member is None and user_id_s.isdigit():
            try:
                member = await guild.fetch_member(int(user_id_s))
            except Exception:
                member = None
        if member is None:
            caps, role_ids = set(), []
        else:
            role_ids = [str(r.id) for r in member.roles]
            caps = self.db.capabilities_for_roles(workspace_id, role_ids)
            if member.id == guild.owner_id:
                caps |= set(CAPABILITIES)
        self._member_caps_cache[key] = (time.monotonic(), set(caps), list(role_ids))
        return caps, role_ids

    async def user_workspace_access(self, user_id: int | str) -> list[dict[str, Any]]:
        out = []
        for ws in self.db.list_workspaces(include_roles=False):
            caps, _ = await self.member_capabilities(user_id, ws["id"])
            if "panel_access" in caps or "workspace_admin" in caps or set(CAPABILITIES).issubset(caps):
                item = dict(ws)
                item["capabilities"] = sorted(caps)
                out.append(item)
        return out

    async def has_capability(self, user_id: int | str, workspace_id: str, capability: str) -> bool:
        if capability not in CAPABILITIES:
            return False
        caps, _ = await self.member_capabilities(user_id, workspace_id)
        return capability in caps or "workspace_admin" in caps

    async def eligible_presence(self, workspace_id: str) -> dict[str, Any]:
        ws = self.db.get_workspace(workspace_id)
        result = {
            "workspace_id": workspace_id,
            "eligible_count": 0,
            "eligible": [],
            "occupied": False,
            "stable": False,
            "voice_connected": False,
            "voice_channel_name": "",
        }
        if not ws or not ws.get("enabled") or not ws.get("voice_channel_id"):
            self._presence_since.pop(workspace_id, None)
            return result
        guild = self.bot.get_guild(int(ws["guild_id"])) if self.bot.is_ready() else None
        channel = guild.get_channel(int(ws["voice_channel_id"])) if guild else None
        if not isinstance(channel, discord.VoiceChannel):
            self._presence_since.pop(workspace_id, None)
            return result
        result["voice_channel_name"] = channel.name
        vc = guild.voice_client
        result["voice_connected"] = bool(vc and vc.is_connected())
        eligible = []
        for member in list(channel.members):
            if member.bot:
                continue
            # AFK and self-deafened users are not treated as pickup operators.
            if guild.afk_channel and channel.id == guild.afk_channel.id:
                continue
            if member.voice and member.voice.self_deaf:
                continue
            caps = self.db.capabilities_for_roles(workspace_id, [str(r.id) for r in member.roles])
            if "receive_inbound" in caps or "workspace_admin" in caps or member.id == guild.owner_id or member.id in self._system_admin_ids():
                eligible.append({"id": str(member.id), "name": member.display_name})
        count = len(eligible)
        result["eligible_count"] = count
        result["eligible"] = eligible
        result["occupied"] = count > 0
        previous = self._presence_last_count.get(workspace_id, 0)
        self._presence_last_count[workspace_id] = count
        now = time.monotonic()
        if count > 0:
            if previous <= 0 or workspace_id not in self._presence_since:
                self._presence_since[workspace_id] = now
            grace = float(ws.get("presence_grace_seconds", 4) or 0)
            result["stable"] = (now - self._presence_since.get(workspace_id, now)) >= grace
            result["stable_in_seconds"] = max(0.0, grace - (now - self._presence_since.get(workspace_id, now)))
        else:
            self._presence_since.pop(workspace_id, None)
            result["stable"] = False
        return result

    async def routing_status(self) -> dict[str, Any]:
        cfg = self.db.get_setting("inbound_routing", {}) or {}
        now = time.time()
        override_expires = float(cfg.get("override_expires", 0) or 0)
        if override_expires and override_expires <= now:
            cfg = {**cfg, "mode": "auto", "targets": [], "override_expires": 0}
            self.db.set_setting("inbound_routing", cfg)
        statuses = []
        for ws in self.db.list_workspaces():
            p = await self.eligible_presence(ws["id"])
            statuses.append({**ws, "presence": p})
        selected = await self.resolve_inbound_workspaces(preview_statuses=statuses)
        return {
            "mode": str(cfg.get("mode", "auto")),
            "targets": list(cfg.get("targets", [])),
            "fallback": str(cfg.get("fallback", "default")),
            "override_expires": float(cfg.get("override_expires", 0) or 0),
            "all_occupied": bool(cfg.get("all_occupied", False)),
            "selected": [w["id"] for w in selected],
            "workspaces": statuses,
        }

    async def resolve_inbound_workspaces(self, preview_statuses: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        cfg = self.db.get_setting("inbound_routing", {}) or {}
        mode = str(cfg.get("mode", "auto"))
        targets = [str(x) for x in cfg.get("targets", [])]
        override_expires = float(cfg.get("override_expires", 0) or 0)
        if override_expires and override_expires <= time.time():
            mode, targets = "auto", []
        workspaces = [w for w in self.db.list_workspaces() if w.get("enabled") and w.get("accept_inbound")]
        by_id = {w["id"]: w for w in workspaces}
        if mode in {"off", "dnd", "reject"}:
            return []
        if mode in {"manual", "ring_group"}:
            return [by_id[x] for x in targets if x in by_id]

        # AUTO: choose workspaces with stable eligible operators, by configured priority.
        status_map = {x["id"]: x.get("presence", {}) for x in (preview_statuses or [])}
        occupied: list[dict[str, Any]] = []
        for ws in sorted(workspaces, key=lambda x: (int(x.get("priority", 100)), x.get("alias", ""))):
            if not ws.get("auto_route"):
                continue
            p = status_map.get(ws["id"]) or await self.eligible_presence(ws["id"])
            if p.get("stable"):
                occupied.append(ws)
        if occupied:
            return occupied if bool(cfg.get("all_occupied", False)) else occupied[:1]

        fallback = str(cfg.get("fallback", "default"))
        if fallback == "all":
            return workspaces
        if fallback == "none":
            return []
        default_ws = self.default_workspace()
        if default_ws and default_ws.get("accept_inbound"):
            return [default_ws]
        return workspaces[:1]

    async def decorate_workspaces(self, user_id: str = "") -> list[dict[str, Any]]:
        catalog = {x["guild_id"]: x for x in await self.discord_catalog()}
        out = []
        for ws in self.db.list_workspaces(include_roles=True):
            item = dict(ws)
            guild = catalog.get(str(ws["guild_id"]))
            item["discord"] = guild or {"guild_id": ws["guild_id"], "name": "Bot not connected", "voice_channels": [], "text_channels": [], "roles": []}
            item["presence"] = await self.eligible_presence(ws["id"])
            if user_id:
                caps, role_ids = await self.member_capabilities(user_id, ws["id"])
                item["current_user_capabilities"] = sorted(caps)
                item["current_user_role_ids"] = role_ids
            out.append(item)
        return out
