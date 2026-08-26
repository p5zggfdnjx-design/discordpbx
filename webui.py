from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import hmac
import html
import io
import json
import logging
import os
import random
import re
import shutil
import socket
import time
import zipfile
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

from appdb import AppDatabase, CAPABILITIES
from auth_service import AuthService, CSRF_HEADER
from backup_manager import BackupManager
from eventbus import EventBus
from webui_legacy import WebControlServer as LegacyWebControlServer

log = logging.getLogger("discord-pbx.web.v3")

WEB_DIR = Path(__file__).with_name("web")
PAGE = (WEB_DIR / "index.html").read_text(encoding="utf-8") if (WEB_DIR / "index.html").exists() else "PBX"
LOGIN_PAGE = (WEB_DIR / "login.html").read_text(encoding="utf-8") if (WEB_DIR / "login.html").exists() else "Login"
SETUP_PAGE = (WEB_DIR / "setup.html").read_text(encoding="utf-8") if (WEB_DIR / "setup.html").exists() else "Setup"

PUBLIC_PATHS = {
    "/", "/login", "/setup", "/manifest.webmanifest", "/sw.js",
    "/auth/discord", "/auth/discord/callback",
    "/api/setup/status", "/api/setup/initialize", "/api/auth/local",
    "/api/pbx/inbound/register",
}

SENSITIVE_PATH_PREFIXES = (
    "/api/system/secrets", "/api/system/update", "/api/setup", "/api/auth/local", "/api/soundboard",
)


class WebControlServer(LegacyWebControlServer):
    """v3 multi-workspace operator console.

    The proven v2.2 telephony workers/stores remain underneath, while v3 adds a
    transactional application database, Discord OAuth/RBAC, multi-guild routing,
    SSE events, health/backup/policy surfaces and attributable audit logging.
    """

    def __init__(self, bot, config, db: AppDatabase, secret_store, workspaces):
        super().__init__(bot, config)
        self.db = db
        self.secret_store = secret_store
        self.workspaces = workspaces
        self.auth = AuthService(bot, config, db, secret_store, workspaces)
        self.events = EventBus()
        self.backups = BackupManager(config.data_dir)
        self.bot.bridge.workspace_provider = workspaces
        self._dial_rate: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=120))
        self._last_status_publish = 0.0
        self._webhook_tasks: set[asyncio.Task] = set()
        self._maintenance_task: asyncio.Task | None = None
        self._startup_ts = time.time()
        self._updates_dir = Path(config.data_dir) / "updates"
        self._updates_dir.mkdir(parents=True, exist_ok=True)
        self._apply_persisted_runtime_settings()

    def _apply_persisted_runtime_settings(self) -> None:
        # Settings saved by Setup/System UI are applied over copied legacy .env.
        mapping = {
            "asterisk_host": "ami_host",
            "asterisk_port": "ami_port",
            "asterisk_user": "ami_user",
            "audiosocket_advertise_host": "audiosocket_advertise_host",
            "asterisk_dial_context": "ami_dial_context",
            "max_simultaneous_calls": "max_simultaneous_calls",
            "public_base_url": "public_base_url",
            "discord_client_id": "discord_client_id",
            "web_auth_mode": "web_auth_mode",
        }
        for setting, attr in mapping.items():
            value = self.db.get_setting(setting, None)
            if value not in (None, ""):
                try:
                    if attr in {"ami_port", "max_simultaneous_calls"}:
                        value = int(value)
                    setattr(self.config, attr, value)
                except Exception:
                    log.warning("Ignoring invalid persisted setting %s", setting)
        # Conference mode is workspace state, not a browser preference. Persist it
        # so status refreshes, reconnects and service restarts cannot silently
        # flip the switch back off.
        for ws in self.db.list_workspaces():
            wid = str(ws.get("id", ""))
            if wid:
                enabled = bool(self.db.get_setting(self._conference_setting_key(wid), False))
                self.bot.bridge.set_workspace_conference_mode(wid, enabled)
        ami_secret = self.secret_store.get("asterisk_ami_secret", self.config.ami_secret)
        if ami_secret:
            self.bot.ami.secret = ami_secret
        self.bot.ami.host = self.config.ami_host
        self.bot.ami.port = self.config.ami_port
        self.bot.ami.username = self.config.ami_user

    # ---------------------- request helpers / auth ----------------------
    def _request_ip(self, request: web.Request) -> str:
        return self.auth._request_ip(request)

    def _ingress_authorized_v3(self, request: web.Request) -> bool:
        if request.path != "/api/pbx/inbound/register":
            return False
        expected = self.secret_store.get("pbx_ingress_token", self.config.pbx_ingress_token)
        supplied = request.query.get("token", "")
        return bool(expected and supplied and hmac.compare_digest(expected, supplied))

    @web.middleware
    async def security_middleware(self, request: web.Request, handler):
        if request.path == "/api/pbx/inbound/register":
            if not self._ingress_authorized_v3(request):
                return web.json_response({"ok": False, "error": "invalid PBX ingress token"}, status=403)
            request["actor"] = {"user_id": "pbx:ingress", "name": "FreePBX", "auth_type": "ingress", "system_admin": False, "capabilities": set()}
            return await handler(request)

        if request.path in PUBLIC_PATHS or request.path.startswith("/assets/"):
            return await handler(request)

        actor = await self.auth.actor_from_request(request)
        if not actor:
            # Preserve v2.x native HTTP Basic access when WEB_AUTH_MODE=basic.
            # This is especially important for upgrades whose legacy password is
            # shorter than the new 12-character break-glass minimum.
            if self.config.web_auth_mode == "basic":
                headers = {"WWW-Authenticate": 'Basic realm="PBX Console", charset="UTF-8"'}
                if request.path.startswith("/api/"):
                    return web.json_response({"ok": False, "error": "authentication required"}, status=401, headers=headers)
                return web.Response(text="Authentication required", status=401, headers=headers)
            if request.path.startswith("/api/"):
                return web.json_response({"ok": False, "error": "authentication required", "login": "/login"}, status=401)
            raise web.HTTPFound("/login")
        if not self.auth.csrf_valid(request, actor):
            return web.json_response({"ok": False, "error": "CSRF token is missing or invalid"}, status=403)
        request["actor"] = actor
        return await handler(request)

    @web.middleware
    async def audit_middleware(self, request: web.Request, handler):
        body_preview: Any = None
        mutating = request.method not in {"GET", "HEAD", "OPTIONS"} and request.path.startswith("/api/")
        if mutating and not any(request.path.startswith(prefix) for prefix in SENSITIVE_PATH_PREFIXES):
            try:
                raw = await request.read()
                if raw and len(raw) < 8192 and "application/json" in request.headers.get("Content-Type", ""):
                    body_preview = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                body_preview = None
        started = time.monotonic()
        response = None
        error_status = 0
        error_text = ""
        try:
            response = await handler(request)
            return response
        except web.HTTPException as exc:
            error_status = int(exc.status)
            error_text = str(exc.reason or exc.text or "")[:300]
            raise
        finally:
            actor = request.get("actor")
            # Audit authenticated mutations and authenticated authorization failures.
            should_log = bool(actor and request.path.startswith("/api/") and request.path != "/api/pbx/inbound/register" and (mutating or error_status in {401, 403}))
            if should_log:
                workspace_id = request.headers.get("X-PBX-Workspace", "")
                status = error_status or int(getattr(response, "status", 200) or 200)
                detail = {"status": status, "ms": round((time.monotonic() - started) * 1000, 1)}
                if error_text:
                    detail["error"] = error_text
                if isinstance(body_preview, dict):
                    safe = {k: v for k, v in body_preview.items() if "secret" not in k.lower() and "password" not in k.lower() and "token" not in k.lower()}
                    detail["request"] = safe
                self.db.audit(
                    f"{request.method} {request.path}", actor_user_id=str(actor.get("user_id", "")), actor_name=str(actor.get("name", "")),
                    auth_type=str(actor.get("auth_type", "")), workspace_id=workspace_id,
                    entity_type="http", entity_id=request.path, detail=detail, ip=self._request_ip(request),
                )

    async def _actor_workspaces(self, actor: dict[str, Any]) -> list[dict[str, Any]]:
        if actor.get("system_admin"):
            out = self.db.list_workspaces()
            for ws in out:
                ws["capabilities"] = sorted(CAPABILITIES)
            return out
        if actor.get("auth_type") == "discord":
            return await self.workspaces.user_workspace_access(actor["user_id"])
        if actor.get("auth_type") == "local_user":
            return self.db.local_user_workspaces(str(actor.get("local_user_id", "")))
        if actor.get("auth_type") == "api":
            wid = str(actor.get("workspace_id", ""))
            ws = self.db.get_workspace(wid) if wid else None
            if ws:
                ws["capabilities"] = sorted(actor.get("capabilities", []))
                return [ws]
        return []

    async def _workspace(self, request: web.Request, capability: str = "panel_access", explicit: str = "", allow_all: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        actor = request.get("actor") or await self.auth.actor_from_request(request)
        if not actor:
            raise web.HTTPUnauthorized(text="authentication required")
        workspace_id = str(explicit or request.headers.get("X-PBX-Workspace", "") or request.query.get("workspace_id", ""))
        if allow_all and workspace_id == "__all__" and actor.get("system_admin"):
            return actor, {"id": "__all__", "alias": "All workspaces"}
        if not workspace_id:
            accessible = await self._actor_workspaces(actor)
            if len(accessible) == 1:
                workspace_id = accessible[0]["id"]
            else:
                default = self.workspaces.default_workspace()
                if default and (actor.get("system_admin") or any(x["id"] == default["id"] for x in accessible)):
                    workspace_id = default["id"]
                elif accessible:
                    workspace_id = accessible[0]["id"]
        ws = self.db.get_workspace(workspace_id) if workspace_id else None
        if not ws:
            raise web.HTTPBadRequest(text="select a valid PBX workspace")
        if capability and not await self.auth.can(actor, workspace_id, capability):
            raise web.HTTPForbidden(text=f"workspace permission required: {capability}")
        return actor, ws

    async def _system_admin(self, request: web.Request) -> dict[str, Any]:
        actor = request.get("actor") or await self.auth.actor_from_request(request)
        if not actor or not actor.get("system_admin"):
            raise web.HTTPForbidden(text="system administrator access required")
        return actor

    async def _call_access(self, request: web.Request, call_uuid: str, capability: str = "bridge") -> tuple[dict[str, Any], str]:
        actor = request.get("actor") or await self.auth.actor_from_request(request)
        if not actor:
            raise web.HTTPUnauthorized()
        session = self.bot.bridge.get_session(call_uuid)
        pending = self.bot.bridge.get_pending(call_uuid)
        row = self.call_history.get_by_uuid(call_uuid)
        ids = list(getattr(session, "workspace_ids", []) if session else (pending or {}).get("workspace_ids", []) or (row or {}).get("workspace_ids", []))
        if actor.get("system_admin"):
            return actor, (request.headers.get("X-PBX-Workspace") or (ids[0] if ids else ""))
        requested = request.headers.get("X-PBX-Workspace", "")
        candidates = [requested] if requested else ids
        for wid in candidates:
            if wid in ids and await self.auth.can(actor, wid, capability):
                return actor, wid
        raise web.HTTPForbidden(text="you do not have permission for this call")

    def _dial_rate_check(self, actor: dict[str, Any], workspace_id: str) -> None:
        policy = self.db.get_setting("dial_rate_limits", {}) or {}
        per_minute = max(1, min(120, int(policy.get("per_user_per_minute", 30) or 30)))
        key = f"{actor.get('user_id')}:{workspace_id}"
        q = self._dial_rate[key]
        now = time.monotonic()
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= per_minute:
            raise ValueError(f"dial rate limit reached ({per_minute}/minute)")
        q.append(now)

    def _calling_policy_check(self, number: str) -> None:
        if self.db.dnc_contains(number):
            raise ValueError("destination is on the PBX do-not-call/block list")
        policy = self.db.get_setting("calling_policy", {}) or {}
        if not bool(policy.get("time_window_enabled", False)):
            return
        tz_name = str(policy.get("timezone", "America/New_York"))
        try:
            now = time.localtime() if tz_name == "local" else __import__("datetime").datetime.now(ZoneInfo(tz_name)).timetuple()
        except Exception:
            now = time.localtime()
        hour = now.tm_hour + now.tm_min / 60.0
        start = float(policy.get("start_hour", 8) or 8)
        end = float(policy.get("end_hour", 21) or 21)
        if start <= end:
            allowed = start <= hour < end
        else:
            allowed = hour >= start or hour < end
        if not allowed:
            raise ValueError("calling is outside the configured allowed-hours window")

    # ---------------------- lifecycle ----------------------
    async def start(self):
        app = web.Application(middlewares=[self.security_middleware, self.audit_middleware], client_max_size=32 * 1024 * 1024)
        app.add_routes([
            web.get("/", self.index), web.get("/login", self.login_page), web.get("/setup", self.setup_page),
            web.get("/manifest.webmanifest", self.manifest), web.get("/sw.js", self.service_worker),
            web.get("/auth/discord", self.discord_login), web.get("/auth/discord/callback", self.discord_callback),
            web.get("/api/setup/status", self.setup_status), web.post("/api/setup/initialize", self.setup_initialize),
            web.post("/api/auth/local", self.local_login), web.post("/api/auth/logout", self.logout), web.get("/api/auth/me", self.auth_me),
            web.get("/api/events", self.event_stream),

            web.get("/api/status", self.status), web.get("/api/stats", self.stats), web.get("/api/health", self.health), web.get("/api/diagnostics", self.diagnostics_v3),
            web.post("/api/self-test/{test}", self.self_test),

            web.get("/api/workspaces", self.workspaces_list), web.get("/api/workspaces/catalog", self.workspace_catalog), web.get("/api/workspaces/invite", self.workspace_invite),
            web.post("/api/workspaces", self.workspace_create), web.put("/api/workspaces/{workspace_id}", self.workspace_update), web.delete("/api/workspaces/{workspace_id}", self.workspace_delete),
            web.put("/api/workspaces/{workspace_id}/roles/{role_id}", self.workspace_role_update), web.delete("/api/workspaces/{workspace_id}/roles/{role_id}", self.workspace_role_delete),
            web.post("/api/workspaces/{workspace_id}/connect", self.workspace_connect), web.post("/api/workspaces/{workspace_id}/disconnect", self.workspace_disconnect),
            web.get("/api/routing", self.routing_get), web.post("/api/routing", self.routing_set),

            web.get("/api/contacts", self.contacts_list), web.post("/api/contacts", self.contacts_create), web.put("/api/contacts/{contact_id}", self.contacts_update), web.delete("/api/contacts/{contact_id}", self.contacts_delete),
            web.post("/api/contacts/reorder", self.contacts_reorder), web.get("/api/contacts.csv", self.contacts_csv), web.post("/api/contacts/import", self.contacts_import),

            web.get("/api/caller-id-pool", self.caller_id_pool_list_v3), web.post("/api/caller-id-pool/preview", self.caller_id_pool_preview_v3), web.post("/api/caller-id-pool/bulk", self.caller_id_pool_bulk_v3),
            web.put("/api/caller-id-pool/{entry_id}", self.caller_id_pool_update_v3), web.delete("/api/caller-id-pool/{entry_id}", self.caller_id_pool_delete_v3), web.get("/api/caller-id-pool.yaml", self.caller_id_pool_yaml_v3),
            web.get("/api/random-call-pool", self.random_call_pool_list_v3), web.post("/api/random-call-pool/preview", self.random_call_pool_preview_v3), web.post("/api/random-call-pool/bulk", self.random_call_pool_bulk_v3),
            web.put("/api/random-call-pool/{entry_id}", self.random_call_pool_update_v3), web.delete("/api/random-call-pool/{entry_id}", self.random_call_pool_delete_v3), web.delete("/api/random-call-pool", self.random_call_pool_clear_v3), web.get("/api/random-call-pool.yaml", self.random_call_pool_yaml_v3),

            web.get("/api/schedules", self.schedules_list), web.post("/api/schedules", self.schedules_create), web.post("/api/schedules/{schedule_id}/cancel", self.schedules_cancel), web.delete("/api/schedules/{schedule_id}", self.schedules_delete),
            web.get("/api/soundboard", self.soundboard_list_v3), web.post("/api/soundboard/{slot}", self.soundboard_save_v3), web.delete("/api/soundboard/{slot}", self.soundboard_delete_v3), web.post("/api/soundboard/{slot}/play", self.soundboard_play_v3),

            web.get("/api/pbx/inbound/register", self.inbound_register), web.post("/api/join", self.join_v3), web.post("/api/leave", self.leave_v3), web.post("/api/pbx/ping", self.pbx_ping_v3),
            web.post("/api/dial", self.dial), web.post("/api/dial-random", self.dial_random), web.post("/api/outbound/{uuid}/cancel", self.cancel_outbound),
            web.post("/api/call/{uuid}/claim", self.call_claim), web.post("/api/call/{uuid}/routes", self.call_routes), web.post("/api/call/{uuid}/solo-talk", self.solo_talk), web.post("/api/call/{uuid}/focus", self.focus),
            web.post("/api/call/{uuid}/dtmf", self.call_dtmf), web.post("/api/call/{uuid}/auto-redial", self.call_auto_redial), web.put("/api/call/{uuid}/notes", self.call_notes),
            web.post("/api/call/{uuid}/workspaces", self.call_workspaces), web.post("/api/call/{uuid}/hold", self.call_hold), web.post("/api/call/{uuid}/park", self.call_park), web.post("/api/park/{slot}/retrieve", self.call_unpark),
            web.post("/api/call/{uuid}/transfer", self.call_transfer), web.post("/api/call/{uuid}/split", self.call_split), web.post("/api/calls/conference", self.calls_conference), web.post("/api/call/{uuid}/hangup", self.call_hangup),
            web.post("/api/calls/talk", self.all_talk), web.post("/api/calls/listen", self.all_listen), web.post("/api/operator/audio", self.operator_audio), web.post("/api/calls/all-routes", self.all_routes), web.post("/api/calls/hangup-all", self.hangup_all), web.post("/api/calls/conference-mode", self.workspace_conference_mode),
            web.post("/api/operator/ringback", self.ringback_setting), web.post("/api/operator/voicemail-detection", self.voicemail_detection_setting),
            web.get("/api/operator/preferences", self.operator_preferences), web.post("/api/operator/preferences", self.operator_preferences_update),

            web.get("/api/history", self.history_list), web.get("/api/history/{uuid}/timeline", self.history_timeline), web.put("/api/history/{row_id}", self.history_update),
            web.get("/api/audit", self.audit_list), web.get("/api/audit/verify", self.audit_verify),
            web.get("/api/policies", self.policies_get), web.post("/api/policies", self.policies_set), web.get("/api/dnc", self.dnc_list), web.post("/api/dnc", self.dnc_add), web.delete("/api/dnc/{number}", self.dnc_delete),

            web.get("/api/system/settings", self.system_settings), web.post("/api/system/settings", self.system_settings_update), web.get("/api/system/oauth-status", self.system_oauth_status), web.get("/api/system/secrets", self.system_secrets_status), web.post("/api/system/secrets", self.system_secrets_update),
            web.get("/api/system/local-admin", self.system_local_admin_status), web.post("/api/system/local-admin", self.system_local_admin_update),
            web.get("/api/system/local-users", self.system_local_users_list), web.post("/api/system/local-users", self.system_local_user_save),
            web.put("/api/system/local-users/{user_id}", self.system_local_user_update), web.delete("/api/system/local-users/{user_id}", self.system_local_user_delete),
            web.get("/api/system/revisions", self.revisions_list), web.post("/api/system/revisions/{revision_id}/restore", self.revision_restore), web.post("/api/system/restart", self.system_restart),
            web.get("/api/system/backups", self.backups_list), web.post("/api/system/backups", self.backup_create), web.get("/api/system/backups/{name}", self.backup_download), web.post("/api/system/backups/{name}/restore", self.backup_restore),
            web.get("/api/system/update/status", self.system_update_status), web.post("/api/system/update/upload", self.system_update_upload), web.post("/api/system/update/apply", self.system_update_apply),
            web.get("/api/system/update/github", self.system_update_github_status), web.post("/api/system/update/github/stage", self.system_update_github_stage), web.post("/api/system/update/github/install", self.system_update_github_install),
            web.get("/api/system/api-tokens", self.api_tokens_list), web.post("/api/system/api-tokens", self.api_token_create), web.delete("/api/system/api-tokens/{token_id}", self.api_token_revoke),
            web.get("/api/system/webhooks", self.webhooks_list), web.post("/api/system/webhooks", self.webhook_save), web.delete("/api/system/webhooks/{webhook_id}", self.webhook_delete),
        ])
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.config.web_bind, self.config.web_port)
        await self.site.start()
        self.scheduler_task = asyncio.create_task(self._scheduler_loop(), name="scheduled-call-worker")
        self._maintenance_task = asyncio.create_task(self._maintenance_loop(), name="pbx-maintenance")
        try:
            snap = await asyncio.to_thread(self.backups.startup_snapshot_once, self.config.version)
            if snap:
                log.info("Created v3 startup safety backup %s", snap.name)
        except Exception:
            log.exception("Startup backup failed")
        log.info("PBX %s web console listening on http://%s:%s", self.config.version, self.config.web_bind, self.config.web_port)

    async def close(self):
        if self._maintenance_task:
            self._maintenance_task.cancel()
        for task in list(self._webhook_tasks):
            task.cancel()
        await super().close()

    async def _maintenance_loop(self):
        while True:
            try:
                await asyncio.sleep(300)
                retention = self.db.get_setting("retention", {}) or {}
                await asyncio.to_thread(
                    self.call_history.prune,
                    int(retention.get("call_days", 365) or 365),
                    int(retention.get("event_days", 365) or 365),
                    int(retention.get("activity_days", 90) or 90),
                )
                await self.events.publish("health.tick", {"ts": time.time()})
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Maintenance loop failed")

    # ---------------------- HTML / auth / setup ----------------------
    async def index(self, request):
        # The public root is the convenient entry point: unauthenticated users go
        # directly to the login page instead of briefly loading the operator UI.
        if not self.db.get_setting("system_initialized", False):
            raise web.HTTPFound("/setup")
        if self.config.web_auth_mode != "none" and not await self.auth.actor_from_request(request):
            raise web.HTTPFound("/login")
        return web.Response(text=PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def login_page(self, request):
        if await self.auth.actor_from_request(request):
            raise web.HTTPFound("/")
        return web.Response(text=LOGIN_PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def setup_page(self, request):
        if self.db.get_setting("system_initialized", False):
            actor = await self.auth.actor_from_request(request)
            if not actor or not actor.get("system_admin"):
                raise web.HTTPFound("/login")
        return web.Response(text=SETUP_PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def manifest(self, request):
        return web.json_response({"name": "SkypePBX // Matrix Operator Console", "short_name": "SkypePBX", "start_url": "/", "display": "standalone", "background_color": "#020706", "theme_color": "#00aff0", "icons": []})

    async def service_worker(self, request):
        js = "self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));self.addEventListener('fetch',()=>{});"
        return web.Response(text=js, content_type="application/javascript", headers={"Cache-Control": "no-cache"})

    async def setup_status(self, request):
        initialized = bool(self.db.get_setting("system_initialized", False))
        return web.json_response({
            "ok": True, "initialized": initialized, "version": self.config.version,
            "local_admin_configured": self.db.local_admin_configured(),
            "discord_token_stored": self.secret_store.has("discord_bot_token") or bool(self.config.discord_token),
            "oauth_client_id": self.auth.client_id(), "public_base_url": self.auth.public_base_url(request),
            "ami_configured": bool(self.config.ami_host and self.config.ami_user and (self.secret_store.has("asterisk_ami_secret") or self.config.ami_secret)),
        })

    async def setup_initialize(self, request):
        if self.db.get_setting("system_initialized", False):
            return web.json_response({"ok": False, "error": "setup has already been completed"}, status=409)
        body = await request.json()
        if not self.auth.setup_code_valid(str(body.get("setup_code", ""))):
            return web.json_response({"ok": False, "error": "invalid setup code"}, status=403)
        try:
            username = str(body.get("admin_username", "admin")).strip() or "admin"
            password = str(body.get("admin_password", ""))
            self.db.set_local_admin(username, password)
            for key, secret_key in (
                ("discord_token", "discord_bot_token"), ("discord_client_secret", "discord_oauth_client_secret"),
                ("ami_secret", "asterisk_ami_secret"), ("pbx_ingress_token", "pbx_ingress_token"),
            ):
                value = str(body.get(key, "")).strip()
                if value:
                    self.secret_store.set(secret_key, value)
            admin_id = str(body.get("system_admin_discord_id", "")).strip()
            if admin_id and not admin_id.isdigit():
                raise ValueError("system administrator Discord User ID must be numeric")
            settings = {
                "discord_client_id": str(body.get("discord_client_id", "")).strip(),
                "system_admin_discord_ids": [admin_id] if admin_id else [],
                "public_base_url": str(body.get("public_base_url", "")).strip().rstrip("/"),
                "asterisk_host": str(body.get("ami_host", self.config.ami_host)).strip(),
                "asterisk_port": int(body.get("ami_port", self.config.ami_port) or 5038),
                "asterisk_user": str(body.get("ami_user", self.config.ami_user)).strip(),
                "audiosocket_advertise_host": str(body.get("audiosocket_advertise_host", self.config.audiosocket_advertise_host)).strip(),
                "asterisk_dial_context": str(body.get("ami_dial_context", self.config.ami_dial_context)).strip() or "from-internal",
                "max_simultaneous_calls": max(1, min(100, int(body.get("max_simultaneous_calls", self.config.max_simultaneous_calls) or 15))),
            }
            for key, value in settings.items():
                if value not in (None, ""):
                    self.db.set_setting(key, value)
            guild_id = str(body.get("guild_id", "")).strip()
            if guild_id:
                ws = self.db.upsert_workspace({
                    "guild_id": guild_id, "alias": str(body.get("workspace_alias", "Main"))[:80] or "Main",
                    "voice_channel_id": str(body.get("voice_channel_id", "")).strip(), "text_channel_id": str(body.get("text_channel_id", "")).strip(),
                    "priority": 1, "max_calls": settings["max_simultaneous_calls"], "enabled": True, "accept_inbound": True, "allow_outbound": True, "auto_route": True,
                })
                self.db.set_setting("default_workspace_id", ws["id"])
            self.db.set_setting("inbound_routing", {"mode": "auto", "targets": [], "fallback": "default", "override_expires": 0, "all_occupied": False})
            self.auth.mark_initialized()
            session = self.auth.create_local_session(request, username, password)
            response = web.json_response({"ok": True, "message": "Setup saved. Restart the service to load Discord/PBX credentials.", "restart_required": True})
            if session:
                self.auth.set_session_cookie(response, session, request)
            self.db.audit("setup completed", actor_user_id="local:admin", actor_name=username, auth_type="setup", detail={"workspace_created": bool(guild_id)}, ip=self._request_ip(request))
            return response
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def local_login(self, request):
        body = await request.json()
        username=str(body.get("username", "")); password=str(body.get("password", ""))
        session = self.auth.create_local_session(request, username, password)
        auth_type = "local"
        if not session:
            session = self.auth.create_local_user_session(request, username, password)
            auth_type = "local_user"
        if not session:
            self.db.audit("login.failed", actor_user_id="local:unknown", actor_name=username[:120], auth_type="local", detail={"reason":"invalid credentials"}, ip=self._request_ip(request))
            return web.json_response({"ok": False, "error": "invalid username or password"}, status=403)
        response = web.json_response({"ok": True, "message": "Signed in.", "auth_type": auth_type})
        self.auth.set_session_cookie(response, session, request)
        return response

    async def discord_login(self, request):
        try:
            return_to = str(request.query.get("return", "/"))
            raise web.HTTPFound(self.auth.discord_login_url(request, return_to))
        except RuntimeError as exc:
            return web.Response(text=f"Discord login unavailable: {html.escape(str(exc))}", status=503)

    async def discord_callback(self, request):
        state = str(request.query.get("state", "")); code = str(request.query.get("code", ""))
        return_to = self.db.consume_oauth_state(state)
        if return_to is None or not code:
            return web.Response(text="Discord OAuth state/code is invalid or expired.", status=400)
        try:
            user = await self.auth.exchange_discord_code(request, code)
            session = await self.auth.create_discord_session(request, user)
            response = web.HTTPFound(return_to if return_to.startswith("/") else "/")
            self.auth.set_session_cookie(response, session, request)
            raise response
        except PermissionError as exc:
            return web.Response(text=html.escape(str(exc)), status=403)
        except web.HTTPException:
            raise
        except Exception as exc:
            log.exception("Discord OAuth callback failed")
            return web.Response(text=f"Discord login failed: {html.escape(str(exc))}", status=502)

    async def logout(self, request):
        actor = request.get("actor")
        if actor and actor.get("session_id"):
            self.db.delete_session(actor["session_id"])
        response = web.json_response({"ok": True})
        response.del_cookie("pbx_session", path="/"); response.del_cookie("pbx_csrf", path="/")
        return response

    async def auth_me(self, request):
        actor = request["actor"]
        return web.json_response(await self.auth.me_payload(actor))

    # ---------------------- realtime / webhooks ----------------------
    async def event_stream(self, request):
        response = web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream", "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive", "X-Accel-Buffering": "no",
        })
        await response.prepare(request)
        q = self.events.subscribe()
        try:
            await response.write(b"event: ready\ndata: {\"ok\":true}\n\n")
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=20)
                    await response.write(self.events.sse(item))
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.events.unsubscribe(q)
        return response

    async def _publish(self, event: str, payload: dict[str, Any]):
        await self.events.publish(event, payload)
        for hook in self.db.list_webhooks():
            if not hook.get("enabled"):
                continue
            configured = set(hook.get("events", []))
            if configured and event not in configured and "*" not in configured:
                continue
            workspace_id = str(payload.get("workspace_id", "") or (payload.get("workspace_ids") or [""])[0])
            if hook.get("workspace_id") and hook.get("workspace_id") != workspace_id:
                continue
            task = asyncio.create_task(self._deliver_webhook(hook, event, payload), name=f"webhook-{hook['id']}")
            self._webhook_tasks.add(task); task.add_done_callback(self._webhook_tasks.discard)

    async def _deliver_webhook(self, hook: dict[str, Any], event: str, payload: dict[str, Any]):
        body = json.dumps({"event": event, "timestamp": time.time(), "data": payload}, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "User-Agent": f"DiscordPBX/{self.config.version}"}
        if hook.get("secret"):
            headers["X-PBX-Signature"] = "sha256=" + hmac.new(str(hook["secret"]).encode(), body, hashlib.sha256).hexdigest()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
                async with session.post(hook["url"], data=body, headers=headers) as resp:
                    await resp.read()
                    if resp.status >= 400:
                        log.warning("Webhook %s returned %s", hook["id"], resp.status)
        except Exception:
            log.exception("Webhook %s delivery failed", hook["id"])

    async def _bridge_event(self, event: str, payload: dict) -> None:
        await super()._bridge_event(event, payload)
        uid = str(payload.get("uuid", ""))
        ws_ids = [str(x) for x in payload.get("workspace_ids", [])]
        if uid and ws_ids:
            self.call_history.set_workspaces(uid, ws_ids)
        if uid and payload.get("operator_user_id"):
            # start_call already records this for web calls; this fills slash-command calls.
            row = self.call_history.get_by_uuid(uid)
            if not row:
                self.call_history.start_call(uuid=uid, direction=str(payload.get("direction", "")), number=str(payload.get("number", "")), caller_id=str(payload.get("caller_id", "")), contact_name=str(payload.get("contact_name", "")), source=str(payload.get("source", "")), workspace_ids=ws_ids, operator_user_id=str(payload.get("operator_user_id", "")), operator_name=str(payload.get("operator_name", "")))
        if event == "ended" and uid:
            self.db.remove_parked_call(uid)
        self.db.audit(
            f"call.{event}", actor_user_id=str(payload.get("operator_user_id", "system")), actor_name=str(payload.get("operator_name", "PBX")),
            auth_type="system", workspace_id=(ws_ids[0] if ws_ids else ""), entity_type="call", entity_id=uid,
            call_uuid=uid, number=str(payload.get("number", "")), detail={k: v for k, v in payload.items() if k not in {"operator_user_id", "operator_name"}},
        )
        await self._publish(f"call.{event}", payload)


    # ---------------------- v3 state / workspace surfaces ----------------------
    async def status(self, request):
        actor = request["actor"]
        accessible = await self._actor_workspaces(actor)
        allowed_ids = {x["id"] for x in accessible}
        requested_wid = str(request.headers.get("X-PBX-Workspace", "") or "")
        selected_wid = requested_wid if requested_wid in allowed_ids else (accessible[0]["id"] if len(accessible) == 1 else "")
        data = self.bot.bridge.status_dict()
        # Drain no-answer records without allowing a stale pending call to linger.
        for timed_out in self.bot.bridge.drain_pending_timeouts():
            uid = str(timed_out.get("uuid", ""))
            if uid:
                self.call_history.fail(uid, outcome="no answer", diagnostic=self._sanitize_detail(timed_out.get("detail", "")))
        def visible(item):
            ids = set(str(x) for x in item.get("workspace_ids", []))
            if selected_wid:
                return selected_wid in ids
            return bool(not ids or ids.intersection(allowed_ids))
        data["calls"] = [self._decorate_contact(x) for x in data.get("calls", []) if visible(x)]
        data["outbound_pending"] = [x for x in data.get("outbound_pending", []) if visible(x)]
        data["call_count"] = len(data["calls"])
        data["outbound_pending_count"] = len(data["outbound_pending"])
        for item in data["calls"]:
            uid = str(item.get("uuid", ""))
            row = self.call_history.get_by_uuid(uid) or {}
            item["notes"] = row.get("notes", "")
            item["disposition"] = row.get("disposition", "")
            item["answered_by_name"] = row.get("answered_by_name", "")
            item["auto_redial"] = dict(self._auto_redial.get(uid, {}))
        for item in data["outbound_pending"]:
            item["auto_redial"] = dict(self._auto_redial.get(str(item.get("uuid", "")), {}))
        cid_total, cid_enabled = self.caller_id_pool.counts()
        target_total, target_enabled = self.random_call_pool.counts()
        if selected_wid:
            self.bot.bridge.set_workspace_conference_mode(selected_wid, self._conference_mode_get(selected_wid))
        data.update({
            "version": self.config.version,
            "ami_configured": self.bot.ami.configured,
            "max_simultaneous_calls": int(self.config.max_simultaneous_calls),
            "caller_id_pool_count": cid_total,
            "caller_id_pool_enabled_count": cid_enabled,
            "random_caller_id_available": cid_enabled > 0,
            "random_call_pool_count": target_total,
            "random_call_pool_enabled_count": target_enabled,
            "voicemail_detection_enabled": bool(self._voicemail_detection_enabled),
            "voicemail_detection_supported": True,
            "parked_calls": self.db.list_parked(),
            "workspaces": accessible,
            "routing": await self.workspaces.routing_status(),
            "stats": self.call_history.stats(selected_wid),
            "selected_workspace_id": selected_wid,
            "conference_callers_enabled": bool(selected_wid and self.bot.bridge.workspace_conference_enabled(selected_wid)),
            "conference": self.bot.bridge.conference_diagnostics(selected_wid) if selected_wid else {"enabled": False, "eligible_calls": 0, "routed_frames": 0, "last_routed_seconds_ago": None},
            "conference_diagnostics": self.bot.bridge.conference_diagnostics(selected_wid) if selected_wid else {"enabled": False, "eligible_calls": 0, "routed_frames": 0, "last_routed_seconds_ago": None},
            "voice_mode": "on_demand",
            "voice_idle_disconnect_seconds": self.config.leave_voice_after_call_seconds,
        })
        if selected_wid:
            voice = next((x for x in data.get("discord_workspaces", []) if x.get("id") == selected_wid), None) or {}
            data["discord_connected"] = bool(voice.get("connected"))
            data["discord_channel"] = voice.get("channel")
        return web.json_response(data)

    async def stats(self, request):
        actor, ws = await self._workspace(request, "history", allow_all=True)
        wid = "" if ws["id"] == "__all__" else ws["id"]
        return web.json_response({"ok": True, **self.call_history.stats(wid)})

    async def health(self, request):
        actor = request["actor"]
        workspaces = await self._actor_workspaces(actor)
        bridge_status = self.bot.bridge.status_dict()
        ami_ok = False; ami_detail = "not configured"
        if self.bot.ami.configured:
            try:
                ami_ok, ami_detail = await asyncio.wait_for(asyncio.to_thread(self.bot.ami.ping), timeout=max(3.0, self.config.ami_timeout + 1))
            except Exception as exc:
                ami_detail = str(exc)
        voices = {x.get("id"): x for x in bridge_status.get("discord_workspaces", [])}
        return web.json_response({
            "ok": True, "version": self.config.version, "uptime_seconds": round(time.time() - self._startup_ts, 1),
            "discord_gateway": bool(self.bot.is_ready()), "ami": {"ok": bool(ami_ok), "detail": self._sanitize_detail(ami_detail)},
            "audiosocket": {"ok": bool(getattr(self.bot, "audio_server", None)), "listen": f"{self.config.audiosocket_bind}:{self.config.audiosocket_port}"},
            "database": {"ok": True, "path": str(self.db.path)},
            "workspaces": [{"id": w["id"], "alias": w["alias"], "voice": voices.get(w["id"], {})} for w in workspaces],
            "active_calls": bridge_status.get("call_count", 0), "pending_calls": bridge_status.get("outbound_pending_count", 0),
        })

    async def diagnostics_v3(self, request):
        actor = request["actor"]
        health_resp = await self.health(request)
        health_data = json.loads(health_resp.text)
        return web.json_response({
            "ok": True, "health": health_data,
            "routing": await self.workspaces.routing_status(),
            "audit_chain": self.db.verify_audit_chain(limit=10000),
            "secrets": self.secret_store.status() if actor.get("system_admin") else {},
            "config": {
                "max_calls": self.config.max_simultaneous_calls,
                "dial_context": self.config.ami_dial_context,
                "audiosocket_advertise_host": self.config.audiosocket_advertise_host,
                "public_base_url": self.auth.public_base_url(request),
            } if actor.get("system_admin") else {},
        })

    async def self_test(self, request):
        test = request.match_info["test"]
        actor = request["actor"]
        if test == "ami":
            await self._system_admin(request)
            ok, detail = await asyncio.to_thread(self.bot.ami.ping)
            return web.json_response({"ok": ok, "detail": self._sanitize_detail(detail)}, status=200 if ok else 502)
        if test == "discord":
            return web.json_response({"ok": bool(self.bot.is_ready()), "guilds": len(getattr(self.bot, "guilds", [])), "detail": "Discord gateway connected" if self.bot.is_ready() else "Discord gateway is offline"}, status=200 if self.bot.is_ready() else 503)
        if test == "voice":
            _, ws = await self._workspace(request, "workspace_admin")
            try:
                vc = await asyncio.wait_for(self.bot.bridge.ensure_voice(ws["id"]), timeout=12)
                self.bot.bridge.schedule_voice_idle_disconnect(ws["id"])
                return web.json_response({"ok": True, "detail": f"Connected to {vc.channel.name}; on-demand voice will disconnect when idle"})
            except Exception as exc:
                return web.json_response({"ok": False, "detail": str(exc)}, status=502)
        if test == "database":
            self.db.get_setting("system_initialized", False)
            return web.json_response({"ok": True, "detail": "Database readable/writable"})
        return web.json_response({"ok": False, "error": "unknown self-test"}, status=404)

    async def workspaces_list(self, request):
        actor = request["actor"]
        if actor.get("system_admin"):
            rows = await self.workspaces.decorate_workspaces(actor.get("user_id", "") if actor.get("auth_type") == "discord" else "")
            for row in rows:
                row["current_user_capabilities"] = sorted(CAPABILITIES)
        else:
            allowed = {x["id"]: x for x in await self._actor_workspaces(actor)}
            decorated = await self.workspaces.decorate_workspaces(actor.get("user_id", ""))
            rows = [x for x in decorated if x["id"] in allowed]
        return web.json_response({"ok": True, "workspaces": rows, "capabilities": sorted(CAPABILITIES)})

    async def workspace_catalog(self, request):
        await self._system_admin(request)
        return web.json_response({"ok": True, "guilds": await self.workspaces.discord_catalog()})

    async def workspace_invite(self, request):
        await self._system_admin(request)
        try:
            return web.json_response({"ok": True, "url": self.auth.bot_invite_url()})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    def _workspace_body(self, body: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
        current = existing or {}
        return {
            "id": current.get("id") or body.get("id"),
            "guild_id": str(body.get("guild_id", current.get("guild_id", ""))).strip(),
            "alias": str(body.get("alias", current.get("alias", "Workspace"))).strip()[:80] or "Workspace",
            "voice_channel_id": str(body.get("voice_channel_id", current.get("voice_channel_id", ""))).strip(),
            "text_channel_id": str(body.get("text_channel_id", current.get("text_channel_id", ""))).strip(),
            "enabled": bool(body.get("enabled", current.get("enabled", True))),
            "accept_inbound": bool(body.get("accept_inbound", current.get("accept_inbound", True))),
            "allow_outbound": bool(body.get("allow_outbound", current.get("allow_outbound", True))),
            "auto_route": bool(body.get("auto_route", current.get("auto_route", True))),
            "priority": int(body.get("priority", current.get("priority", 100)) or 100),
            "max_calls": int(body.get("max_calls", current.get("max_calls", self.config.max_simultaneous_calls)) or self.config.max_simultaneous_calls),
            "presence_grace_seconds": float(body.get("presence_grace_seconds", current.get("presence_grace_seconds", 4)) or 0),
            "ring_mode": str(body.get("ring_mode", current.get("ring_mode", "auto")))[:30],
        }

    async def workspace_create(self, request):
        actor = await self._system_admin(request)
        body = await request.json()
        try:
            self.db.save_revision("before workspace create", actor["user_id"], actor["name"])
            ws = self.db.upsert_workspace(self._workspace_body(body))
            self.workspaces.invalidate_cache() if hasattr(self.workspaces, "invalidate_cache") else None
            await self._publish("workspace.changed", {"workspace_id": ws["id"], "action": "created"})
            return web.json_response({"ok": True, "workspace": ws})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def workspace_update(self, request):
        wid = request.match_info["workspace_id"]
        actor, ws = await self._workspace(request, "workspace_admin", explicit=wid)
        body = await request.json()
        try:
            self.db.save_revision(f"before workspace {ws['alias']} update", actor["user_id"], actor["name"])
            updated = self.db.upsert_workspace(self._workspace_body(body, ws))
            await self._publish("workspace.changed", {"workspace_id": wid, "action": "updated"})
            return web.json_response({"ok": True, "workspace": updated})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def workspace_delete(self, request):
        await self._system_admin(request)
        wid = request.match_info["workspace_id"]
        await self.bot.bridge.disconnect_voice(wid)
        ok = self.db.delete_workspace(wid)
        if ok and str(self.db.get_setting("default_workspace_id", "") or "") == wid:
            replacement = next((x for x in self.db.list_workspaces() if x.get("enabled")), None)
            self.db.set_setting("default_workspace_id", replacement["id"] if replacement else "")
        # Remove deleted workspaces from any persistent inbound target list.
        route = self.db.get_setting("inbound_routing", {}) or {}
        if wid in set(str(x) for x in route.get("targets", [])):
            route["targets"] = [str(x) for x in route.get("targets", []) if str(x) != wid]
            if route.get("mode") in {"manual", "ring_group"} and not route["targets"]:
                route["mode"] = "auto"
            self.db.set_setting("inbound_routing", route)
        await self._publish("workspace.changed", {"workspace_id": wid, "action": "deleted"})
        return web.json_response({"ok": ok})

    async def workspace_role_update(self, request):
        wid = request.match_info["workspace_id"]; role_id = request.match_info["role_id"]
        await self._workspace(request, "workspace_admin", explicit=wid)
        body = await request.json()
        caps = [x for x in body.get("capabilities", []) if x in CAPABILITIES]
        self.db.replace_role_capabilities(wid, role_id, str(body.get("role_name", role_id)), caps)
        await self._publish("workspace.roles", {"workspace_id": wid, "role_id": role_id, "capabilities": caps})
        return web.json_response({"ok": True, "roles": self.db.list_workspace_roles(wid)})

    async def workspace_role_delete(self, request):
        wid = request.match_info["workspace_id"]
        await self._workspace(request, "workspace_admin", explicit=wid)
        ok = self.db.remove_workspace_role(wid, request.match_info["role_id"])
        return web.json_response({"ok": ok, "roles": self.db.list_workspace_roles(wid)})

    async def workspace_connect(self, request):
        _, ws = await self._workspace(request, "workspace_admin", explicit=request.match_info["workspace_id"])
        try:
            vc = await self.bot.bridge.ensure_voice(ws["id"])
            self.bot.bridge.schedule_voice_idle_disconnect(ws["id"])
            return web.json_response({"ok": True, "message": f"Connected to {vc.channel.name} for testing; on-demand voice will disconnect when idle."})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=502)

    async def workspace_disconnect(self, request):
        _, ws = await self._workspace(request, "workspace_admin", explicit=request.match_info["workspace_id"])
        await self.bot.bridge.disconnect_voice(ws["id"])
        return web.json_response({"ok": True})

    async def routing_get(self, request):
        await self._workspace(request, "panel_access")
        return web.json_response({"ok": True, "routing": await self.workspaces.routing_status()})

    async def routing_set(self, request):
        actor = request["actor"]
        # Global DID routing is intentionally stronger than workspace receive permission.
        if not actor.get("system_admin"):
            accessible = await self._actor_workspaces(actor)
            if not any("routing" in set(x.get("capabilities", [])) for x in accessible):
                raise web.HTTPForbidden(text="global inbound-routing permission required")
        body = await request.json()
        mode = str(body.get("mode", "auto")).lower()
        if mode not in {"auto", "manual", "ring_group", "dnd", "off", "reject"}:
            return web.json_response({"ok": False, "error": "invalid routing mode"}, status=400)
        valid = {x["id"] for x in self.db.list_workspaces() if x.get("enabled")}
        targets = list(dict.fromkeys(str(x) for x in body.get("targets", []) if str(x) in valid))
        if mode in {"manual", "ring_group"} and not targets:
            return web.json_response({"ok": False, "error": "select at least one enabled inbound workspace"}, status=400)
        try:
            expires = max(0.0, float(body.get("override_expires", 0) or 0))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "invalid routing expiration"}, status=400)
        fallback = str(body.get("fallback", "default")).lower()
        if fallback not in {"default", "all", "none"}:
            fallback = "default"
        cfg = {"mode": mode, "targets": targets, "fallback": fallback, "all_occupied": bool(body.get("all_occupied", False)), "override_expires": expires}
        self.db.save_revision("before inbound routing change", actor.get("user_id", ""), actor.get("name", ""))
        self.db.set_setting("inbound_routing", cfg)
        await self._publish("routing.changed", cfg)
        return web.json_response({"ok": True, "routing": await self.workspaces.routing_status()})

    # ---------------------- workspace data ----------------------
    async def contacts_list(self, request):
        actor = request["actor"]
        if request.query.get("scope") == "mine":
            accessible = await self._actor_workspaces(actor)
            allowed = {w["id"]: w.get("alias", w["id"]) for w in accessible}
            rows = []
            seen_global = set()
            for item in self.contacts.list("", include_global=True):
                scope = str(item.get("scope", "workspace"))
                wid = str(item.get("workspace_id", ""))
                if scope == "global":
                    key = str(item.get("id", ""))
                    if key in seen_global:
                        continue
                    seen_global.add(key)
                    row = self._decorate_contact(item); row["workspace_alias"] = "Global"; rows.append(row)
                elif wid in allowed:
                    row = self._decorate_contact(item); row["workspace_alias"] = allowed[wid]; rows.append(row)
            rows.sort(key=lambda x: (str(x.get("name", "")).lower(), str(x.get("workspace_alias", "")).lower()))
            return web.json_response({"ok": True, "contacts": rows, "scope": "mine"})
        _, ws = await self._workspace(request, "panel_access")
        rows = [self._decorate_contact(x) for x in self.contacts.list(ws["id"], include_global=True)]
        for row in rows:
            row["workspace_alias"] = "Global" if row.get("scope") == "global" else ws.get("alias", ws["id"])
        return web.json_response({"ok": True, "contacts": rows, "scope": "workspace"})

    @staticmethod
    def _contact_bool(value, default=False):
        if value is None:
            return bool(default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on", "enabled"}:
            return True
        if text in {"0", "false", "no", "off", "disabled", ""}:
            return False
        return bool(default)

    def _contact_values_v3(self, body, wsid, existing=None):
        existing = existing or {}
        name = str(body.get("name", existing.get("name", ""))).strip()
        if not name: raise ValueError("contact name is required")
        number = self.bot.ami.normalize_number(str(body.get("number", existing.get("number", ""))))
        scope = str(body.get("scope", existing.get("scope", "workspace"))).lower()
        if scope not in {"workspace", "global"}: scope = "workspace"
        tags = body.get("tags", existing.get("tags", []))
        if isinstance(tags, str): tags = [x.strip() for x in tags.split(",") if x.strip()]
        return dict(name=name[:120], number=number, group=str(body.get("group", existing.get("group", "")))[:80], notes=str(body.get("notes", existing.get("notes", "")))[:2000], favorite=self._contact_bool(body.get("favorite"), existing.get("favorite", False)), bypass_voicemail_detection=self._contact_bool(body.get("bypass_voicemail_detection"), existing.get("bypass_voicemail_detection", False)), workspace_id=wsid, scope=scope, tags=list(tags or [])[:20])

    async def contacts_create(self, request):
        actor, ws = await self._workspace(request, "contacts")
        try:
            values = self._contact_values_v3(await request.json(), ws["id"])
            if values.get("scope") == "global" and not actor.get("system_admin"):
                raise PermissionError("only the system administrator can create global contacts")
            item = self.contacts.create(**values)
            return web.json_response({"ok": True, "contact": self._decorate_contact(item)}, status=201)
        except Exception as exc: return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def contacts_update(self, request):
        actor, ws = await self._workspace(request, "contacts")
        current = self.contacts.get(request.match_info["contact_id"])
        if not current: return web.json_response({"ok": False, "error": "contact not found"}, status=404)
        if current.get("scope") == "workspace" and current.get("workspace_id") not in {"", ws["id"]}: raise web.HTTPForbidden()
        if current.get("scope") == "global" and not actor.get("system_admin"): raise web.HTTPForbidden(text="only the system administrator can edit global contacts")
        try:
            values = self._contact_values_v3(await request.json(), ws["id"], current)
            if values.get("scope") == "global" and not actor.get("system_admin"):
                raise web.HTTPForbidden(text="only the system administrator can create global contacts")
            item = self.contacts.update(request.match_info["contact_id"], **values)
            return web.json_response({"ok": True, "contact": self._decorate_contact(item)})
        except Exception as exc: return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def contacts_delete(self, request):
        actor, ws = await self._workspace(request, "contacts")
        current = self.contacts.get(request.match_info["contact_id"])
        if current and current.get("scope") == "workspace" and current.get("workspace_id") not in {"", ws["id"]}: raise web.HTTPForbidden()
        if current and current.get("scope") == "global" and not actor.get("system_admin"): raise web.HTTPForbidden(text="only the system administrator can delete global contacts")
        return web.json_response({"ok": self.contacts.delete(request.match_info["contact_id"])})

    async def contacts_reorder(self, request):
        _, ws = await self._workspace(request, "contacts")
        body = await request.json(); count = self.contacts.reorder([str(x) for x in body.get("ids", body.get("contact_ids", []))], workspace_id=ws["id"])
        return web.json_response({"ok": True, "updated": count})

    async def contacts_csv(self, request):
        _, ws = await self._workspace(request, "panel_access")
        out = io.StringIO(); writer = csv.DictWriter(out, fieldnames=["name","number","group","favorite","bypass_voicemail_detection","scope","tags","notes"]); writer.writeheader()
        for x in self.contacts.list(ws["id"], True):
            writer.writerow({"name":x.get("name",""),"number":x.get("number",""),"group":x.get("group",""),"favorite":"true" if x.get("favorite") else "false","bypass_voicemail_detection":"true" if x.get("bypass_voicemail_detection") else "false","scope":x.get("scope","workspace"),"tags":",".join(x.get("tags",[])),"notes":x.get("notes","")})
        return web.Response(text=out.getvalue(), content_type="text/csv", headers={"Content-Disposition":"attachment; filename=pbx-contacts.csv"})

    async def contacts_import(self, request):
        _, ws = await self._workspace(request, "contacts")
        try:
            body = await request.json(); raw = str(body.get("csv", body.get("raw", "")))
            reader = csv.DictReader(io.StringIO(raw)); added=updated=invalid=0
            for row in reader:
                try:
                    vals = self._contact_values_v3(row, ws["id"])
                    existing = self.contacts.find_by_number(vals["number"], ws["id"])
                    if existing and bool(body.get("merge", True)):
                        self.contacts.update(existing["id"], **vals); updated += 1
                    else:
                        self.contacts.create(**vals); added += 1
                except Exception: invalid += 1
            return web.json_response({"ok": True, "added": added, "updated": updated, "invalid": invalid})
        except Exception as exc: return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _system_pool(self, request, fn, *args):
        await self._system_admin(request)
        return await fn(request, *args) if args else await fn(request)

    async def caller_id_pool_list_v3(self, request): await self._system_admin(request); return await super().caller_id_pool_list(request)
    async def caller_id_pool_preview_v3(self, request): await self._system_admin(request); return await super().caller_id_pool_preview(request)
    async def caller_id_pool_bulk_v3(self, request): await self._system_admin(request); return await super().caller_id_pool_bulk(request)
    async def caller_id_pool_update_v3(self, request): await self._system_admin(request); return await super().caller_id_pool_update(request)
    async def caller_id_pool_delete_v3(self, request): await self._system_admin(request); return await super().caller_id_pool_delete(request)
    async def caller_id_pool_yaml_v3(self, request): await self._system_admin(request); return await super().caller_id_pool_yaml(request)
    async def random_call_pool_list_v3(self, request): await self._system_admin(request); return await super().random_call_pool_list(request)
    async def random_call_pool_preview_v3(self, request): await self._system_admin(request); return await super().random_call_pool_preview(request)
    async def random_call_pool_bulk_v3(self, request): await self._system_admin(request); return await super().random_call_pool_bulk(request)
    async def random_call_pool_update_v3(self, request): await self._system_admin(request); return await super().random_call_pool_update(request)
    async def random_call_pool_delete_v3(self, request): await self._system_admin(request); return await super().random_call_pool_delete(request)
    async def random_call_pool_clear_v3(self, request): await self._system_admin(request); return await super().random_call_pool_clear(request)
    async def random_call_pool_yaml_v3(self, request): await self._system_admin(request); return await super().random_call_pool_yaml(request)

    async def schedules_list(self, request):
        actor, ws = await self._workspace(request, "panel_access")
        rows = [x for x in self.scheduler.list() if not x.get("workspace_id") or x.get("workspace_id") == ws["id"] or actor.get("system_admin") and request.query.get("all") == "1"]
        return web.json_response({"ok": True, "scheduled_calls": rows})

    async def schedules_create(self, request):
        actor, ws = await self._workspace(request, "schedule")
        try:
            body = await request.json(); number=self.bot.ami.normalize_number(str(body.get("number",""))); self._calling_policy_check(number)
            cid=self._normalize_web_caller_id(body.get("caller_id","")); rand=bool(body.get("randomize_caller_id",False))
            if rand and not self._authorized_caller_id_pool(): raise ValueError("Random Caller ID needs an enabled Caller ID pool")
            contact=self.contacts.find_by_number(number,ws["id"])
            item=self.scheduler.create(number=number,caller_id=cid,recurrence=str(body.get("recurrence","weekly")),timezone_name=str(body.get("timezone","America/New_York")),local_time=str(body.get("local_time","")),weekdays=body.get("weekdays",[]),run_at=float(body.get("run_at",0) or 0),contact_name=(contact or {}).get("name",""),randomize_caller_id=rand,workspace_id=ws["id"],created_by_user_id=actor["user_id"],created_by_name=actor["name"])
            return web.json_response({"ok":True,"scheduled_call":item},status=201)
        except Exception as exc:return web.json_response({"ok":False,"error":str(exc)},status=400)

    async def schedules_cancel(self, request):
        actor, ws=await self._workspace(request,"schedule"); item=next((x for x in self.scheduler.list() if x["id"]==request.match_info["schedule_id"]),None)
        if not item:return web.json_response({"ok":False,"error":"schedule not found"},status=404)
        if item.get("workspace_id") and item["workspace_id"]!=ws["id"] and not actor.get("system_admin"):raise web.HTTPForbidden()
        return web.json_response({"ok":self.scheduler.cancel(item["id"])})

    async def schedules_delete(self, request):
        actor, ws=await self._workspace(request,"schedule"); item=next((x for x in self.scheduler.list() if x["id"]==request.match_info["schedule_id"]),None)
        if item and item.get("workspace_id") and item["workspace_id"]!=ws["id"] and not actor.get("system_admin"):raise web.HTTPForbidden()
        return web.json_response({"ok":self.scheduler.delete(request.match_info["schedule_id"])})

    async def soundboard_list_v3(self, request): await self._workspace(request,"panel_access"); return await super().soundboard_list(request)
    async def soundboard_save_v3(self, request): await self._workspace(request,"workspace_admin"); return await super().soundboard_save(request)
    async def soundboard_delete_v3(self, request): await self._workspace(request,"workspace_admin"); return await super().soundboard_delete(request)
    async def soundboard_play_v3(self, request): await self._workspace(request,"bridge"); return await super().soundboard_play(request)

    # ---------------------- telephony / routing ----------------------
    async def inbound_register(self, request):
        try:
            import uuid as uuidlib
            call_uuid=str(request.query.get("uuid","")).strip(); uuidlib.UUID(call_uuid)
            raw=str(request.query.get("number","")).strip(); number=self.bot.ami.normalize_number(raw) if raw else ""
            selected=await self.workspaces.resolve_inbound_workspaces(); wsids=[x["id"] for x in selected]
            default=selected[0]["id"] if selected else ""
            contact=self.contacts.find_by_number(number, default) if number else None
            name=(contact or {}).get("name","")
            self.bot.bridge.prepare_inbound(call_uuid,number,name,workspace_ids=wsids)
            route=await self.workspaces.routing_status()
            self.call_history.start_call(uuid=call_uuid,direction="inbound",number=number,contact_name=name,source="inbound",state="incoming",workspace_ids=wsids,route_reason=f"{route.get('mode','auto')} -> {','.join(wsids) or 'none'}")
            self.db.audit("call.inbound.registered",actor_user_id="pbx:ingress",actor_name="FreePBX",auth_type="ingress",workspace_id=default,entity_type="call",entity_id=call_uuid,call_uuid=call_uuid,number=number,detail={"workspaces":wsids,"routing_mode":route.get("mode")})
            await self._publish("call.incoming",{"uuid":call_uuid,"number":number,"contact_name":name,"workspace_ids":wsids})
            return web.json_response({"ok":True,"workspace_ids":wsids,"route_mode":route.get("mode")})
        except Exception as exc:return web.json_response({"ok":False,"error":str(exc)},status=400)

    async def join_v3(self, request):
        _,ws=await self._workspace(request,"panel_access")
        try:
            vc=await self.bot.bridge.ensure_voice(ws["id"]); self.bot.bridge.schedule_voice_idle_disconnect(ws["id"]); return web.json_response({"ok":True,"message":f"Connected {ws['alias']} to {vc.channel.name} for testing; on-demand voice will disconnect it when idle."})
        except Exception as exc:return web.json_response({"ok":False,"error":str(exc)},status=502)

    async def leave_v3(self, request):
        _,ws=await self._workspace(request,"workspace_admin"); await self.bot.bridge.disconnect_voice(ws["id"]); return web.json_response({"ok":True})

    async def pbx_ping_v3(self, request):
        await self._system_admin(request); return await super().pbx_ping(request)

    def _workspace_usage(self, workspace_id: str) -> int:
        total=0
        for s in self.bot.bridge.get_sessions():
            if workspace_id in getattr(s,"workspace_ids",[]):total+=1
        for p in self.bot.bridge.outbound_pending():
            if workspace_id in p.get("workspace_ids",[]):total+=1
        return total

    def _prepare_outbound(self, number: str, caller_id: str = "", contact_name: str = "", randomize_caller_id: bool = False, source: str = "manual", retry_of: str = "", retry_index: int = 0, *, workspace_ids=None, operator_user_id: str = "", operator_name: str = ""):
        if not self.bot.ami.configured or not self.config.audiosocket_advertise_host: raise RuntimeError("AMI or AudioSocket advertise host is not configured")
        number=self.bot.ami.normalize_number(number); self._calling_policy_check(number)
        caller_id=self._choose_web_caller_id(caller_id,randomize=randomize_caller_id)
        wsids=[str(x) for x in (workspace_ids or []) if self.db.get_workspace(str(x))]
        if not wsids and retry_of:
            prev=self.call_history.get_by_uuid(retry_of) or {}; wsids=list(prev.get("workspace_ids",[])); operator_user_id=operator_user_id or prev.get("operator_user_id",""); operator_name=operator_name or prev.get("operator_name","")
        if not wsids:
            default=self.workspaces.default_workspace(); wsids=[default["id"]] if default else []
        if not wsids: raise RuntimeError("No Discord workspace is configured for the call")
        for wid in wsids:
            ws=self.db.get_workspace(wid)
            if not ws or not ws.get("enabled") or not ws.get("allow_outbound"): raise ValueError(f"Outbound calling is disabled for {wid}")
            if self._workspace_usage(wid)>=int(ws.get("max_calls",self.config.max_simultaneous_calls)): raise RuntimeError(f"Workspace {ws.get('alias')} reached its call limit")
        status=self.bot.bridge.status_dict()
        if int(status.get("call_count",0))+int(status.get("outbound_pending_count",0))>=self.config.max_simultaneous_calls:raise RuntimeError(f"Maximum simultaneous/pending calls reached ({self.config.max_simultaneous_calls})")
        contact=self.contacts.find_by_number(number,wsids[0])
        if not contact_name:
            contact_name=(contact or {}).get("name","")
        bypass_vm=bool((contact or {}).get("bypass_voicemail_detection",False))
        vm_detection=bool(self._voicemail_detection_enabled) and not bypass_vm
        uid=self.bot.bridge.prepare_outbound(number,caller_id,contact_name,source=source,randomize_caller_id=randomize_caller_id,retry_of=retry_of,retry_index=retry_index,voicemail_detection_enabled=vm_detection,workspace_ids=wsids,operator_user_id=operator_user_id,operator_name=operator_name)
        if bypass_vm:
            self.call_history.log_activity("voicemail detection bypass", f"Contact override: {contact_name or number}", uuid=uid, number=number)
        self.bot.bridge.update_pending_state(uid,"starting")
        self.call_history.start_call(uuid=uid,direction="outbound",number=number,caller_id=caller_id,contact_name=contact_name,source=source,state="starting",retry_of=retry_of,retry_index=retry_index,workspace_ids=wsids,operator_user_id=operator_user_id,operator_name=operator_name)
        return number,caller_id,contact_name,uid

    async def _originate_prepared(self, number, caller_id, contact_name, call_uuid):
        pending=self.bot.bridge.get_pending(call_uuid) or {}; wsids=list(pending.get("workspace_ids",[]))
        try:
            self.bot.bridge.update_pending_state(call_uuid,"joining Discord"); self.call_history.set_state(call_uuid,"joining Discord")
            results=await asyncio.gather(*(asyncio.wait_for(self.bot.bridge.ensure_voice(wid),timeout=12) for wid in wsids),return_exceptions=True)
            if not any(not isinstance(x,Exception) for x in results): raise RuntimeError("No routed Discord voice destination could connect")
            if not self.bot.bridge.get_pending(call_uuid):return False,"Call was cancelled before originate",call_uuid
            self.bot.bridge.update_pending_state(call_uuid,"sending to PBX"); self.call_history.set_state(call_uuid,"sending to PBX")
            ok,detail,_=await asyncio.wait_for(asyncio.to_thread(self.bot.ami.originate_to_audiosocket,number,self.config.audiosocket_advertise_host,self.config.audiosocket_port,self.config.ami_dial_context,self.config.ami_dial_timeout_ms,caller_id,call_uuid),timeout=max(7.0,float(self.config.ami_timeout)+2))
            if not ok:
                self.bot.bridge.fail_pending(call_uuid,detail); outcome=self._failure_outcome(detail); self.call_history.fail(call_uuid,outcome=outcome,diagnostic=self._sanitize_detail(detail)); await self._maybe_schedule_redial(call_uuid,outcome,{"uuid":call_uuid,"number":number,"caller_id":caller_id,"contact_name":contact_name}); return False,detail,call_uuid
            self.bot.bridge.update_pending_state(call_uuid,"dialing / ringing"); self.call_history.set_state(call_uuid,"dialing / ringing"); self.contacts.mark_called(number=number,workspace_id=wsids[0] if wsids else "")
            return True,detail or "Originate queued",call_uuid
        except asyncio.CancelledError:self.bot.bridge.cancel_pending(call_uuid);raise
        except Exception as exc:
            self.bot.bridge.fail_pending(call_uuid,str(exc)); outcome="timeout" if isinstance(exc,asyncio.TimeoutError) else "failed"; self.call_history.fail(call_uuid,outcome=outcome,diagnostic=self._sanitize_detail(exc)); await self._maybe_schedule_redial(call_uuid,outcome,{"uuid":call_uuid,"number":number,"caller_id":caller_id,"contact_name":contact_name}); return False,str(exc),call_uuid

    def _queue_web_outbound(self, number: str, caller_id: str = "", contact_name: str = "", randomize_caller_id: bool = False, source: str = "manual", retry_of: str = "", retry_index: int = 0, *, workspace_ids=None, operator_user_id: str = "", operator_name: str = ""):
        n,c,name,uid=self._prepare_outbound(number,caller_id,contact_name,randomize_caller_id,source,retry_of,retry_index,workspace_ids=workspace_ids,operator_user_id=operator_user_id,operator_name=operator_name)
        task=asyncio.create_task(self._originate_prepared(n,c,name,uid),name=f"web-outbound-{uid[:8]}");self._outbound_tasks[uid]=task
        def done(t,uid=uid):
            self._outbound_tasks.pop(uid,None)
            if not t.cancelled():
                try:t.result()
                except Exception:log.exception("Outbound task failed %s",uid)
        task.add_done_callback(done);return n,c,name,uid

    async def _dial_outbound(self, number: str, caller_id: str = "", contact_name: str = "", randomize_caller_id: bool = False, source: str = "manual", retry_of: str = "", retry_index: int = 0, *, workspace_ids=None, operator_user_id: str = "", operator_name: str = ""):
        n,c,name,uid=self._prepare_outbound(number,caller_id,contact_name,randomize_caller_id,source,retry_of,retry_index,workspace_ids=workspace_ids,operator_user_id=operator_user_id,operator_name=operator_name);return await self._originate_prepared(n,c,name,uid)

    async def dial(self, request):
        uid=""
        try:
            actor,ws=await self._workspace(request,"dial"); self._dial_rate_check(actor,ws["id"]); body=await request.json()
            n,c,name,uid=self._queue_web_outbound(str(body.get("number","")),str(body.get("caller_id","")),str(body.get("contact_name","")),bool(body.get("randomize_caller_id",False)),str(body.get("source","manual"))[:32],workspace_ids=[ws["id"]],operator_user_id=actor["user_id"],operator_name=actor["name"])
            await self._publish("call.queued",{"uuid":uid,"number":n,"caller_id":c,"contact_name":name,"workspace_ids":[ws["id"]],"operator_name":actor["name"]})
            return web.json_response({"ok":True,"uuid":uid,"number":n,"caller_id":c,"contact_name":name},status=202)
        except Exception as exc:
            if uid:self.bot.bridge.cancel_pending(uid)
            return web.json_response({"ok":False,"error":str(exc)},status=400 if isinstance(exc,ValueError) else 500)

    async def dial_random(self, request):
        uid=""
        try:
            actor,ws=await self._workspace(request,"dial");self._dial_rate_check(actor,ws["id"]);body=await request.json() if request.can_read_body else {}
            async with self._random_dial_lock:
                picked=self._choose_random_call_target(); number=str(picked.get("number","")); contact=self.contacts.find_by_number(number,ws["id"]); name=(contact or {}).get("name","") or str(picked.get("label","")).strip()
                n,c,name,uid=self._queue_web_outbound(number,str(body.get("caller_id","")),name,bool(body.get("randomize_caller_id",False)),"random",workspace_ids=[ws["id"]],operator_user_id=actor["user_id"],operator_name=actor["name"])
            return web.json_response({"ok":True,"uuid":uid,"number":n,"caller_id":c,"contact_name":name},status=202)
        except Exception as exc:
            if uid:self.bot.bridge.cancel_pending(uid)
            return web.json_response({"ok":False,"error":str(exc)},status=400 if isinstance(exc,ValueError) else 500)

    async def cancel_outbound(self, request):
        await self._call_access(request,request.match_info["uuid"],"dial"); return await super().cancel_outbound(request)

    async def call_claim(self, request):
        uid=request.match_info["uuid"];actor,wid=await self._call_access(request,uid,"receive_inbound")
        row=self.call_history.get_by_uuid(uid) or {}
        if row.get("answered_by_user_id") and row.get("answered_by_user_id")!=actor["user_id"]:return web.json_response({"ok":False,"error":f"Already claimed by {row.get('answered_by_name') or 'another operator'}"},status=409)
        self.call_history.set_answered_by(uid,actor["user_id"],actor["name"],wid);await self._publish("call.claimed",{"uuid":uid,"workspace_id":wid,"user_id":actor["user_id"],"name":actor["name"]});return web.json_response({"ok":True})

    async def call_workspaces(self, request):
        uid=request.match_info["uuid"];actor,_=await self._call_access(request,uid,"bridge");body=await request.json();desired=[]
        for wid in body.get("workspace_ids",[]):
            wid=str(wid)
            if actor.get("system_admin") or await self.auth.can(actor,wid,"bridge"):desired.append(wid)
        if not desired:return web.json_response({"ok":False,"error":"select at least one permitted workspace"},status=400)
        current=self.bot.bridge.get_session(uid);old=set(getattr(current,"workspace_ids",[]) if current else [])
        for wid in desired:
            if wid not in old:
                try:await self.bot.bridge.add_call_workspace(uid,wid)
                except Exception as exc:return web.json_response({"ok":False,"error":str(exc)},status=502)
        for wid in old-set(desired):self.bot.bridge.remove_call_workspace(uid,wid)
        self.call_history.set_workspaces(uid,desired,"operator routing change");return web.json_response({"ok":True,"workspace_ids":desired})

    async def call_hold(self, request):
        uid=request.match_info["uuid"];await self._call_access(request,uid,"bridge");body=await request.json();held=bool(body.get("held",True))
        if not self.bot.bridge.set_hold(uid,held):return web.json_response({"ok":False,"error":"call not found"},status=404)
        self.call_history.event(uid,"hold" if held else "resume",detail={"held":held});return web.json_response({"ok":True,"held":held})

    async def call_park(self, request):
        uid=request.match_info["uuid"];actor,wid=await self._call_access(request,uid,"bridge");slot=self.db.park_call(uid,wid,actor["name"]);self.bot.bridge.set_hold(uid,True);session=self.bot.bridge.get_session(uid)
        if session:session.park_slot=slot
        self.call_history.event(uid,"parked",actor_user_id=actor["user_id"],actor_name=actor["name"],workspace_id=wid,detail={"slot":slot});return web.json_response({"ok":True,"slot":slot})

    async def call_unpark(self, request):
        slot=int(request.match_info["slot"]);item=next((x for x in self.db.list_parked() if int(x["slot"])==slot),None)
        if not item:return web.json_response({"ok":False,"error":"park slot empty"},status=404)
        await self._workspace(request,"bridge",explicit=item["workspace_id"]);self.db.unpark_call(slot);self.bot.bridge.set_hold(item["call_uuid"],False);s=self.bot.bridge.get_session(item["call_uuid"])
        if s:s.park_slot=0
        return web.json_response({"ok":True,"uuid":item["call_uuid"]})

    async def call_transfer(self, request):
        uid=request.match_info["uuid"];await self._call_access(request,uid,"bridge");body=await request.json();target=str(body.get("target",""))
        ok,detail=await asyncio.to_thread(self.bot.ami.blind_transfer,uid,target,self.config.ami_dial_context);self.call_history.event(uid,"transfer",detail={"target":target,"ok":ok,"detail":self._sanitize_detail(detail)});return web.json_response({"ok":ok,"detail":self._sanitize_detail(detail)},status=200 if ok else 502)

    async def call_routes(self, request): await self._call_access(request,request.match_info["uuid"],"bridge"); return await super().call_routes(request)
    async def solo_talk(self, request): await self._call_access(request,request.match_info["uuid"],"bridge"); return await super().solo_talk(request)
    async def focus(self, request): await self._call_access(request,request.match_info["uuid"],"bridge"); return await super().focus(request)
    async def call_dtmf(self, request):
        uid = request.match_info["uuid"]
        actor, wid = await self._call_access(request, uid, "bridge")
        session = self.bot.bridge.get_session(uid)
        if not session or not session.active:
            return web.json_response({"ok": False, "error": "Call not found."}, status=404)
        try:
            body = await request.json()
            digit = str(body.get("digit", ""))[:1]
            ok, detail, channel = await asyncio.to_thread(self.bot.ami.play_dtmf, uid, digit)
            safe_detail = self._sanitize_detail(detail)
            if not ok:
                log.warning("DTMF failed call=%s digit=%s workspace=%s detail=%s", uid, digit, wid, safe_detail)
                return web.json_response({"ok": False, "error": safe_detail or "Asterisk refused DTMF."}, status=502)
            self.call_history.log_activity("DTMF", f"Sent {digit}", uuid=uid, number=str(getattr(session, "remote_number", "")))
            self.call_history.event(uid, "dtmf", actor_user_id=actor["user_id"], actor_name=actor["name"], workspace_id=wid, detail={"digit": digit, "channel": channel})
            log.info("DTMF %s -> call %s via %s", digit, uid, channel)
            return web.json_response({"ok": True, "message": f"Sent DTMF {digit}.", "channel": channel})
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            log.exception("DTMF dispatch failed for call %s", uid)
            return web.json_response({"ok": False, "error": self._sanitize_detail(exc)}, status=502)
    async def call_auto_redial(self, request): await self._call_access(request,request.match_info["uuid"],"dial"); return await super().call_auto_redial(request)
    async def call_notes(self, request): await self._call_access(request,request.match_info["uuid"],"history"); return await super().call_notes(request)
    async def call_split(self, request): await self._call_access(request,request.match_info["uuid"],"bridge"); return await super().call_split(request)
    async def call_hangup(self, request): await self._call_access(request,request.match_info["uuid"],"bridge"); return await super().call_hangup(request)
    async def calls_conference(self, request):
        body=await request.json();ids=[str(x) for x in body.get("uuids",[])];
        for uid in ids:await self._call_access(request,uid,"bridge")
        # Legacy method expects to read body again; call bridge directly.
        ok,detail,group=self.bot.bridge.create_conference(ids);return web.json_response({"ok":ok,"message":detail,"group":group},status=200 if ok else 400)

    async def all_talk(self, request): await self._workspace(request,"bridge"); return await super().all_talk(request)
    async def all_listen(self, request): await self._workspace(request,"bridge"); return await super().all_listen(request)
    async def operator_audio(self, request): await self._workspace(request,"bridge"); return await super().operator_audio(request)
    async def all_routes(self, request): await self._workspace(request,"bridge"); return await super().all_routes(request)
    def _conference_setting_key(self, workspace_id: str) -> str:
        return f"workspace_conference_callers:{str(workspace_id or '').strip()}"

    def _conference_mode_get(self, workspace_id: str) -> bool:
        wid = str(workspace_id or "").strip()
        if not wid:
            return False
        return bool(self.db.get_setting(self._conference_setting_key(wid), False))

    def _conference_mode_set(self, workspace_id: str, enabled: bool) -> int:
        wid = str(workspace_id or "").strip()
        if not wid:
            raise ValueError("workspace_id is required")
        enabled = bool(enabled)
        self.db.set_setting(self._conference_setting_key(wid), enabled)
        return self.bot.bridge.set_workspace_conference_mode(wid, enabled)

    async def workspace_conference_mode(self, request):
        actor, ws = await self._workspace(request, "bridge")
        try:
            body = await request.json() if request.can_read_body else {}
            if "enabled" not in body:
                raise ValueError("enabled is required")
            active_count = self._conference_mode_set(ws["id"], bool(body.get("enabled")))
            persisted = self._conference_mode_get(ws["id"])
            self.bot.bridge.set_workspace_conference_mode(ws["id"], persisted)
            diag = self.bot.bridge.conference_diagnostics(ws["id"])
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

        state = "enabled" if persisted else "disabled"
        self.call_history.log_activity("workspace conference mode", f"{ws['alias']}: {state} ({active_count} active call(s))")
        self.db.audit(
            "workspace.conference_mode", actor_user_id=actor["user_id"], actor_name=actor["name"],
            auth_type=actor.get("auth_type", "session"), workspace_id=ws["id"], entity_type="workspace",
            entity_id=ws["id"], detail={"enabled": persisted, "active_calls": active_count},
        )
        await self._publish("workspace.conference_mode", {"workspace_id": ws["id"], "enabled": persisted, "active_calls": active_count})
        if persisted:
            message = f"Conference callers enabled for {ws['alias']}."
            message += f" {active_count} active callers are linked." if active_count >= 2 else " It will engage as soon as two calls are active."
        else:
            message = f"Conference callers disabled for {ws['alias']}. Callers are isolated again."
        return web.json_response({"ok": True, "enabled": persisted, "active_calls": active_count, "workspace_id": ws["id"], "conference": diag, "message": message})

    async def hangup_all(self, request):
        actor, ws = await self._workspace(request, "bridge")
        wid = ws["id"]
        sessions = [s for s in self.bot.bridge.get_sessions() if wid in list(getattr(s, "workspace_ids", []) or [])]
        # Cancel automatic redial for calls owned by this workspace as well, so
        # "Hang up all" cannot immediately recreate a call the operator stopped.
        for uid in list(self._auto_redial):
            row = self.call_history.get_by_uuid(uid) or {}
            if wid in list(row.get("workspace_ids", []) or []):
                self._cancel_auto_redial(uid)
        disconnected = 0
        for session in sessions:
            uid = str(getattr(session, "call_uuid", "") or "")
            if uid and await self.bot.bridge.hangup(uid):
                disconnected += 1
                self.call_history.event(uid, "hangup-all", actor_user_id=actor["user_id"], actor_name=actor["name"], workspace_id=wid)
        self.call_history.log_activity("workspace hangup all", f"{ws['alias']}: disconnected {disconnected} call(s)")
        return web.json_response({"ok": True, "message": f"Disconnected {disconnected} call(s) in {ws['alias']}.", "count": disconnected, "workspace_id": wid})
    async def ringback_setting(self, request): await self._workspace(request,"settings"); return await super().ringback_setting(request)
    async def voicemail_detection_setting(self, request): await self._workspace(request,"settings"); return await super().voicemail_detection_setting(request)

    def _operator_preferences_key(self, user_id: str, workspace_id: str) -> str:
        # Persist console choices per signed-in user and Discord workspace. Using the
        # application settings store keeps the preference in normal PBX backups and
        # makes it follow the operator across browsers/devices.
        return f"operator_preferences:{user_id}:{workspace_id}"

    async def operator_preferences(self, request):
        actor, ws = await self._workspace(request, "panel_access")
        prefs = self.db.get_setting(self._operator_preferences_key(actor["user_id"], ws["id"]), {}) or {}
        return web.json_response({
            "ok": True,
            "workspace_id": ws["id"],
            "preferences": {
                "random_caller_id": bool(prefs.get("random_caller_id", False)),
            },
        })

    async def operator_preferences_update(self, request):
        actor, ws = await self._workspace(request, "panel_access")
        body = await request.json()
        key = self._operator_preferences_key(actor["user_id"], ws["id"])
        prefs = self.db.get_setting(key, {}) or {}
        if "random_caller_id" in body:
            prefs["random_caller_id"] = bool(body.get("random_caller_id"))
        self.db.set_setting(key, prefs)
        return await self.operator_preferences(request)

    async def _scheduler_loop(self):
        try:
            while True:
                now=time.time()
                for item in self.scheduler.claim_due(now):
                    item_id=item["id"]
                    if now-float(item.get("run_at",now))>300:
                        self.scheduler.finish_occurrence(item_id,ok=False,detail="Missed while service was offline for more than 5 minutes",missed=True);continue
                    wid=str(item.get("workspace_id","") or "")
                    if not wid:
                        default=self.workspaces.default_workspace();wid=default["id"] if default else ""
                    if not wid or not self.db.get_workspace(wid):
                        self.scheduler.finish_occurrence(item_id,ok=False,detail="Scheduled workspace no longer exists");continue
                    try:
                        self._calling_policy_check(str(item["number"]))
                        ok,detail,uid=await self._dial_outbound(item["number"],item.get("caller_id",""),item.get("contact_name",""),randomize_caller_id=bool(item.get("randomize_caller_id",False)),source="schedule",workspace_ids=[wid],operator_user_id=str(item.get("created_by_user_id","system:scheduler")),operator_name=str(item.get("created_by_name","Scheduler")))
                        self.scheduler.finish_occurrence(item_id,ok=ok,detail=detail,call_uuid=uid)
                    except Exception as exc:
                        self.scheduler.finish_occurrence(item_id,ok=False,detail=str(exc));log.exception("Scheduled call failed %s",item_id)
                await asyncio.sleep(1)
        except asyncio.CancelledError:raise

    # ---------------------- history / audit / policy ----------------------
    async def history_list(self, request):
        actor=request["actor"]
        common=dict(limit=int(request.query.get("limit",100)),offset=int(request.query.get("offset",0)),q=str(request.query.get("q","")),direction=str(request.query.get("direction","")),outcome=str(request.query.get("outcome","")),answered=request.query.get("answered")=="1",missed=request.query.get("missed")=="1",operator_user_id=str(request.query.get("operator","")))
        if request.query.get("scope") == "mine":
            accessible=await self._actor_workspaces(actor)
            ids=[]
            for ws in accessible:
                if actor.get("system_admin") or await self.auth.can(actor,ws["id"],"history"):
                    ids.append(ws["id"])
            if not ids: raise web.HTTPForbidden(text="history permission required")
            result=self.call_history.list_calls(**common,workspace_ids=ids)
            aliases={w["id"]:w.get("alias",w["id"]) for w in accessible}
            for row in result.get("calls",[]):
                row["workspace_aliases"]=[aliases.get(wid,wid) for wid in row.get("workspace_ids",[]) if wid in aliases]
            return web.json_response({"ok":True,"scope":"mine",**result})
        actor,ws=await self._workspace(request,"history",allow_all=True);wid="" if ws["id"]=="__all__" else ws["id"]
        result=self.call_history.list_calls(**common,workspace_id=wid)
        aliases={w["id"]:w.get("alias",w["id"]) for w in await self._actor_workspaces(actor)}
        for row in result.get("calls",[]): row["workspace_aliases"]=[aliases.get(x,x) for x in row.get("workspace_ids",[])]
        return web.json_response({"ok":True,"scope":"workspace",**result})

    async def history_timeline(self, request):
        uid=request.match_info["uuid"]
        row=self.call_history.get_by_uuid(uid)
        if not row:return web.json_response({"ok":False,"error":"call not found"},status=404)
        await self._call_access(request,uid,"history")
        audit=self.db.audit_list(limit=500,call_uuid=uid)
        return web.json_response({"ok":True,"call":row,"timeline":self.call_history.timeline(uid),"audit":audit.get("events",[])})

    async def history_update(self, request):
        row=self.call_history.get_by_id(int(request.match_info["row_id"]))
        if not row:return web.json_response({"ok":False,"error":"call not found"},status=404)
        await self._call_access(request,row["uuid"],"history");body=await request.json();ok=self.call_history.update_notes(row["uuid"],notes=body.get("notes") if "notes" in body else None,disposition=body.get("disposition") if "disposition" in body else None);return web.json_response({"ok":ok})

    async def audit_list(self, request):
        actor=request["actor"]
        wid=str(request.query.get("workspace_id","") or request.headers.get("X-PBX-Workspace",""))
        if not actor.get("system_admin"):
            if not wid: _,ws=await self._workspace(request,"audit");wid=ws["id"]
            elif not await self.auth.can(actor,wid,"audit"):raise web.HTTPForbidden()
        result=self.db.audit_list(limit=int(request.query.get("limit",200)),offset=int(request.query.get("offset",0)),q=str(request.query.get("q","")),actor=str(request.query.get("actor","")),workspace_id=wid,action=str(request.query.get("action","")),call_uuid=str(request.query.get("call_uuid","")))
        # `events` is canonical; `items` is retained as a frontend/API compatibility alias.
        return web.json_response({"ok":True,**result,"items":result.get("events",[])})

    async def audit_verify(self, request):
        await self._system_admin(request)
        result=self.db.verify_audit_chain()
        return web.json_response({
            "ok":True, "valid":bool(result.get("ok")), "checked":int(result.get("checked",0)),
            "head":str(result.get("head","")), "first_bad_id":result.get("failed_id"),
        })

    async def policies_get(self, request):
        await self._system_admin(request);return web.json_response({"ok":True,"calling_policy":self.db.get_setting("calling_policy",{}),"rate_limits":self.db.get_setting("dial_rate_limits",{}),"retention":self.db.get_setting("retention",{})})

    async def policies_set(self, request):
        actor=await self._system_admin(request);body=await request.json();self.db.save_revision("before policy change",actor["user_id"],actor["name"])
        for key in ("calling_policy","dial_rate_limits","retention"):
            if key in body:self.db.set_setting(key,body[key])
        return await self.policies_get(request)

    async def dnc_list(self, request): await self._system_admin(request); return web.json_response({"ok":True,"numbers":self.db.dnc_list()})
    async def dnc_add(self, request):
        actor=await self._system_admin(request);body=await request.json()
        try:number=self.bot.ami.normalize_number(str(body.get("number","")));self.db.dnc_add(number,str(body.get("reason","")),actor["user_id"]);return web.json_response({"ok":True,"number":number})
        except Exception as exc:return web.json_response({"ok":False,"error":str(exc)},status=400)
    async def dnc_delete(self, request): await self._system_admin(request); return web.json_response({"ok":self.db.dnc_remove(request.match_info["number"])})

    # ---------------------- system administration ----------------------
    async def system_settings(self, request):
        await self._system_admin(request)
        return web.json_response({"ok":True,"settings":self.db.all_settings(),"runtime":{"ami_host":self.config.ami_host,"ami_port":self.config.ami_port,"ami_user":self.config.ami_user,"audiosocket_advertise_host":self.config.audiosocket_advertise_host,"dial_context":self.config.ami_dial_context,"max_simultaneous_calls":self.config.max_simultaneous_calls,"public_base_url":self.auth.public_base_url(request),"discord_client_id":self.auth.client_id(),"web_auth_mode":self.config.web_auth_mode,"system_admin_discord_ids":sorted(self.auth.system_admin_discord_ids()),"voice_mode":"on_demand","voice_idle_disconnect_seconds":self.config.leave_voice_after_call_seconds}})

    async def system_oauth_status(self, request):
        await self._system_admin(request)
        base = self.auth.public_base_url(request)
        redirect = (base.rstrip("/") + "/auth/discord/callback") if base else ""
        client_id = self.auth.client_id()
        client_secret = self.auth.client_secret()
        bot_token = self.secret_store.get("discord_bot_token", self.config.discord_token)
        members_intent = bool(getattr(getattr(self.bot, "intents", None), "members", False))
        return web.json_response({"ok":True,"ready":bool(base.startswith("https://") and client_id and client_secret and bot_token and members_intent and self.bot.is_ready()),"public_base_url":base,"redirect_uri":redirect,"client_id":client_id,"client_id_configured":bool(client_id),"client_secret_configured":bool(client_secret),"bot_token_configured":bool(bot_token),"discord_gateway_ready":bool(self.bot.is_ready()),"server_members_intent_requested":members_intent,"system_admin_discord_ids":sorted(self.auth.system_admin_discord_ids()),"auth_mode":self.config.web_auth_mode})

    async def system_settings_update(self, request):
        actor=await self._system_admin(request);body=await request.json();self.db.save_revision("before system settings change",actor["user_id"],actor["name"])
        allowed={"asterisk_host","asterisk_port","asterisk_user","audiosocket_advertise_host","asterisk_dial_context","max_simultaneous_calls","public_base_url","discord_client_id","web_auth_mode","system_admin_discord_ids","inbound_routing","calling_policy","dial_rate_limits","retention","demo_mode","github_repo"}
        if "github_repo" in body:
            repo=str(body.get("github_repo", "")).strip()
            if repo and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
                return web.json_response({"ok":False,"error":"GitHub repository must be owner/repository"},status=400)
            body["github_repo"]=repo
        if "system_admin_discord_ids" in body:
            raw=body.get("system_admin_discord_ids",[])
            if isinstance(raw,str): raw=[x.strip() for x in raw.replace("\n",",").split(",") if x.strip()]
            ids=[]
            for x in raw if isinstance(raw,(list,tuple,set)) else []:
                x=str(x).strip()
                if x and not x.isdigit(): return web.json_response({"ok":False,"error":f"invalid Discord user ID: {x}"},status=400)
                if x and x not in ids: ids.append(x)
            body["system_admin_discord_ids"]=ids
        if "web_auth_mode" in body:
            mode=str(body.get("web_auth_mode","")).strip().lower()
            if mode not in {"discord","hybrid","basic","none"}:
                return web.json_response({"ok":False,"error":"invalid web authentication mode"},status=400)
            if mode in {"discord","hybrid"} and not self.db.local_admin_configured() and not (self.auth.client_id() and self.auth.client_secret()):
                return web.json_response({"ok":False,"error":"configure a local break-glass administrator or Discord OAuth before enabling this authentication mode"},status=400)
            body["web_auth_mode"]=mode
        for key,val in body.items():
            if key in allowed:self.db.set_setting(key,val)
        self._apply_persisted_runtime_settings();return await self.system_settings(request)

    async def system_secrets_status(self, request):
        await self._system_admin(request);return web.json_response({"ok":True,"stored":self.secret_store.status(),"discord_bot_token":self.secret_store.has("discord_bot_token") or bool(self.config.discord_token),"ami_secret":self.secret_store.has("asterisk_ami_secret") or bool(self.config.ami_secret),"github_release_token":self.secret_store.has("github_release_token")})

    async def system_secrets_update(self, request):
        await self._system_admin(request);body=await request.json();mapping={"discord_bot_token":"discord_bot_token","discord_oauth_client_secret":"discord_oauth_client_secret","asterisk_ami_secret":"asterisk_ami_secret","pbx_ingress_token":"pbx_ingress_token","github_release_token":"github_release_token"};changed=[]
        for src,dst in mapping.items():
            value=str(body.get(src,"")).strip()
            if value:self.secret_store.set(dst,value);changed.append(dst)
        self._apply_persisted_runtime_settings();return web.json_response({"ok":True,"changed":changed,"restart_recommended":bool(set(changed)&{"discord_bot_token","discord_oauth_client_secret"})})

    async def system_local_admin_status(self, request):
        await self._system_admin(request)
        info=self.db.local_admin_info()
        return web.json_response({"ok":True,"configured":self.db.local_admin_configured(),"username":info.get("username",""),"updated_at":info.get("updated_at",0)})

    async def system_local_admin_update(self, request):
        actor=await self._system_admin(request);body=await request.json()
        username=str(body.get("username","admin")).strip() or "admin"
        password=str(body.get("password",""))
        try:
            self.db.set_local_admin(username,password)
            self.db.audit("security.local_admin.updated",actor_user_id=str(actor.get("user_id","")),actor_name=str(actor.get("name","")),auth_type=str(actor.get("auth_type","")),detail={"username":username},ip=self._request_ip(request))
            return web.json_response({"ok":True,"configured":True,"username":username})
        except Exception as exc:
            return web.json_response({"ok":False,"error":str(exc)},status=400)

    async def system_local_users_list(self, request):
        await self._system_admin(request)
        return web.json_response({"ok":True,"users":self.db.list_local_users(),"capabilities":sorted(CAPABILITIES)})

    def _local_user_body(self, body: dict[str, Any]) -> dict[str, Any]:
        access=body.get("workspace_access", [])
        if not isinstance(access, list):
            raise ValueError("workspace_access must be a list")
        cleaned=[]
        for item in access:
            if not isinstance(item, dict): continue
            wid=str(item.get("workspace_id", "")).strip()
            if not wid: continue
            if not self.db.get_workspace(wid): raise ValueError(f"unknown workspace: {wid}")
            caps=sorted({str(c) for c in (item.get("capabilities") or []) if str(c) in CAPABILITIES})
            if caps: cleaned.append({"workspace_id":wid,"capabilities":caps})
        return {
            "username":str(body.get("username", "")).strip(),
            "display_name":str(body.get("display_name", "")).strip(),
            "password":str(body.get("password", "")),
            "enabled":bool(body.get("enabled", True)),
            "is_system_admin":bool(body.get("is_system_admin", False)),
            "workspace_access":cleaned,
        }

    async def system_local_user_save(self, request):
        actor=await self._system_admin(request)
        try:
            values=self._local_user_body(await request.json())
            row=self.db.save_local_user(**values)
            self.db.audit("security.local_user.created",actor_user_id=actor["user_id"],actor_name=actor["name"],auth_type=actor.get("auth_type","session"),entity_type="local_user",entity_id=row["id"],detail={"username":row["username"],"system_admin":row["is_system_admin"],"workspaces":[x["workspace_id"] for x in row.get("workspace_access",[])]},ip=self._request_ip(request))
            return web.json_response({"ok":True,"user":row,"message":f"Local account {row['username']} created."},status=201)
        except Exception as exc:
            return web.json_response({"ok":False,"error":str(exc)},status=400)

    async def system_local_user_update(self, request):
        actor=await self._system_admin(request); uid=str(request.match_info["user_id"])
        if not self.db.get_local_user(uid): return web.json_response({"ok":False,"error":"local account not found"},status=404)
        try:
            values=self._local_user_body(await request.json())
            row=self.db.save_local_user(user_id=uid,**values)
            self.db.audit("security.local_user.updated",actor_user_id=actor["user_id"],actor_name=actor["name"],auth_type=actor.get("auth_type","session"),entity_type="local_user",entity_id=uid,detail={"username":row["username"],"enabled":row["enabled"],"system_admin":row["is_system_admin"]},ip=self._request_ip(request))
            return web.json_response({"ok":True,"user":row,"message":f"Local account {row['username']} updated."})
        except Exception as exc:
            return web.json_response({"ok":False,"error":str(exc)},status=400)

    async def system_local_user_delete(self, request):
        actor=await self._system_admin(request); uid=str(request.match_info["user_id"]); old=self.db.get_local_user(uid)
        if not old: return web.json_response({"ok":False,"error":"local account not found"},status=404)
        ok=self.db.delete_local_user(uid)
        if ok:self.db.audit("security.local_user.deleted",actor_user_id=actor["user_id"],actor_name=actor["name"],auth_type=actor.get("auth_type","session"),entity_type="local_user",entity_id=uid,detail={"username":old.get("username","")},ip=self._request_ip(request))
        return web.json_response({"ok":ok})

    async def revisions_list(self, request): await self._system_admin(request); return web.json_response({"ok":True,"revisions":self.db.list_revisions()})
    async def revision_restore(self, request):
        actor=await self._system_admin(request);self.db.save_revision("automatic snapshot before restore",actor["user_id"],actor["name"]);ok=self.db.restore_revision(int(request.match_info["revision_id"]));return web.json_response({"ok":ok,"restart_recommended":ok})

    async def system_restart(self, request):
        await self._system_admin(request)
        async def stop_later():
            await asyncio.sleep(.5);os.kill(os.getpid(),15)
        asyncio.create_task(stop_later());return web.json_response({"ok":True,"message":"Service restart requested. Docker restart policy will bring it back."})

    async def backups_list(self, request): await self._system_admin(request); return web.json_response({"ok":True,"backups":self.backups.list(),"restore":self.backups.restore_status()})
    async def backup_create(self, request):
        await self._system_admin(request);body=await request.json() if request.can_read_body else {};path=await asyncio.to_thread(self.backups.create,str(body.get("label","manual"))[:60],bool(body.get("include_secrets",False)));self.backups.prune(30);return web.json_response({"ok":True,"name":path.name,"size":path.stat().st_size})
    async def backup_download(self, request):
        await self._system_admin(request);name=Path(request.match_info["name"]).name;path=self.backups.backup_dir/name
        if not path.is_file():raise web.HTTPNotFound()
        return web.FileResponse(path,headers={"Content-Disposition":f'attachment; filename="{name}"'})

    async def backup_restore(self, request):
        actor=await self._system_admin(request);name=Path(request.match_info["name"]).name
        try:
            # Safety snapshot is taken before staging a restore. The selected restore
            # is applied by bot startup before any SQLite database is opened.
            safety=await asyncio.to_thread(self.backups.create,"pre-restore",False)
            manifest=await asyncio.to_thread(self.backups.queue_restore,name)
            self.db.audit("system.backup.restore_queued",actor_user_id=str(actor.get("user_id","")),actor_name=str(actor.get("name","")),auth_type=str(actor.get("auth_type","")),detail={"backup":name,"safety_backup":safety.name,"format":manifest.get("format")},ip=self._request_ip(request))
            async def stop_later():
                await asyncio.sleep(.8);os.kill(os.getpid(),15)
            asyncio.create_task(stop_later())
            return web.json_response({"ok":True,"message":"Backup restore queued; service restart requested.","backup":name,"safety_backup":safety.name})
        except Exception as exc:
            return web.json_response({"ok":False,"error":str(exc)},status=400)

    def _update_status_path(self) -> Path:
        return self._updates_dir / "status.json"

    def _read_update_json(self, path: Path, default=None):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {} if default is None else default

    @staticmethod
    def _version_tuple(value: str) -> tuple[int, ...]:
        nums=re.findall(r"\d+", str(value or ""))
        return tuple(int(x) for x in nums[:4]) if nums else (0,)

    def _github_headers(self, *, binary: bool = False) -> dict[str, str]:
        headers={"Accept":"application/octet-stream" if binary else "application/vnd.github+json","User-Agent":f"DiscordPBX/{self.config.version}","X-GitHub-Api-Version":"2022-11-28"}
        token=self.secret_store.get("github_release_token", "")
        if token: headers["Authorization"]=f"Bearer {token}"
        return headers

    async def _github_latest_release(self) -> dict[str, Any]:
        repo=str(self.db.get_setting("github_repo", "") or "").strip()
        if not repo: raise ValueError("configure a GitHub release repository first (owner/repository)")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo): raise ValueError("invalid GitHub repository setting")
        url=f"https://api.github.com/repos/{repo}/releases/latest"
        timeout=aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url,headers=self._github_headers()) as resp:
                try: payload=await resp.json(content_type=None)
                except Exception: payload={}
                if resp.status==404: raise ValueError("GitHub repository/release was not found, or the token cannot access it")
                if resp.status>=400: raise ValueError(f"GitHub release lookup failed (HTTP {resp.status})")
        assets=payload.get("assets") or []
        zips=[a for a in assets if str(a.get("name","")).lower().endswith(".zip")]
        preferred=[a for a in zips if "discord-freepbx-bridge" in str(a.get("name","")).lower()]
        asset=(preferred or zips or [None])[0]
        if not asset: raise ValueError("latest GitHub release has no ZIP asset")
        tag=str(payload.get("tag_name") or payload.get("name") or "")
        return {"repo":repo,"tag":tag,"name":str(payload.get("name") or tag),"published_at":payload.get("published_at"),"html_url":payload.get("html_url"),"asset":{"name":str(asset.get("name") or "release.zip"),"size":int(asset.get("size") or 0),"api_url":str(asset.get("url") or "")},"newer":self._version_tuple(tag)>self._version_tuple(self.config.version)}

    async def _stage_github_release(self, actor: dict[str, Any]) -> dict[str, Any]:
        info=await self._github_latest_release(); asset=info["asset"]
        if not asset.get("api_url"): raise ValueError("GitHub release asset URL is missing")
        tmp=self._updates_dir/"pending.github"; final=self._updates_dir/"pending.zip"
        try: tmp.unlink()
        except FileNotFoundError: pass
        received=0; timeout=aiohttp.ClientTimeout(total=120)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(asset["api_url"],headers=self._github_headers(binary=True),allow_redirects=True) as resp:
                    if resp.status>=400: raise ValueError(f"GitHub asset download failed (HTTP {resp.status})")
                    with tmp.open("wb") as fh:
                        async for chunk in resp.content.iter_chunked(1024*1024):
                            received+=len(chunk)
                            if received>50*1024*1024: raise ValueError("GitHub update ZIP is larger than 50 MB")
                            fh.write(chunk)
            if received<=0: raise ValueError("GitHub returned an empty release asset")
            inspected=await asyncio.to_thread(self._inspect_update_zip,tmp)
            digest=await asyncio.to_thread(lambda: hashlib.sha256(tmp.read_bytes()).hexdigest())
            os.replace(tmp,final)
            meta={"filename":asset["name"],"version":inspected["version"],"sha256":digest,"bytes":received,"expanded_bytes":inspected["expanded_bytes"],"uploaded_at":time.time(),"uploaded_by":actor.get("name","system admin"),"source":"github","github_repo":info["repo"],"github_tag":info["tag"]}
            (self._updates_dir/"pending_meta.json").write_text(json.dumps(meta,indent=2),encoding="utf-8")
            self.db.audit("system.update.github_staged",actor_user_id=actor["user_id"],actor_name=actor["name"],auth_type=actor.get("auth_type","session"),entity_type="system",entity_id=inspected["version"],detail={"repo":info["repo"],"tag":info["tag"],"sha256":digest,"bytes":received})
            return meta
        finally:
            try: tmp.unlink()
            except FileNotFoundError: pass

    async def system_update_github_status(self, request):
        await self._system_admin(request)
        repo=str(self.db.get_setting("github_repo", "") or "").strip()
        base={"ok":True,"configured":bool(repo),"repo":repo,"token_configured":self.secret_store.has("github_release_token"),"current_version":self.config.version}
        if not repo:return web.json_response(base)
        try:
            base["latest"]=await self._github_latest_release();return web.json_response(base)
        except Exception as exc:
            base["error"]=str(exc);return web.json_response(base,status=200)

    async def system_update_github_stage(self, request):
        actor=await self._system_admin(request)
        try:
            meta=await self._stage_github_release(actor)
            return web.json_response({"ok":True,"pending":meta,"message":f"GitHub release {meta['version']} staged."})
        except Exception as exc:
            return web.json_response({"ok":False,"error":str(exc)},status=400)

    async def system_update_status(self, request):
        await self._system_admin(request)
        pending_meta = self._read_update_json(self._updates_dir / "pending_meta.json", {})
        agent = self._read_update_json(self._update_status_path(), {})
        return web.json_response({
            "ok": True, "current_version": self.config.version, "pending": pending_meta,
            "apply_pending": (self._updates_dir / "apply.json").exists(), "agent": agent,
            "managed_agent_ready": bool(agent.get("managed") and agent.get("project_dir")),
        })

    @staticmethod
    def _inspect_update_zip(path: Path) -> dict[str, Any]:
        required = {"bot.py", "config.py", "docker-compose.yml", "upgrade-from-current.sh"}
        with zipfile.ZipFile(path, "r") as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if not infos:
                raise ValueError("update ZIP is empty")
            if len(infos) > 5000:
                raise ValueError("update ZIP contains too many files")
            total = sum(max(0, int(i.file_size)) for i in infos)
            if total > 150 * 1024 * 1024:
                raise ValueError("expanded update is larger than 150 MB")
            names=[]
            for info in infos:
                name=info.filename.replace("\\", "/").lstrip("/")
                parts=[x for x in name.split("/") if x not in ("", ".")]
                if ".." in parts:
                    raise ValueError("unsafe path in update ZIP")
                names.append("/".join(parts))
            first_parts={n.split("/",1)[0] for n in names if "/" in n}
            root=next(iter(first_parts)) if len(first_parts)==1 else ""
            prefix=(root+"/") if root and all(n.startswith(root+"/") for n in names) else ""
            relative={n[len(prefix):] if prefix and n.startswith(prefix) else n for n in names}
            missing=sorted(required-relative)
            if missing:
                raise ValueError("not a DiscordPBX update package; missing " + ", ".join(missing))
            cfg=zf.read(prefix+"config.py").decode("utf-8",errors="replace")
            match=re.search(r'version\s*=\s*["\']([^"\']+)',cfg)
            version=match.group(1).strip() if match else "unknown"
        return {"version":version,"expanded_bytes":total}

    async def system_update_upload(self, request):
        actor=await self._system_admin(request)
        tmp=self._updates_dir/"pending.upload"; final=self._updates_dir/"pending.zip"
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        try:
            reader=await request.multipart(); received=0; filename="update.zip"; found=False
            async for part in reader:
                if part.name!="file" or not part.filename:
                    continue
                found=True; filename=Path(part.filename).name[:200]
                with tmp.open("wb") as fh:
                    while True:
                        chunk=await part.read_chunk(size=1024*1024)
                        if not chunk: break
                        received+=len(chunk)
                        if received>50*1024*1024: raise ValueError("update ZIP is larger than 50 MB")
                        fh.write(chunk)
                break
            if not found or not tmp.exists() or received<=0:
                raise ValueError("choose a DiscordPBX ZIP first")
            info=await asyncio.to_thread(self._inspect_update_zip,tmp)
            digest=await asyncio.to_thread(lambda: hashlib.sha256(tmp.read_bytes()).hexdigest())
            os.replace(tmp,final)
            meta={"filename":filename,"version":info["version"],"sha256":digest,"bytes":received,"expanded_bytes":info["expanded_bytes"],"uploaded_at":time.time(),"uploaded_by":actor.get("name","system admin")}
            (self._updates_dir/"pending_meta.json").write_text(json.dumps(meta,indent=2),encoding="utf-8")
            self.db.audit("system.update.uploaded",actor_user_id=actor["user_id"],actor_name=actor["name"],auth_type=actor.get("auth_type","session"),entity_type="system",entity_id=info["version"],detail={"sha256":digest,"bytes":received})
            return web.json_response({"ok":True,"pending":meta,"message":f"DiscordPBX {info['version']} is staged and ready to install."})
        except (ValueError,zipfile.BadZipFile) as exc:
            try: tmp.unlink()
            except FileNotFoundError: pass
            return web.json_response({"ok":False,"error":str(exc)},status=400)
        except Exception as exc:
            try: tmp.unlink()
            except FileNotFoundError: pass
            log.exception("Update upload failed")
            return web.json_response({"ok":False,"error":self._sanitize_detail(exc)},status=500)

    def _queue_update(self, actor: dict[str, Any]) -> dict[str, Any]:
        pending=self._updates_dir/"pending.zip"; meta=self._read_update_json(self._updates_dir/"pending_meta.json",{})
        if not pending.exists() or not meta: raise ValueError("stage an update first")
        agent=self._read_update_json(self._update_status_path(),{})
        if not (agent.get("managed") and agent.get("project_dir")): raise RuntimeError("managed updater is not installed on this host; run ./install-managed-updater.sh once")
        marker={"requested_at":time.time(),"requested_by":actor.get("name","system admin"),"requested_by_user_id":actor.get("user_id",""),"current_version":self.config.version,"target_version":meta.get("version","unknown"),"sha256":meta.get("sha256","")}
        tmp=self._updates_dir/"apply.json.tmp"; tmp.write_text(json.dumps(marker,indent=2),encoding="utf-8"); os.replace(tmp,self._updates_dir/"apply.json")
        self.db.audit("system.update.requested",actor_user_id=actor["user_id"],actor_name=actor["name"],auth_type=actor.get("auth_type","session"),entity_type="system",entity_id=str(meta.get("version","")),detail={"sha256":meta.get("sha256",""),"source":meta.get("source","upload")})
        return meta

    async def system_update_apply(self, request):
        actor=await self._system_admin(request)
        try: meta=self._queue_update(actor)
        except ValueError as exc:return web.json_response({"ok":False,"error":str(exc)},status=400)
        except RuntimeError as exc:return web.json_response({"ok":False,"error":str(exc)},status=409)
        return web.json_response({"ok":True,"target_version":meta.get("version"),"message":f"Update to {meta.get('version','new version')} queued. The PBX will restart automatically after backup/build/health checks."})

    async def system_update_github_install(self, request):
        actor=await self._system_admin(request)
        try:
            meta=await self._stage_github_release(actor)
            queued=self._queue_update(actor)
            return web.json_response({"ok":True,"target_version":queued.get("version"),"message":f"GitHub release {meta.get('version')} downloaded and queued for installation."})
        except ValueError as exc:return web.json_response({"ok":False,"error":str(exc)},status=400)
        except RuntimeError as exc:return web.json_response({"ok":False,"error":str(exc)},status=409)
        except Exception as exc:return web.json_response({"ok":False,"error":self._sanitize_detail(exc)},status=500)

    async def api_tokens_list(self, request): await self._system_admin(request); return web.json_response({"ok":True,"tokens":self.db.list_api_tokens()})
    async def api_token_create(self, request):
        await self._system_admin(request);body=await request.json();wid=str(body.get("workspace_id","")).strip()
        if not wid or not self.db.get_workspace(wid):
            return web.json_response({"ok":False,"error":"select a valid workspace for the API token"},status=400)
        meta,token=self.db.create_api_token(str(body.get("name","API token")),wid,body.get("capabilities",[]));return web.json_response({"ok":True,"token":token,"meta":meta,"warning":"This token is shown once. Store it securely."},status=201)
    async def api_token_revoke(self, request): await self._system_admin(request); return web.json_response({"ok":self.db.revoke_api_token(request.match_info["token_id"])})

    async def webhooks_list(self, request):
        await self._system_admin(request);rows=[]
        for x in self.db.list_webhooks():
            y=dict(x);y["secret_configured"]=bool(y.pop("secret", ""));rows.append(y)
        return web.json_response({"ok":True,"webhooks":rows})
    async def webhook_save(self, request):
        await self._system_admin(request);body=await request.json();url=str(body.get("url","")).strip()
        if not (url.startswith("https://") or url.startswith("http://")):
            return web.json_response({"ok":False,"error":"webhook URL must start with http:// or https://"},status=400)
        wid=str(body.get("workspace_id","")).strip()
        if wid and not self.db.get_workspace(wid):
            return web.json_response({"ok":False,"error":"invalid webhook workspace"},status=400)
        old=None
        if body.get("id"):old=next((x for x in self.db.list_webhooks() if x["id"]==body["id"]),None)
        if old and "secret" not in body:body["secret"]=old.get("secret","")
        body["url"]=url;body["workspace_id"]=wid
        row=self.db.upsert_webhook(body);row=dict(row);row["secret_configured"]=bool(row.pop("secret",""));return web.json_response({"ok":True,"webhook":row})
    async def webhook_delete(self, request): await self._system_admin(request); return web.json_response({"ok":self.db.delete_webhook(request.match_info["webhook_id"])})
