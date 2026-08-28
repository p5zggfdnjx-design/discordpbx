from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import discord
from discord.ext import voice_recv

from bridge import BridgeManager, DiscordAudioSink, PBXAudioSource, utc_now

log = logging.getLogger("discord-pbx.inbound-voice")


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


CONNECT_ATTEMPTS = _env_int("PBX_VOICE_CONNECT_ATTEMPTS", 3, 1, 6)
CONNECT_TIMEOUT = _env_float("PBX_VOICE_CONNECT_TIMEOUT", 10.0, 3.0, 30.0)
READY_TIMEOUT = _env_float("PBX_VOICE_READY_TIMEOUT", 15.0, 3.0, 45.0)
WATCHDOG_INTERVAL = _env_float("PBX_VOICE_WATCHDOG_INTERVAL", 2.0, 0.5, 15.0)
INBOUND_PENDING_TTL = _env_float("PBX_INBOUND_PENDING_TTL", 30.0, 5.0, 180.0)


def _bool_call(obj: object, name: str) -> bool:
    try:
        fn = getattr(obj, name, None)
        return bool(fn()) if callable(fn) else False
    except Exception:
        return False


def _healthy(vc: object | None) -> bool:
    return bool(
        vc
        and isinstance(vc, voice_recv.VoiceRecvClient)
        and _bool_call(vc, "is_connected")
        and _bool_call(vc, "is_playing")
        and _bool_call(vc, "is_listening")
    )


async def _drop(vc: object | None) -> None:
    if vc is None:
        return
    try:
        if isinstance(vc, voice_recv.VoiceRecvClient) and _bool_call(vc, "is_listening"):
            vc.stop_listening()
    except Exception:
        pass
    try:
        if _bool_call(vc, "is_playing"):
            (vc.stop_playing() if hasattr(vc, "stop_playing") else vc.stop())
    except Exception:
        pass
    failed = False
    try:
        await asyncio.wait_for(vc.disconnect(force=True), timeout=3.0)
    except Exception:
        failed = True
    if failed or not _bool_call(vc, "is_connected"):
        try:
            cleanup = getattr(vc, "cleanup", None)
            if callable(cleanup):
                cleanup()
        except Exception:
            pass


def _active_workspaces(manager: BridgeManager) -> list[str]:
    out: list[str] = []
    with manager._sessions_lock:
        for session in manager._sessions.values():
            if not getattr(session, "active", False):
                continue
            for wid in list(getattr(session, "workspace_ids", []) or []):
                wid = str(wid or "")
                if wid and wid not in out:
                    out.append(wid)
    return out


def _patch_bridge() -> None:
    if getattr(BridgeManager, "_inbound_voice_guard", False):
        return

    old_init = BridgeManager.__init__
    old_prepare = BridgeManager.prepare_inbound
    old_has_work = BridgeManager._workspace_has_voice_work
    old_disconnect = BridgeManager.disconnect_voice
    old_call_started = BridgeManager.call_started
    old_status = BridgeManager.status_dict

    def __init__(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        self._voice_watchdogs: dict[str, asyncio.Task] = {}
        self._voice_guard_loop: asyncio.AbstractEventLoop | None = None
        self._voice_guard_busy: set[str] = set()
        self._voice_guard_suppressed: set[str] = set()
        self._voice_guard_errors: dict[str, str] = {}
        self._voice_guard_recoveries: dict[str, int] = {}

    def _prune_inbound_pending(self) -> int:
        now = time.time()
        stale: list[tuple[str, dict[str, Any]]] = []
        with self._sessions_lock:
            for uid, item in list(self._pending.items()):
                if item.get("direction") != "inbound":
                    continue
                deadline = float(item.get("deadline_ts", 0) or 0)
                created = float(item.get("created_ts", 0) or 0)
                if (deadline and deadline <= now) or (created and created + INBOUND_PENDING_TTL <= now):
                    removed = self._pending.pop(uid, None)
                    if removed:
                        stale.append((uid, dict(removed)))
        for uid, item in stale:
            log.warning("Expired stale inbound registration %s before AudioSocket connected", uid)
            self.history.appendleft({
                "event": "inbound registration expired", "uuid": uid,
                "direction": "inbound", "number": item.get("number", ""),
                "detail": "AudioSocket did not connect before the inbound registration TTL",
                "time": utc_now(),
            })
        return len(stale)

    def prepare_inbound(self, call_uuid: str, number: str = "", contact_name: str = "", workspace_ids=None) -> None:
        old_prepare(self, call_uuid, number, contact_name, workspace_ids)
        now = time.time()
        with self._sessions_lock:
            item = self._pending.get(call_uuid)
            if item and item.get("direction") == "inbound":
                item["created_ts"] = now
                item["deadline_ts"] = now + INBOUND_PENDING_TTL

    def _workspace_has_voice_work(self, workspace_id: str) -> bool:
        _prune_inbound_pending(self)
        return old_has_work(self, workspace_id)

    async def ensure_voice(self, workspace_id: str = ""):
        wid, guild_id, channel_id, _ = self._workspace_voice_config(workspace_id)
        self._cancel_voice_leave(wid)
        self._voice_guard_suppressed.discard(wid)
        if not guild_id or not channel_id:
            raise RuntimeError("Discord workspace has no configured voice channel")

        lock = self._voice_locks.setdefault(wid, asyncio.Lock())
        async with lock:
            self._voice_guard_busy.add(wid)
            self._voice_guard_loop = asyncio.get_running_loop()
            try:
                try:
                    await asyncio.wait_for(self.bot.wait_until_ready(), timeout=READY_TIMEOUT)
                except asyncio.TimeoutError as exc:
                    raise RuntimeError(f"Discord bot was not ready within {READY_TIMEOUT:.0f}s") from exc

                last: Exception | None = None
                for attempt in range(1, CONNECT_ATTEMPTS + 1):
                    vc = None
                    try:
                        guild = self.bot.get_guild(guild_id)
                        if guild is None:
                            raise RuntimeError(f"Discord bot is not connected to guild {guild_id}")
                        channel = guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
                        if channel is None:
                            channel = await asyncio.wait_for(
                                self.bot.fetch_channel(channel_id), timeout=min(CONNECT_TIMEOUT, 8.0)
                            )
                        if not isinstance(channel, discord.VoiceChannel):
                            raise RuntimeError("Configured Discord voice channel must be a normal voice channel")

                        vc = guild.voice_client
                        stale = bool(
                            vc is not None
                            and (not isinstance(vc, voice_recv.VoiceRecvClient) or not _bool_call(vc, "is_connected"))
                        )
                        if stale:
                            log.warning("Discarding stale Discord voice client for workspace %s", wid or guild_id)
                            await _drop(vc)
                            vc = None

                        if vc is None:
                            log.info("Joining Discord voice for workspace %s (attempt %d/%d)", wid or guild_id, attempt, CONNECT_ATTEMPTS)
                            vc = await asyncio.wait_for(
                                channel.connect(cls=voice_recv.VoiceRecvClient, self_deaf=False, self_mute=False),
                                timeout=CONNECT_TIMEOUT,
                            )
                        elif getattr(getattr(vc, "channel", None), "id", None) != channel.id:
                            await asyncio.wait_for(vc.move_to(channel), timeout=min(CONNECT_TIMEOUT, 8.0))

                        if not _bool_call(vc, "is_connected"):
                            raise RuntimeError("Discord voice client exists but is disconnected")
                        source = self._voice_sources.setdefault(wid, PBXAudioSource(self, wid))
                        sink = self._voice_sinks.setdefault(wid, DiscordAudioSink(self, wid))
                        if not _bool_call(vc, "is_playing"):
                            vc.play(source, after=self._playback_after)
                        if not _bool_call(vc, "is_listening"):
                            vc.listen(sink, after=self._listen_after)
                        await asyncio.sleep(0)
                        if not _healthy(vc):
                            raise RuntimeError("Discord voice connected but audio workers did not start")

                        self._voice_guard_errors.pop(wid, None)
                        if stale or attempt > 1:
                            self._voice_guard_recoveries[wid] = self._voice_guard_recoveries.get(wid, 0) + 1
                        self._start_voice_watchdog(wid)
                        return vc
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        last = exc
                        self._voice_guard_errors[wid] = str(exc)[:500]
                        log.warning("Discord voice setup %s attempt %d/%d failed: %s", wid or guild_id, attempt, CONNECT_ATTEMPTS, exc)
                        await _drop(vc)
                        if attempt < CONNECT_ATTEMPTS:
                            await asyncio.sleep(min(2.0, 0.35 * (2 ** (attempt - 1))))
                raise RuntimeError(f"Discord voice setup failed after {CONNECT_ATTEMPTS} attempts: {last}")
            finally:
                self._voice_guard_busy.discard(wid)

    def _start_voice_watchdog(self, workspace_id: str) -> None:
        wid = str(workspace_id or "")
        if not wid or wid in self._voice_guard_suppressed or not self._workspace_has_voice_work(wid):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._voice_guard_loop = loop
        task = self._voice_watchdogs.get(wid)
        if task and not task.done():
            return
        self._voice_watchdogs[wid] = loop.create_task(self._voice_watchdog(wid), name=f"voice-watchdog-{wid[:24]}")

    async def _voice_watchdog(self, workspace_id: str) -> None:
        current = asyncio.current_task()
        try:
            while self._workspace_has_voice_work(workspace_id):
                await asyncio.sleep(WATCHDOG_INTERVAL)
                if workspace_id in self._voice_guard_suppressed or not self._workspace_has_voice_work(workspace_id):
                    break
                if workspace_id in self._voice_guard_busy:
                    continue
                _, guild_id, _, _ = self._workspace_voice_config(workspace_id)
                guild = self.bot.get_guild(guild_id) if guild_id and self.bot.is_ready() else None
                if _healthy(guild.voice_client if guild else None):
                    continue
                try:
                    log.warning("Voice watchdog repairing workspace %s during an active PBX call", workspace_id)
                    await self.ensure_voice(workspace_id)
                    self._voice_guard_recoveries[workspace_id] = self._voice_guard_recoveries.get(workspace_id, 0) + 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._voice_guard_errors[workspace_id] = str(exc)[:500]
                    log.error("Voice watchdog repair failed for %s: %s", workspace_id, exc)
        except asyncio.CancelledError:
            pass
        finally:
            if self._voice_watchdogs.get(workspace_id) is current:
                self._voice_watchdogs.pop(workspace_id, None)

    def _schedule_voice_repair(self, reason: str) -> None:
        loop = self._voice_guard_loop
        if loop is None or loop.is_closed():
            return
        ids = _active_workspaces(self)
        if not ids:
            return

        def schedule() -> None:
            for wid in ids:
                if wid in self._voice_guard_suppressed or wid in self._voice_guard_busy:
                    continue
                log.warning("Scheduling voice repair for %s after %s", wid, reason)
                self._start_voice_watchdog(wid)

        try:
            loop.call_soon_threadsafe(schedule)
        except RuntimeError:
            pass

    def _playback_after(self, error) -> None:
        log.error("Discord playback stopped: %s", error) if error else log.warning("Discord playback stopped unexpectedly")
        self._schedule_voice_repair("playback stop")

    def _listen_after(self, error) -> None:
        log.error("Discord receive stopped: %s", error) if error else log.warning("Discord receive stopped unexpectedly")
        self._schedule_voice_repair("receive stop")

    async def disconnect_voice(self, workspace_id=None, _from_idle_task: bool = False) -> None:
        if workspace_id is None:
            targets = set(_active_workspaces(self)) | set(self._voice_watchdogs)
        else:
            wid, _, _, _ = self._workspace_voice_config(workspace_id)
            targets = {wid} if wid else set()
        for wid in targets:
            self._voice_guard_suppressed.add(wid)
            task = self._voice_watchdogs.pop(wid, None)
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
        try:
            await old_disconnect(self, workspace_id, _from_idle_task=_from_idle_task)
        finally:
            if _from_idle_task:
                for wid in targets:
                    self._voice_guard_suppressed.discard(wid)

    async def call_started(self, session) -> bool:
        uid = session.call_uuid or "unknown"
        before = self.get_pending(uid) or {}
        active_before = sum(1 for s in self.get_sessions() if getattr(s, "active", False))
        accepted = await old_call_started(self, session)
        if accepted:
            for wid in list(getattr(session, "workspace_ids", []) or []):
                self._start_voice_watchdog(wid)
            return True

        if before.get("direction") == "inbound" and self.get_pending(uid):
            self.cancel_pending(uid)

        ids = list(getattr(session, "workspace_ids", before.get("workspace_ids", [])) or [])
        if active_before >= self.config.max_simultaneous_calls:
            detail = f"simultaneous-call limit reached ({self.config.max_simultaneous_calls})"
        elif not ids:
            detail = "no Discord workspace route is available for this call"
        else:
            errors = [f"{wid}: {self._voice_guard_errors.get(str(wid), 'voice setup failed')}" for wid in ids]
            detail = "; ".join(errors)[:500]
        direction = str(getattr(session, "direction", before.get("direction", "inbound")) or "inbound")
        number = str(getattr(session, "remote_number", before.get("number", "")) or "")
        self.history.appendleft({
            "event": "bridge failed", "uuid": uid, "direction": direction,
            "number": number, "detail": detail, "time": utc_now(),
        })
        await self._emit_event("bridge_failed", {
            "uuid": uid, "direction": direction, "number": number,
            "contact_name": str(getattr(session, "contact_name", before.get("contact_name", "")) or ""),
            "source": str(getattr(session, "source", before.get("source", "")) or ""),
            "workspace_ids": ids, "detail": detail,
        })
        return False

    def status_dict(self) -> dict:
        _prune_inbound_pending(self)
        payload = old_status(self)
        payload["voice_reliability"] = {
            "connect_attempts": CONNECT_ATTEMPTS,
            "connect_timeout_seconds": CONNECT_TIMEOUT,
            "watchdog_interval_seconds": WATCHDOG_INTERVAL,
            "watchdogs": sorted(wid for wid, task in self._voice_watchdogs.items() if task and not task.done()),
            "recoveries": dict(self._voice_guard_recoveries),
            "last_errors": dict(self._voice_guard_errors),
        }
        return payload

    BridgeManager.__init__ = __init__
    BridgeManager._prune_inbound_pending = _prune_inbound_pending
    BridgeManager.prepare_inbound = prepare_inbound
    BridgeManager._workspace_has_voice_work = _workspace_has_voice_work
    BridgeManager.ensure_voice = ensure_voice
    BridgeManager._start_voice_watchdog = _start_voice_watchdog
    BridgeManager._voice_watchdog = _voice_watchdog
    BridgeManager._schedule_voice_repair = _schedule_voice_repair
    BridgeManager._playback_after = _playback_after
    BridgeManager._listen_after = _listen_after
    BridgeManager.disconnect_voice = disconnect_voice
    BridgeManager.call_started = call_started
    BridgeManager.status_dict = status_dict
    BridgeManager._inbound_voice_guard = True


def _patch_history() -> None:
    import webui_legacy

    cls = webui_legacy.WebControlServer
    if getattr(cls, "_bridge_failure_history_guard", False):
        return
    old_event = cls._bridge_event

    async def _bridge_event(self, event: str, payload: dict) -> None:
        if event != "bridge_failed":
            return await old_event(self, event, payload)
        uid = str(payload.get("uuid", ""))
        if not uid:
            return
        detail = str(payload.get("detail", "voice bridge setup failed") or "")[:1000]
        outcome = "rejected" if "limit reached" in detail or "no Discord workspace route" in detail else "failed"
        self.call_history.finish(uid, outcome=outcome, duration=0.0, diagnostic=detail)
        self.call_history.log_activity("bridge failed", detail, uuid=uid, number=str(payload.get("number", "")))

    cls._bridge_event = _bridge_event
    cls._bridge_failure_history_guard = True


def apply() -> None:
    _patch_bridge()
    _patch_history()
