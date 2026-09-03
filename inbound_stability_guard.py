from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid as uuidlib
from collections import deque

from aiohttp import web

from bridge import BridgeManager
import inbound_voice_guard as voice_guard

log = logging.getLogger("discord-pbx.inbound-stability")


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


ENABLED = _env_bool("PBX_INBOUND_STABILITY_GUARD", True)
# The bundled FreePBX example has a 2-second CURL timeout. Keep the default wait
# comfortably below it so metadata registration still returns to the dialplan.
HANDSHAKE_WAIT_SECONDS = _env_float("PBX_INBOUND_HANDSHAKE_WAIT", 1.25, 0.0, 10.0)
HANDSHAKE_POLL_SECONDS = _env_float("PBX_INBOUND_HANDSHAKE_POLL", 0.05, 0.01, 0.5)
HANGUP_SOUND_WINDOW_SECONDS = _env_float("PBX_HANGUP_SOUND_WINDOW", 60.0, 5.0, 300.0)
HANGUP_SOUND_BURST_LIMIT = _env_int("PBX_HANGUP_SOUND_BURST_LIMIT", 3, 0, 50)


def _allow_hangup_cue(history: deque[float], now: float) -> bool:
    """Allow only the first few hangup sounds in a rolling time window."""
    cutoff = float(now) - HANGUP_SOUND_WINDOW_SECONDS
    while history and history[0] < cutoff:
        history.popleft()
    allowed = HANGUP_SOUND_BURST_LIMIT > 0 and len(history) < HANGUP_SOUND_BURST_LIMIT
    # Retain suppressed events too so a sustained burst stays quiet.
    history.append(float(now))
    return allowed


def _voice_is_ready(server, workspace_id: str) -> bool:
    try:
        bridge = server.bot.bridge
        _, guild_id, _, _ = bridge._workspace_voice_config(workspace_id)
        guild = server.bot.get_guild(guild_id) if guild_id and server.bot.is_ready() else None
        return bool(guild and voice_guard._healthy(getattr(guild, "voice_client", None)))
    except Exception:
        return False


async def _wait_for_selected_voice(server, workspace_ids: list[str]) -> bool:
    ids = [str(x) for x in workspace_ids if str(x)]
    if not ids:
        return False
    if any(_voice_is_ready(server, wid) for wid in ids):
        return True
    if HANDSHAKE_WAIT_SECONDS <= 0:
        return False

    loop = asyncio.get_running_loop()
    deadline = loop.time() + HANDSHAKE_WAIT_SECONDS
    while loop.time() < deadline:
        remaining = max(0.0, deadline - loop.time())
        await asyncio.sleep(min(HANDSHAKE_POLL_SECONDS, remaining))
        if any(_voice_is_ready(server, wid) for wid in ids):
            return True
    return any(_voice_is_ready(server, wid) for wid in ids)


def apply() -> None:
    """Synchronize inbound prewarm with FreePBX and quiet rapid hangup cues.

    Inbound registration used to resolve routing twice: once to select the target
    and again to build the status/audit label. The second pass can perform fresh
    Discord presence work and consumes a meaningful part of FreePBX's short CURL
    callback budget. This guard performs one routing decision, immediately starts
    the existing prewarm, then gives that prewarm a bounded head start before the
    callback returns. A timeout never rejects the call; AudioSocket keeps its normal
    retry/self-heal path.
    """
    if getattr(BridgeManager, "_inbound_stability_guard", False):
        return

    old_bridge_init = BridgeManager.__init__
    old_queue_sound = getattr(BridgeManager, "_queue_discord_sound", None)
    old_status = BridgeManager.status_dict

    def bridge_init(self, *args, **kwargs):
        old_bridge_init(self, *args, **kwargs)
        self._hangup_sound_history: deque[float] = deque(maxlen=512)
        self._inbound_stability_metrics = {
            "handshake_ready": 0,
            "handshake_timeout": 0,
            "hangup_cues_suppressed": 0,
        }

    def _queue_discord_sound(self, event: str, workspace_ids=None) -> bool:
        if old_queue_sound is None:
            return False
        if str(event) == "hangup":
            if not _allow_hangup_cue(self._hangup_sound_history, time.monotonic()):
                self._inbound_stability_metrics["hangup_cues_suppressed"] += 1
                return False
        return bool(old_queue_sound(self, event, workspace_ids))

    def status_dict(self) -> dict:
        payload = old_status(self)
        now = time.monotonic()
        cutoff = now - HANGUP_SOUND_WINDOW_SECONDS
        while self._hangup_sound_history and self._hangup_sound_history[0] < cutoff:
            self._hangup_sound_history.popleft()
        payload["inbound_stability"] = {
            "enabled": ENABLED,
            "handshake_wait_seconds": HANDSHAKE_WAIT_SECONDS,
            "hangup_sound_window_seconds": HANGUP_SOUND_WINDOW_SECONDS,
            "hangup_sound_burst_limit": HANGUP_SOUND_BURST_LIMIT,
            "hangups_in_window": len(self._hangup_sound_history),
            **dict(self._inbound_stability_metrics),
        }
        return payload

    BridgeManager.__init__ = bridge_init
    if old_queue_sound is not None:
        BridgeManager._queue_discord_sound = _queue_discord_sound
    BridgeManager.status_dict = status_dict

    import webui_v3

    cls = webui_v3.WebControlServer

    async def inbound_register(self, request):
        """Fast single-pass inbound registration with bounded Discord prewarm wait."""
        try:
            call_uuid = str(request.query.get("uuid", "")).strip()
            uuidlib.UUID(call_uuid)
            raw = str(request.query.get("number", "")).strip()
            number = self.bot.ami.normalize_number(raw) if raw else ""

            selected = await self.workspaces.resolve_inbound_workspaces()
            wsids = [str(x["id"]) for x in selected]
            default = wsids[0] if wsids else ""
            contact = self.contacts.find_by_number(number, default) if number else None
            name = (contact or {}).get("name", "")

            # prepare_inbound is already wrapped by inbound_first_call_guard, so
            # this line starts voice prewarm without blocking the HTTP callback.
            self.bot.bridge.prepare_inbound(call_uuid, number, name, workspace_ids=wsids)

            cfg = self.db.get_setting("inbound_routing", {}) or {}
            mode = str(cfg.get("mode", "auto") or "auto")
            try:
                expires = float(cfg.get("override_expires", 0) or 0)
            except (TypeError, ValueError):
                expires = 0.0
            if expires and expires <= time.time():
                mode = "auto"

            self.call_history.start_call(
                uuid=call_uuid,
                direction="inbound",
                number=number,
                contact_name=name,
                source="inbound",
                state="incoming",
                workspace_ids=wsids,
                route_reason=f"{mode} -> {','.join(wsids) or 'none'}",
            )
            self.db.audit(
                "call.inbound.registered",
                actor_user_id="pbx:ingress",
                actor_name="FreePBX",
                auth_type="ingress",
                workspace_id=default,
                entity_type="call",
                entity_id=call_uuid,
                call_uuid=call_uuid,
                number=number,
                detail={"workspaces": wsids, "routing_mode": mode},
            )
            await self._publish(
                "call.incoming",
                {"uuid": call_uuid, "number": number, "contact_name": name, "workspace_ids": wsids},
            )

            if ENABLED and wsids and HANDSHAKE_WAIT_SECONDS > 0:
                started = time.monotonic()
                ready = await _wait_for_selected_voice(self, wsids)
                elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
                metrics = self.bot.bridge._inbound_stability_metrics
                if ready:
                    metrics["handshake_ready"] += 1
                    log.info("Inbound Discord prewarm ready before PBX callback returned (%sms)", elapsed_ms)
                else:
                    # Do not fail the inbound call. AudioSocket call_started still
                    # owns the normal multi-attempt voice recovery after return.
                    metrics["handshake_timeout"] += 1
                    log.warning(
                        "Inbound Discord prewarm not ready after %.2fs; allowing AudioSocket retry path",
                        HANDSHAKE_WAIT_SECONDS,
                    )

            return web.json_response({"ok": True, "workspace_ids": wsids, "route_mode": mode})
        except asyncio.CancelledError:
            # FreePBX may abort CURL at its own deadline. Prewarm is an independent
            # task and intentionally continues for the imminent AudioSocket leg.
            raise
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    cls.inbound_register = inbound_register
    BridgeManager._inbound_stability_guard = True
