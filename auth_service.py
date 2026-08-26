from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp
from aiohttp import web

from appdb import AppDatabase, CAPABILITIES

log = logging.getLogger("discord-pbx.auth")

SESSION_COOKIE = "pbx_session"
CSRF_HEADER = "X-CSRF-Token"


class AuthService:
    def __init__(self, bot, config, db: AppDatabase, secret_store, workspaces):
        self.bot = bot
        self.config = config
        self.db = db
        self.secret_store = secret_store
        self.workspaces = workspaces
        self.setup_code_path = Path(config.data_dir) / "setup_code.txt"
        self._ensure_setup_code()

    def _ensure_setup_code(self) -> None:
        initialized = bool(self.db.get_setting("system_initialized", False))
        if initialized:
            return
        if not self.setup_code_path.exists():
            code = f"{secrets.randbelow(1_000_000):06d}"
            self.setup_code_path.write_text(code + "\n", encoding="utf-8")
            try:
                os.chmod(self.setup_code_path, 0o600)
            except OSError:
                pass
            log.warning("PBX v3 first-run setup code: %s", code)
            log.warning("Open /setup and enter the one-time code. It is also stored at %s", self.setup_code_path)

    def setup_code_valid(self, supplied: str) -> bool:
        if bool(self.db.get_setting("system_initialized", False)):
            return False
        try:
            expected = self.setup_code_path.read_text(encoding="utf-8").strip()
        except Exception:
            return False
        return bool(expected and supplied and hmac.compare_digest(expected, str(supplied).strip()))

    def mark_initialized(self) -> None:
        self.db.set_setting("system_initialized", True)
        try:
            self.setup_code_path.unlink(missing_ok=True)
        except OSError:
            pass

    def system_admin_discord_ids(self) -> set[str]:
        ids = {str(x) for x in getattr(self.config, "bot_owner_ids", set())}
        saved = self.db.get_setting("system_admin_discord_ids", []) or []
        if isinstance(saved, str):
            saved = [x.strip() for x in saved.replace("\n", ",").split(",") if x.strip()]
        for value in saved if isinstance(saved, (list, tuple, set)) else []:
            value = str(value).strip()
            if value.isdigit():
                ids.add(value)
        return ids

    def client_id(self) -> str:
        return str(self.db.get_setting("discord_client_id", "") or self.config.discord_client_id or "")

    def client_secret(self) -> str:
        return self.secret_store.get("discord_oauth_client_secret", self.config.discord_client_secret)

    @staticmethod
    def normalize_public_base_url(value: str, *, require_https: bool = False) -> str:
        raw = str(value or "").strip().rstrip("/")
        if not raw:
            return ""
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Public URL must include http:// or https:// and a hostname")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Public URL must be an origin only, without credentials, query text, or a fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("Public URL must not include a path; use an origin such as https://pbx.example.com")
        host = (parsed.hostname or "").lower()
        if require_https and parsed.scheme.lower() != "https" and host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Discord sign-in requires an HTTPS Public URL (HTTP is allowed only for localhost)")
        return urlunsplit((parsed.scheme.lower(), parsed.netloc, "", "", "")).rstrip("/")

    @staticmethod
    def safe_return_to(value: str) -> str:
        target = str(value or "/").strip()
        return target if target.startswith("/") and not target.startswith("//") else "/"

    def public_base_url(self, request: web.Request | None = None) -> str:
        configured = str(self.db.get_setting("public_base_url", "") or self.config.public_base_url or "").rstrip("/")
        if configured:
            try:
                return self.normalize_public_base_url(configured)
            except ValueError:
                return configured
        if request is None:
            return ""
        proto = request.scheme
        host = request.host
        if self.config.trusted_proxy:
            proto = request.headers.get("X-Forwarded-Proto", proto).split(",", 1)[0].strip() or proto
            host = request.headers.get("X-Forwarded-Host", host).split(",", 1)[0].strip() or host
        return f"{proto}://{host}".rstrip("/")

    def discord_redirect_uri(self, request: web.Request) -> str:
        base = self.normalize_public_base_url(self.public_base_url(request), require_https=True)
        if not base:
            raise RuntimeError("Public URL is not configured; set it to the HTTPS address used to open the PBX panel")
        return base + "/auth/discord/callback"

    def discord_login_url(self, request: web.Request, return_to: str = "/") -> str:
        client_id = self.client_id()
        if not client_id:
            raise RuntimeError("Discord OAuth client ID is not configured")
        redirect_uri = self.discord_redirect_uri(request)
        state = self.db.create_oauth_state(self.safe_return_to(return_to))
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": "identify",
            "state": state,
        }
        return "https://discord.com/oauth2/authorize?" + urlencode(params)

    def bot_invite_url(self) -> str:
        client_id = self.client_id()
        if not client_id:
            return ""
        # View channels, Send messages, Connect, Speak, Use voice activity.
        permissions = 3148800
        return "https://discord.com/oauth2/authorize?" + urlencode({
            "client_id": client_id,
            "scope": "bot applications.commands",
            "permissions": str(permissions),
        })

    async def exchange_discord_code(self, request: web.Request, code: str) -> dict[str, Any]:
        client_id = self.client_id(); client_secret = self.client_secret()
        if not client_id or not client_secret:
            raise RuntimeError("Discord OAuth client credentials are not configured")
        redirect_uri = self.discord_redirect_uri(request)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
            async with session.post("https://discord.com/api/v10/oauth2/token", data={
                "client_id": client_id, "client_secret": client_secret,
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": redirect_uri,
            }, headers={"Content-Type": "application/x-www-form-urlencoded"}) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    reason = str(body.get("error_description") or body.get("error") or "unknown Discord error")
                    log.warning("Discord OAuth token exchange failed (%s): %s; redirect_uri=%s", resp.status, reason, redirect_uri)
                    raise RuntimeError(
                        f"Discord rejected the sign-in ({reason}). Confirm this exact redirect URI in the Discord Developer Portal: {redirect_uri}"
                    )
            access_token = str(body.get("access_token", ""))
            if not access_token:
                raise RuntimeError("Discord OAuth did not return an access token")
            async with session.get("https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bearer {access_token}"}) as resp:
                user = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise RuntimeError(f"Discord user lookup failed ({resp.status})")
        return user

    def _request_ip(self, request: web.Request) -> str:
        if self.config.trusted_proxy:
            forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
            if forwarded:
                return forwarded
        return request.remote or ""

    def set_session_cookie(self, response: web.StreamResponse, session: dict[str, str], request: web.Request) -> None:
        secure = self.public_base_url(request).lower().startswith("https://")
        response.set_cookie(
            SESSION_COOKIE, session["session_id"], httponly=True, secure=secure,
            samesite="Lax", max_age=86400, path="/",
        )
        response.set_cookie(
            "pbx_csrf", session["csrf_token"], httponly=False, secure=secure,
            samesite="Lax", max_age=86400, path="/",
        )

    async def actor_from_request(self, request: web.Request) -> dict[str, Any] | None:
        # Workspace-scoped API bearer tokens.
        authz = request.headers.get("Authorization", "")
        if authz.startswith("Bearer "):
            token = self.db.verify_api_token(authz[7:].strip())
            if token:
                return {
                    "user_id": f"api:{token['id']}", "name": token["name"], "auth_type": "api",
                    "system_admin": False, "workspace_id": token.get("workspace_id", ""),
                    "capabilities": set(token.get("capabilities", [])), "csrf": "",
                }

        sid = request.cookies.get(SESSION_COOKIE, "")
        if sid:
            row = self.db.get_session(sid)
            if row:
                if row["auth_type"] == "local":
                    return {
                        "user_id": row["user_id"], "name": "Local administrator", "auth_type": "local",
                        "system_admin": True, "workspace_id": "", "capabilities": set(CAPABILITIES),
                        "csrf": row["csrf_token"], "session_id": sid,
                    }
                if row["auth_type"] == "local_user":
                    raw_id = str(row["user_id"])
                    local_id = raw_id.split(":", 1)[1] if raw_id.startswith("localuser:") else raw_id
                    user = self.db.get_local_user(local_id)
                    if user and user.get("enabled"):
                        return {
                            "user_id": raw_id, "local_user_id": local_id,
                            "name": user.get("display_name") or user.get("username") or local_id,
                            "username": user.get("username", ""), "auth_type": "local_user",
                            "system_admin": bool(user.get("is_system_admin")), "workspace_id": "",
                            "capabilities": set(), "csrf": row["csrf_token"], "session_id": sid,
                        }
                    self.db.delete_session(sid)
                    return None
                user = self.db.get_user(row["user_id"])
                if user:
                    return {
                        "user_id": row["user_id"], "name": user.get("display_name") or user.get("username") or row["user_id"],
                        "username": user.get("username", ""), "avatar_url": user.get("avatar_url", ""),
                        "auth_type": "discord", "system_admin": bool(user.get("is_system_admin")),
                        "workspace_id": "", "capabilities": set(), "csrf": row["csrf_token"], "session_id": sid,
                    }

        # Legacy Basic mode remains a break-glass path for upgrades. It is not used
        # by Discord OAuth deployments unless WEB_AUTH_MODE is basic/hybrid.
        if self.config.web_auth_mode in {"basic", "hybrid"} and authz.startswith("Basic "):
            try:
                username, password = base64.b64decode(authz[6:]).decode().split(":", 1)
            except Exception:
                username = password = ""
            stored_pw = self.secret_store.get("legacy_web_password", self.config.web_password)
            if stored_pw and hmac.compare_digest(username, self.config.web_username) and hmac.compare_digest(password, stored_pw):
                return {"user_id": "legacy:admin", "name": username, "auth_type": "basic", "system_admin": True, "workspace_id": "", "capabilities": set(CAPABILITIES), "csrf": ""}

        if self.config.web_auth_mode == "none":
            return {"user_id": "anonymous:admin", "name": "Anonymous admin", "auth_type": "none", "system_admin": True, "workspace_id": "", "capabilities": set(CAPABILITIES), "csrf": ""}
        return None

    async def actor_capabilities(self, actor: dict[str, Any], workspace_id: str) -> set[str]:
        if actor.get("system_admin"):
            return set(CAPABILITIES)
        if actor.get("auth_type") == "api":
            if actor.get("workspace_id") and actor.get("workspace_id") != workspace_id:
                return set()
            return set(actor.get("capabilities", set()))
        if actor.get("auth_type") == "local_user":
            return self.db.local_user_capabilities(str(actor.get("local_user_id", "")), workspace_id)
        if actor.get("auth_type") == "discord":
            caps, _ = await self.workspaces.member_capabilities(actor["user_id"], workspace_id)
            return caps
        return set(actor.get("capabilities", set()))

    async def can(self, actor: dict[str, Any] | None, workspace_id: str, capability: str) -> bool:
        if not actor:
            return False
        if actor.get("system_admin"):
            return True
        caps = await self.actor_capabilities(actor, workspace_id)
        return capability in caps or "workspace_admin" in caps

    def csrf_valid(self, request: web.Request, actor: dict[str, Any]) -> bool:
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return True
        if actor.get("auth_type") in {"api", "basic", "none"}:
            return True
        supplied = request.headers.get(CSRF_HEADER, "")
        return bool(supplied and hmac.compare_digest(supplied, str(actor.get("csrf", ""))))

    async def create_discord_session(self, request: web.Request, user: dict[str, Any]) -> dict[str, str]:
        user_id = str(user.get("id", ""))
        if not user_id.isdigit():
            raise RuntimeError("Discord returned an invalid user ID")
        username = str(user.get("username", ""))
        global_name = str(user.get("global_name") or username)
        avatar = str(user.get("avatar") or "")
        avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png" if avatar else ""
        record = self.db.upsert_user(user_id, username, global_name, avatar_url)
        if user_id in self.system_admin_discord_ids() and not record.get("is_system_admin"):
            self.db.set_system_admin(user_id, True)
            record["is_system_admin"] = True
        if not record.get("is_system_admin"):
            accessible = await self.workspaces.user_workspace_access(user_id)
            if not accessible:
                raise PermissionError("Your Discord account does not have a configured PBX panel role in any workspace")
        session = self.db.create_session(user_id, "discord", ip=self._request_ip(request), user_agent=request.headers.get("User-Agent", ""))
        self.db.audit("login", actor_user_id=user_id, actor_name=global_name, auth_type="discord", detail={"method": "discord"}, ip=self._request_ip(request))
        return session

    def create_local_session(self, request: web.Request, username: str, password: str) -> dict[str, str] | None:
        if not self.db.verify_local_admin(username, password):
            return None
        session = self.db.create_session("local:admin", "local", ip=self._request_ip(request), user_agent=request.headers.get("User-Agent", ""), ttl_seconds=12 * 3600)
        self.db.audit("login", actor_user_id="local:admin", actor_name=username, auth_type="local", detail={"method": "break-glass"}, ip=self._request_ip(request))
        return session

    def create_local_user_session(self, request: web.Request, username: str, password: str) -> dict[str, str] | None:
        user = self.db.verify_local_user(username, password)
        if not user:
            return None
        user_id = f"localuser:{user['id']}"
        session = self.db.create_session(user_id, "local_user", ip=self._request_ip(request), user_agent=request.headers.get("User-Agent", ""), ttl_seconds=24 * 3600)
        self.db.audit("login", actor_user_id=user_id, actor_name=user.get("display_name") or user.get("username") or username, auth_type="local_user", detail={"method": "local-account"}, ip=self._request_ip(request))
        return session

    async def me_payload(self, actor: dict[str, Any]) -> dict[str, Any]:
        if actor.get("system_admin"):
            workspaces = self.db.list_workspaces()
            for ws in workspaces:
                ws["capabilities"] = sorted(CAPABILITIES)
        elif actor.get("auth_type") == "discord":
            workspaces = await self.workspaces.user_workspace_access(actor["user_id"])
        elif actor.get("auth_type") == "local_user":
            workspaces = self.db.local_user_workspaces(str(actor.get("local_user_id", "")))
        elif actor.get("auth_type") == "api":
            wsid = actor.get("workspace_id", "")
            workspaces = [self.db.get_workspace(wsid)] if wsid and self.db.get_workspace(wsid) else []
            for ws in workspaces:
                ws["capabilities"] = sorted(actor.get("capabilities", []))
        else:
            workspaces = []
        return {
            "authenticated": True,
            "user": {k: v for k, v in actor.items() if k not in {"csrf", "capabilities", "session_id"}},
            "system_admin": bool(actor.get("system_admin")),
            "workspaces": workspaces,
            "csrf": actor.get("csrf", ""),
        }
