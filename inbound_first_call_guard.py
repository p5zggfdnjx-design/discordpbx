from __future__ import annotations

import asyncio
import logging
import os

import discord
from discord.ext import voice_recv

import inbound_voice_guard as voice_guard
from bridge import BridgeManager, DiscordAudioSink, PBXAudioSource

log = logging.getLogger("discord-pbx.inbound-first-call")


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# discord.py voice connections can report connected before the playback/receive
# workers have fully transitioned to their running state. The older reliability
# guard checked those flags after only one event-loop turn and could tear down a
# perfectly valid cold connection. That is exactly the kind of race that makes
# the first inbound call fail while the second/third succeeds.
WORKER_SETTLE_TIMEOUT = _env_float("PBX_VOICE_WORKER_SETTLE_TIMEOUT", 2.5, 0.1, 10.0)
WORKER_SETTLE_POLL = _env_float("PBX_VOICE_WORKER_SETTLE_POLL", 0.05, 0.01, 0.5)
PREWARM_ENABLED = _env_bool("PBX_INBOUND_VOICE_PREWARM", True)


async def _wait_for_workers(vc: object) -> bool:
    """Wait briefly for a connected Discord voice client's audio workers.

    A real disconnect still fails immediately; only the startup transition gets
    a bounded grace period. This keeps the existing retry/watchdog semantics but
    avoids destroying a healthy cold-start connection too early.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WORKER_SETTLE_TIMEOUT
    while True:
        if voice_guard._healthy(vc):
            return True
        if not voice_guard._bool_call(vc, "is_connected"):
            return False
        remaining = deadline - loop.time()
        if remaining <= 0:
            return voice_guard._healthy(vc)
        await asyncio.sleep(min(WORKER_SETTLE_POLL, remaining))


def apply() -> None:
    """Harden cold-start inbound pickup without changing routing or PBX semantics."""
    if getattr(BridgeManager, "_inbound_first_call_guard", False):
        return

    old_init = BridgeManager.__init__
    old_prepare = BridgeManager.prepare_inbound
    old_disconnect = BridgeManager.disconnect_voice
    old_status = BridgeManager.status_dict

    def __init__(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        self._inbound_prewarm_tasks: dict[str, asyncio.Task] = {}
        self._inbound_prewarm_successes: dict[str, int] = {}
        self._inbound_prewarm_failures: dict[str, int] = {}

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
                    await asyncio.wait_for(self.bot.wait_until_ready(), timeout=voice_guard.READY_TIMEOUT)
                except asyncio.TimeoutError as exc:
                    raise RuntimeError(
                        f"Discord bot was not ready within {voice_guard.READY_TIMEOUT:.0f}s"
                    ) from exc

                last: Exception | None = None
                for attempt in range(1, voice_guard.CONNECT_ATTEMPTS + 1):
                    vc = None
                    try:
                        guild = self.bot.get_guild(guild_id)
                        if guild is None:
                            raise RuntimeError(f"Discord bot is not connected to guild {guild_id}")
                        channel = guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
                        if channel is None:
                            channel = await asyncio.wait_for(
                                self.bot.fetch_channel(channel_id),
                                timeout=min(voice_guard.CONNECT_TIMEOUT, 8.0),
                            )
                        if not isinstance(channel, discord.VoiceChannel):
                            raise RuntimeError("Configured Discord voice channel must be a normal voice channel")

                        vc = guild.voice_client
                        stale = bool(
                            vc is not None
                            and (
                                not isinstance(vc, voice_recv.VoiceRecvClient)
                                or not voice_guard._bool_call(vc, "is_connected")
                            )
                        )
                        if stale:
                            log.warning(
                                "Discarding stale Discord voice client for workspace %s",
                                wid or guild_id,
                            )
                            await voice_guard._drop(vc)
                            vc = None

                        if vc is None:
                            log.info(
                                "Joining Discord voice for workspace %s (attempt %d/%d)",
                                wid or guild_id,
                                attempt,
                                voice_guard.CONNECT_ATTEMPTS,
                            )
                            vc = await asyncio.wait_for(
                                channel.connect(
                                    cls=voice_recv.VoiceRecvClient,
                                    self_deaf=False,
                                    self_mute=False,
                                ),
                                timeout=voice_guard.CONNECT_TIMEOUT,
                            )
                        elif getattr(getattr(vc, "channel", None), "id", None) != channel.id:
                            await asyncio.wait_for(
                                vc.move_to(channel),
                                timeout=min(voice_guard.CONNECT_TIMEOUT, 8.0),
                            )

                        if not voice_guard._bool_call(vc, "is_connected"):
                            raise RuntimeError("Discord voice client exists but is disconnected")

                        source = self._voice_sources.setdefault(wid, PBXAudioSource(self, wid))
                        sink = self._voice_sinks.setdefault(wid, DiscordAudioSink(self, wid))
                        if not voice_guard._bool_call(vc, "is_playing"):
                            vc.play(source, after=self._playback_after)
                        if not voice_guard._bool_call(vc, "is_listening"):
                            vc.listen(sink, after=self._listen_after)

                        if not await _wait_for_workers(vc):
                            raise RuntimeError(
                                "Discord voice connected but audio workers did not become ready "
                                f"within {WORKER_SETTLE_TIMEOUT:.1f}s"
                            )

                        self._voice_guard_errors.pop(wid, None)
                        if stale or attempt > 1:
                            self._voice_guard_recoveries[wid] = (
                                self._voice_guard_recoveries.get(wid, 0) + 1
                            )
                        self._start_voice_watchdog(wid)
                        return vc
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        last = exc
                        self._voice_guard_errors[wid] = str(exc)[:500]
                        log.warning(
                            "Discord voice setup %s attempt %d/%d failed: %s",
                            wid or guild_id,
                            attempt,
                            voice_guard.CONNECT_ATTEMPTS,
                            exc,
                        )
                        await voice_guard._drop(vc)
                        if attempt < voice_guard.CONNECT_ATTEMPTS:
                            await asyncio.sleep(min(2.0, 0.35 * (2 ** (attempt - 1))))

                raise RuntimeError(
                    f"Discord voice setup failed after {voice_guard.CONNECT_ATTEMPTS} attempts: {last}"
                )
            finally:
                self._voice_guard_busy.discard(wid)

    async def _prewarm_voice(self, workspace_id: str) -> None:
        wid = str(workspace_id or "")
        current = asyncio.current_task()
        try:
            await self.ensure_voice(wid)
            self._inbound_prewarm_successes[wid] = self._inbound_prewarm_successes.get(wid, 0) + 1
            log.info("Inbound Discord voice prewarm ready for workspace %s", wid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Prewarm is opportunistic. The real AudioSocket call still gets the
            # normal ensure_voice retry path, so a failed prewarm must never reject
            # an otherwise valid inbound call.
            self._inbound_prewarm_failures[wid] = self._inbound_prewarm_failures.get(wid, 0) + 1
            self._voice_guard_errors[wid] = str(exc)[:500]
            log.warning("Inbound Discord voice prewarm failed for %s: %s", wid, exc)
        finally:
            if self._inbound_prewarm_tasks.get(wid) is current:
                self._inbound_prewarm_tasks.pop(wid, None)

    def _start_inbound_prewarm(self, workspace_id: str) -> None:
        if not PREWARM_ENABLED:
            return
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
            self._prewarm_voice(wid),
            name=f"inbound-voice-prewarm-{wid[:24]}",
        )

    def prepare_inbound(
        self,
        call_uuid: str,
        number: str = "",
        contact_name: str = "",
        workspace_ids=None,
    ) -> None:
        old_prepare(self, call_uuid, number, contact_name, workspace_ids)
        pending = self.get_pending(call_uuid) or {}
        for wid in list(pending.get("workspace_ids", []) or []):
            self._start_inbound_prewarm(str(wid))

    async def disconnect_voice(self, workspace_id=None, _from_idle_task: bool = False) -> None:
        if workspace_id is None:
            targets = list(self._inbound_prewarm_tasks)
        else:
            wid, _, _, _ = self._workspace_voice_config(workspace_id)
            targets = [wid] if wid else []
        for wid in targets:
            task = self._inbound_prewarm_tasks.pop(wid, None)
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
        await old_disconnect(self, workspace_id, _from_idle_task=_from_idle_task)

    def status_dict(self) -> dict:
        payload = old_status(self)
        reliability = payload.setdefault("voice_reliability", {})
        reliability.update(
            {
                "worker_settle_timeout_seconds": WORKER_SETTLE_TIMEOUT,
                "inbound_prewarm_enabled": PREWARM_ENABLED,
                "inbound_prewarms": sorted(
                    wid
                    for wid, task in self._inbound_prewarm_tasks.items()
                    if task and not task.done()
                ),
                "inbound_prewarm_successes": dict(self._inbound_prewarm_successes),
                "inbound_prewarm_failures": dict(self._inbound_prewarm_failures),
            }
        )
        return payload

    BridgeManager.__init__ = __init__
    BridgeManager.ensure_voice = ensure_voice
    BridgeManager._prewarm_voice = _prewarm_voice
    BridgeManager._start_inbound_prewarm = _start_inbound_prewarm
    BridgeManager.prepare_inbound = prepare_inbound
    BridgeManager.disconnect_voice = disconnect_voice
    BridgeManager.status_dict = status_dict
    BridgeManager._inbound_first_call_guard = True
