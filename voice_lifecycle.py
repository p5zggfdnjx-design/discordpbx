from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import discord
from discord.ext import voice_recv

from bridge import BridgeManager, DiscordAudioSink, PBXAudioSource, utc_now

log = logging.getLogger("discord-pbx.voice-lifecycle")


class ReliableBridgeManager(BridgeManager):
    """BridgeManager with one authoritative Discord voice lifecycle.

    DiscordPBX owns reconnects deliberately. ``discord.py``'s voice reconnect loop
    is disabled for bridge connections so it cannot race a second watchdog that is
    simultaneously force-disconnecting/reconnecting the same VoiceRecvClient.

    The manager also owns inbound prewarm/expiry and idle departure. Those pieces
    used to be layered onto BridgeManager at runtime by several monkeypatch guards;
    they live here as normal source now so connection state has one owner.
    """

    def __init__(self, bot, config):
        super().__init__(bot, config)
        self._voice_watchdogs: dict[str, asyncio.Task] = {}
        self._voice_busy: set[str] = set()
        self._voice_suppressed: set[str] = set()
        self._voice_errors: dict[str, str] = {}
        self._voice_recoveries: dict[str, int] = {}
        self._voice_unhealthy_since: dict[str, float] = {}
        self._voice_loop: asyncio.AbstractEventLoop | None = None
        self._inbound_prewarm_tasks: dict[str, asyncio.Task] = {}
        self._inbound_prewarm_successes: dict[str, int] = {}
        self._inbound_prewarm_failures: dict[str, int] = {}
        self._inbound_expiry_tasks: dict[str, asyncio.Task] = {}
        self._event_loop_monitor: asyncio.Task | None = None
        self._event_loop_lag_last = 0.0
        self._event_loop_lag_max = 0.0
        self._event_loop_lag_warnings = 0

    # ---------------- configuration ----------------
    @property
    def _connect_attempts(self) -> int:
        return max(1, min(6, int(getattr(self.config, "voice_connect_attempts", 3))))

    @property
    def _connect_timeout(self) -> float:
        return max(3.0, min(30.0, float(getattr(self.config, "voice_connect_timeout", 10.0))))

    @property
    def _ready_timeout(self) -> float:
        return max(3.0, min(45.0, float(getattr(self.config, "voice_ready_timeout", 15.0))))

    @property
    def _watchdog_interval(self) -> float:
        return max(0.5, min(15.0, float(getattr(self.config, "voice_watchdog_interval", 2.0))))

    @property
    def _unhealthy_grace(self) -> float:
        # A worker can transition briefly during Discord's own state updates. Do
        # not destroy the connection on one poll; require sustained unhealthiness.
        return max(1.0, min(15.0, float(getattr(self.config, "voice_unhealthy_grace", 3.0))))

    @property
    def _worker_settle_timeout(self) -> float:
        return max(0.1, min(10.0, float(getattr(self.config, "voice_worker_settle_timeout", 2.5))))

    @property
    def _worker_settle_poll(self) -> float:
        return max(0.01, min(0.5, float(getattr(self.config, "voice_worker_settle_poll", 0.05))))

    @property
    def _inbound_pending_ttl(self) -> float:
        return max(5.0, min(180.0, float(getattr(self.config, "inbound_pending_ttl", 30.0))))

    # ---------------- health helpers ----------------
    @staticmethod
    def _bool_call(obj: object, name: str) -> bool:
        try:
            fn = getattr(obj, name, None)
            return bool(fn()) if callable(fn) else False
        except Exception:
            return False

    @classmethod
    def _healthy(cls, vc: object | None) -> bool:
        return bool(
            vc
            and isinstance(vc, voice_recv.VoiceRecvClient)
            and cls._bool_call(vc, "is_connected")
            and cls._bool_call(vc, "is_playing")
            and cls._bool_call(vc, "is_listening")
        )

    async def _drop_voice_client(self, vc: object | None) -> None:
        if vc is None:
            return
        try:
            if isinstance(vc, voice_recv.VoiceRecvClient) and self._bool_call(vc, "is_listening"):
                vc.stop_listening()
        except Exception:
            pass
        try:
            if self._bool_call(vc, "is_playing"):
                vc.stop_playing() if hasattr(vc, "stop_playing") else vc.stop()
        except Exception:
            pass
        try:
            await asyncio.wait_for(vc.disconnect(force=True), timeout=3.0)
        except Exception:
            pass
        try:
            cleanup = getattr(vc, "cleanup", None)
            if callable(cleanup):
                cleanup()
        except Exception:
            pass

    async def _wait_for_workers(self, vc: object) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._worker_settle_timeout
        while True:
            if self._healthy(vc):
                return True
            if not self._bool_call(vc, "is_connected"):
                return False
            remaining = deadline - loop.time()
            if remaining <= 0:
                return self._healthy(vc)
            await asyncio.sleep(min(self._worker_settle_poll, remaining))

    # ---------------- event-loop observability ----------------
    def _start_event_loop_monitor(self) -> None:
        if self._event_loop_monitor and not self._event_loop_monitor.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._event_loop_monitor = loop.create_task(
            self._monitor_event_loop(), name="discordpbx-event-loop-lag"
        )

    async def _monitor_event_loop(self) -> None:
        loop = asyncio.get_running_loop()
        interval = 1.0
        expected = loop.time() + interval
        try:
            while True:
                await asyncio.sleep(interval)
                now = loop.time()
                lag = max(0.0, now - expected)
                self._event_loop_lag_last = lag
                self._event_loop_lag_max = max(self._event_loop_lag_max, lag)
                if lag >= 1.0:
                    self._event_loop_lag_warnings += 1
                    log.warning("Async event loop stalled for %.2fs", lag)
                expected = now + interval
        except asyncio.CancelledError:
            pass

    # ---------------- pending/inbound lifecycle ----------------
    def _prune_inbound_pending(self) -> int:
        now = time.time()
        stale: list[tuple[str, dict]] = []
        with self._sessions_lock:
            for uid, item in list(self._pending.items()):
                if item.get("direction") != "inbound":
                    continue
                deadline = float(item.get("deadline_ts", 0) or 0)
                created = float(item.get("created_ts", 0) or 0)
                if (deadline and deadline <= now) or (created and created + self._inbound_pending_ttl <= now):
                    removed = self._pending.pop(uid, None)
                    if removed:
                        stale.append((uid, dict(removed)))
        for uid, item in stale:
            self._cancel_inbound_expiry(uid)
            log.warning("Expired stale inbound registration %s before media connected", uid)
            self.history.appendleft({
                "event": "inbound registration expired",
                "uuid": uid,
                "direction": "inbound",
                "number": item.get("number", ""),
                "detail": "PBX media did not connect before the inbound registration TTL",
                "time": utc_now(),
            })
            for wid in list(item.get("workspace_ids", []) or []):
                self._stop_voice_watchdog(str(wid))
                self.schedule_voice_idle_disconnect(str(wid))
        return len(stale)

    def _workspace_has_voice_work(self, workspace_id: str) -> bool:
        """Only active sessions and non-expired pending calls keep voice resident."""
        self._prune_inbound_pending()
        self._prune_stale_outbound_pending()
        wid = str(workspace_id or "")
        if not wid:
            return False
        with self._sessions_lock:
            for session in self._sessions.values():
                if not getattr(session, "active", False):
                    continue
                if wid in list(getattr(session, "workspace_ids", []) or []):
                    return True
            for item in self._pending.values():
                if wid in list(item.get("workspace_ids", []) or []):
                    return True
        return False

    def _cancel_inbound_expiry(self, call_uuid: str) -> None:
        task = self._inbound_expiry_tasks.pop(str(call_uuid), None)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _expire_inbound_registration(self, call_uuid: str, deadline: float) -> None:
        current = asyncio.current_task()
        try:
            await asyncio.sleep(max(0.0, deadline - time.time()))
            self._prune_inbound_pending()
        except asyncio.CancelledError:
            pass
        finally:
            if self._inbound_expiry_tasks.get(call_uuid) is current:
                self._inbound_expiry_tasks.pop(call_uuid, None)

    def prepare_inbound(self, call_uuid: str, number: str = "", contact_name: str = "", workspace_ids=None) -> None:
        super().prepare_inbound(call_uuid, number, contact_name, workspace_ids)
        now = time.time()
        with self._sessions_lock:
            item = self._pending.get(call_uuid)
            if item:
                item["created_ts"] = now
                item["deadline_ts"] = now + self._inbound_pending_ttl
                ids = list(item.get("workspace_ids", []) or [])
            else:
                ids = []
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            self._cancel_inbound_expiry(call_uuid)
            self._inbound_expiry_tasks[call_uuid] = loop.create_task(
                self._expire_inbound_registration(call_uuid, now + self._inbound_pending_ttl),
                name=f"inbound-registration-expiry-{call_uuid[:8]}",
            )
            if bool(getattr(self.config, "inbound_voice_prewarm", True)):
                for wid in ids:
                    self._start_inbound_prewarm(str(wid))

    def cancel_pending(self, call_uuid: str) -> None:
        self._cancel_inbound_expiry(call_uuid)
        super().cancel_pending(call_uuid)

    # ---------------- prewarm ----------------
    def _start_inbound_prewarm(self, workspace_id: str) -> None:
        wid = str(workspace_id or "")
        if not wid:
            return
        existing = self._inbound_prewarm_tasks.get(wid)
        if existing and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._inbound_prewarm_tasks[wid] = loop.create_task(
            self._prewarm_voice(wid), name=f"inbound-voice-prewarm-{wid[:24]}"
        )

    async def _prewarm_voice(self, workspace_id: str) -> None:
        current = asyncio.current_task()
        try:
            await self.ensure_voice(workspace_id)
            self._inbound_prewarm_successes[workspace_id] = self._inbound_prewarm_successes.get(workspace_id, 0) + 1
            log.info("Inbound Discord voice prewarm ready for workspace %s", workspace_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._inbound_prewarm_failures[workspace_id] = self._inbound_prewarm_failures.get(workspace_id, 0) + 1
            self._voice_errors[workspace_id] = str(exc)[:500]
            log.warning("Inbound Discord voice prewarm failed for %s: %s", workspace_id, exc)
        finally:
            if self._inbound_prewarm_tasks.get(workspace_id) is current:
                self._inbound_prewarm_tasks.pop(workspace_id, None)

    # ---------------- authoritative Discord voice connection ----------------
    async def ensure_voice(self, workspace_id: str = ""):
        wid, guild_id, channel_id, _ = self._workspace_voice_config(workspace_id)
        self._cancel_voice_leave(wid)
        self._voice_suppressed.discard(wid)
        if not guild_id or not channel_id:
            raise RuntimeError("Discord workspace has no configured voice channel")

        self._voice_loop = asyncio.get_running_loop()
        self._start_event_loop_monitor()
        lock = self._voice_locks.setdefault(wid, asyncio.Lock())
        async with lock:
            self._voice_busy.add(wid)
            try:
                try:
                    await asyncio.wait_for(self.bot.wait_until_ready(), timeout=self._ready_timeout)
                except asyncio.TimeoutError as exc:
                    raise RuntimeError(f"Discord bot was not ready within {self._ready_timeout:.0f}s") from exc

                last: Exception | None = None
                for attempt in range(1, self._connect_attempts + 1):
                    vc = None
                    stale = False
                    try:
                        guild = self.bot.get_guild(guild_id)
                        if guild is None:
                            raise RuntimeError(f"Discord bot is not connected to guild {guild_id}")
                        channel = guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
                        if channel is None:
                            channel = await asyncio.wait_for(
                                self.bot.fetch_channel(channel_id), timeout=min(self._connect_timeout, 8.0)
                            )
                        if not isinstance(channel, discord.VoiceChannel):
                            raise RuntimeError("Configured Discord voice channel must be a normal voice channel")

                        vc = guild.voice_client
                        if vc is not None and not isinstance(vc, voice_recv.VoiceRecvClient):
                            stale = True
                        elif vc is not None and not self._bool_call(vc, "is_connected"):
                            stale = True

                        if stale:
                            log.warning("Replacing stale Discord voice client for workspace %s", wid or guild_id)
                            await self._drop_voice_client(vc)
                            vc = None

                        if vc is None:
                            log.info(
                                "Joining Discord voice for workspace %s (attempt %d/%d)",
                                wid or guild_id, attempt, self._connect_attempts,
                            )
                            # IMPORTANT: DiscordPBX owns reconnects. Leaving discord.py's
                            # reconnect loop enabled here creates two independent owners
                            # that can tear down each other's handshakes.
                            vc = await asyncio.wait_for(
                                channel.connect(
                                    cls=voice_recv.VoiceRecvClient,
                                    self_deaf=False,
                                    self_mute=False,
                                    reconnect=False,
                                ),
                                timeout=self._connect_timeout,
                            )
                        elif getattr(getattr(vc, "channel", None), "id", None) != channel.id:
                            await asyncio.wait_for(vc.move_to(channel), timeout=min(self._connect_timeout, 8.0))

                        if not self._bool_call(vc, "is_connected"):
                            raise RuntimeError("Discord voice client exists but is disconnected")

                        source = self._voice_sources.setdefault(wid, PBXAudioSource(self, wid))
                        sink = self._voice_sinks.setdefault(wid, DiscordAudioSink(self, wid))
                        if not self._bool_call(vc, "is_playing"):
                            vc.play(source, after=lambda error, w=wid: self._playback_after_for(w, error))
                        if not self._bool_call(vc, "is_listening"):
                            vc.listen(sink, after=lambda error, w=wid: self._listen_after_for(w, error))

                        if not await self._wait_for_workers(vc):
                            raise RuntimeError(
                                "Discord voice connected but audio workers did not become ready "
                                f"within {self._worker_settle_timeout:.1f}s"
                            )

                        self._voice_errors.pop(wid, None)
                        self._voice_unhealthy_since.pop(wid, None)
                        if stale or attempt > 1:
                            self._voice_recoveries[wid] = self._voice_recoveries.get(wid, 0) + 1
                        if self._workspace_has_voice_work(wid):
                            self._start_voice_watchdog(wid)
                        return vc
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        last = exc
                        self._voice_errors[wid] = str(exc)[:500]
                        log.warning(
                            "Discord voice setup %s attempt %d/%d failed: %s",
                            wid or guild_id, attempt, self._connect_attempts, exc,
                        )
                        await self._drop_voice_client(vc)
                        if attempt < self._connect_attempts:
                            await asyncio.sleep(min(2.0, 0.35 * (2 ** (attempt - 1))))
                raise RuntimeError(f"Discord voice setup failed after {self._connect_attempts} attempts: {last}")
            finally:
                self._voice_busy.discard(wid)

    def _schedule_watchdog_threadsafe(self, workspace_id: str) -> None:
        loop = self._voice_loop
        wid = str(workspace_id or "")
        if not wid or loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._start_voice_watchdog, wid)
        except RuntimeError:
            pass

    def _playback_after_for(self, workspace_id: str, error: Optional[Exception]) -> None:
        if error:
            log.error("Discord playback stopped for %s: %s", workspace_id, error)
        else:
            log.warning("Discord playback stopped unexpectedly for %s", workspace_id)
        self._schedule_watchdog_threadsafe(workspace_id)

    def _listen_after_for(self, workspace_id: str, error: Optional[Exception]) -> None:
        if error:
            log.error("Discord receive stopped for %s: %s", workspace_id, error)
        else:
            log.warning("Discord receive stopped unexpectedly for %s", workspace_id)
        self._schedule_watchdog_threadsafe(workspace_id)

    def _start_voice_watchdog(self, workspace_id: str) -> None:
        wid = str(workspace_id or "")
        if not wid or wid in self._voice_suppressed or not self._workspace_has_voice_work(wid):
            return
        task = self._voice_watchdogs.get(wid)
        if task and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._voice_watchdogs[wid] = loop.create_task(
            self._voice_watchdog(wid), name=f"voice-watchdog-{wid[:24]}"
        )

    def _stop_voice_watchdog(self, workspace_id: str) -> None:
        wid = str(workspace_id or "")
        self._voice_unhealthy_since.pop(wid, None)
        task = self._voice_watchdogs.pop(wid, None)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _voice_watchdog(self, workspace_id: str) -> None:
        current = asyncio.current_task()
        try:
            while self._workspace_has_voice_work(workspace_id):
                await asyncio.sleep(self._watchdog_interval)
                if workspace_id in self._voice_suppressed or not self._workspace_has_voice_work(workspace_id):
                    break
                if workspace_id in self._voice_busy:
                    continue
                _, guild_id, _, _ = self._workspace_voice_config(workspace_id)
                guild = self.bot.get_guild(guild_id) if guild_id and self.bot.is_ready() else None
                vc = guild.voice_client if guild else None
                if self._healthy(vc):
                    self._voice_unhealthy_since.pop(workspace_id, None)
                    continue

                now = time.monotonic()
                since = self._voice_unhealthy_since.setdefault(workspace_id, now)
                unhealthy_for = now - since
                if unhealthy_for < self._unhealthy_grace:
                    continue

                # Re-check immediately before destructive repair. A call may have
                # ended while the watchdog slept; never rejoin an idle workspace.
                if not self._workspace_has_voice_work(workspace_id):
                    break
                log.warning(
                    "Repairing Discord voice for %s after %.1fs continuously unhealthy",
                    workspace_id, unhealthy_for,
                )
                try:
                    await self.ensure_voice(workspace_id)
                    self._voice_recoveries[workspace_id] = self._voice_recoveries.get(workspace_id, 0) + 1
                    self._voice_unhealthy_since.pop(workspace_id, None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._voice_errors[workspace_id] = str(exc)[:500]
                    log.error("Discord voice repair failed for %s: %s", workspace_id, exc)
        except asyncio.CancelledError:
            pass
        finally:
            if self._voice_watchdogs.get(workspace_id) is current:
                self._voice_watchdogs.pop(workspace_id, None)
            self._voice_unhealthy_since.pop(workspace_id, None)

    # ---------------- call and idle departure coordination ----------------
    def schedule_voice_idle_disconnect(self, workspace_id: str, delay: float | None = None) -> None:
        """Schedule one stable idle timer; status/notifications cannot push it back."""
        wid = str(workspace_id or "")
        if not wid or self._workspace_has_voice_work(wid):
            return
        existing = self._leave_tasks.get(wid)
        if existing and not existing.done():
            return
        seconds = self.config.leave_voice_after_call_seconds if delay is None else max(0.0, float(delay))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._leave_tasks[wid] = loop.create_task(
            self._leave_workspace_after(wid, seconds), name=f"discord-voice-idle-{wid[:24]}"
        )

    async def call_started(self, session) -> bool:
        if getattr(session, "call_uuid", None):
            self._cancel_inbound_expiry(session.call_uuid)
        accepted = await super().call_started(session)
        if accepted:
            for wid in list(getattr(session, "workspace_ids", []) or []):
                self._start_voice_watchdog(str(wid))
        return accepted

    async def call_ended(self, session) -> None:
        # Transport sets session.active=False before calling us. With the corrected
        # work predicate that means this call no longer keeps voice resident even
        # before base history/notification awaits finish. Stop repairs and start the
        # leave clock immediately; base call_ended later sees the existing timer and
        # deliberately does not restart it.
        ids = [str(x) for x in list(getattr(session, "workspace_ids", []) or []) if str(x)]
        for wid in ids:
            if not self._workspace_has_voice_work(wid):
                self._stop_voice_watchdog(wid)
                task = self._inbound_prewarm_tasks.pop(wid, None)
                if task and task is not asyncio.current_task() and not task.done():
                    task.cancel()
                self.schedule_voice_idle_disconnect(wid)
        await super().call_ended(session)

    async def disconnect_voice(self, workspace_id: str | None = None, _from_idle_task: bool = False) -> None:
        if workspace_id is None:
            targets = set(self._voice_watchdogs) | set(self._inbound_prewarm_tasks)
            for ws in getattr(self.workspace_provider, "db", object()).list_workspaces() if self.workspace_provider else []:
                if ws.get("id"):
                    targets.add(str(ws["id"]))
        else:
            wid, _, _, _ = self._workspace_voice_config(workspace_id)
            targets = {wid} if wid else set()

        for wid in targets:
            self._voice_suppressed.add(wid)
            self._stop_voice_watchdog(wid)
            task = self._inbound_prewarm_tasks.pop(wid, None)
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()

        try:
            await super().disconnect_voice(workspace_id, _from_idle_task=_from_idle_task)
        finally:
            # Manual/idle disconnect stays suppressed until the next explicit
            # ensure_voice call, which clears suppression before joining.
            pass

    async def close_voice_lifecycle(self) -> None:
        for task in list(self._voice_watchdogs.values()):
            if task and not task.done():
                task.cancel()
        for task in list(self._inbound_prewarm_tasks.values()):
            if task and not task.done():
                task.cancel()
        for task in list(self._inbound_expiry_tasks.values()):
            if task and not task.done():
                task.cancel()
        if self._event_loop_monitor and not self._event_loop_monitor.done():
            self._event_loop_monitor.cancel()
        tasks = [
            *self._voice_watchdogs.values(),
            *self._inbound_prewarm_tasks.values(),
            *self._inbound_expiry_tasks.values(),
            *([self._event_loop_monitor] if self._event_loop_monitor else []),
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._voice_watchdogs.clear()
        self._inbound_prewarm_tasks.clear()
        self._inbound_expiry_tasks.clear()
        self._event_loop_monitor = None

    def status_dict(self) -> dict:
        self._prune_inbound_pending()
        payload = super().status_dict()
        payload["voice_reliability"] = {
            "owner": "discordpbx",
            "discord_builtin_reconnect": False,
            "connect_attempts": self._connect_attempts,
            "connect_timeout_seconds": self._connect_timeout,
            "watchdog_interval_seconds": self._watchdog_interval,
            "unhealthy_grace_seconds": self._unhealthy_grace,
            "worker_settle_timeout_seconds": self._worker_settle_timeout,
            "watchdogs": sorted(wid for wid, task in self._voice_watchdogs.items() if task and not task.done()),
            "recoveries": dict(self._voice_recoveries),
            "last_errors": dict(self._voice_errors),
            "inbound_prewarms": sorted(wid for wid, task in self._inbound_prewarm_tasks.items() if task and not task.done()),
            "inbound_prewarm_successes": dict(self._inbound_prewarm_successes),
            "inbound_prewarm_failures": dict(self._inbound_prewarm_failures),
            "event_loop_lag_seconds": round(self._event_loop_lag_last, 3),
            "event_loop_lag_max_seconds": round(self._event_loop_lag_max, 3),
            "event_loop_lag_warnings": self._event_loop_lag_warnings,
        }
        return payload
