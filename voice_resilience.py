from __future__ import annotations

"""Low-latency resilience on top of the canonical Discord voice lifecycle.

``ReliableBridgeManager`` remains the single owner of Discord voice connection,
reconnect, prewarm, watchdog and idle state.  This subclass adds two narrowly
scoped reliability features:

1. An explicit receive/playback worker-stop callback gets a coalesced fast repair
   instead of waiting several seconds for the polling watchdog.
2. A daemon watchdog outside the asyncio thread captures the *actual* event-loop
   Python stack while a >=1 second stall is in progress.  The old in-loop lag
   counter could prove a stall happened only after the loop resumed.
"""

import asyncio
import logging
import sys
import threading
import time
import traceback
from typing import Optional

from voice_lifecycle import ReliableBridgeManager


log = logging.getLogger("discord-pbx.voice-resilience")


def _format_thread_stack(thread_ident: int, limit: int = 30) -> str:
    try:
        frame = sys._current_frames().get(int(thread_ident))
    except Exception:
        frame = None
    if frame is None:
        return ""
    try:
        return "".join(traceback.format_stack(frame, limit=max(1, int(limit))))[-12000:]
    except Exception:
        return ""


class VoiceResilientBridgeManager(ReliableBridgeManager):
    """Canonical lifecycle plus immediate worker repair and stall forensics."""

    FAST_REPAIR_DELAY_SECONDS = 0.05
    LOOP_HEARTBEAT_SECONDS = 0.25
    LOOP_BLOCK_THRESHOLD_SECONDS = 1.0
    LOOP_STACK_RATE_LIMIT_SECONDS = 10.0

    def __init__(self, bot, config):
        super().__init__(bot, config)
        self._fast_voice_repairs: dict[str, asyncio.Task] = {}
        self._fast_voice_repair_attempts = 0
        self._fast_voice_repair_successes = 0
        self._fast_voice_repair_failures = 0

        self._event_loop_heartbeat = 0.0
        self._event_loop_thread_ident = 0
        self._event_loop_stack_stop = threading.Event()
        self._event_loop_stack_thread: threading.Thread | None = None
        self._event_loop_stack_dumps = 0
        self._event_loop_stack_last_at = 0.0
        self._event_loop_stack_last_blocked_seconds = 0.0

    # ---------------- immediate worker-stop repair ----------------
    def _schedule_fast_repair_threadsafe(self, workspace_id: str, reason: str) -> None:
        loop = self._voice_loop
        wid = str(workspace_id or "")
        if not wid or loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._start_fast_voice_repair, wid, str(reason or "worker-stop"))
        except RuntimeError:
            pass

    def _start_fast_voice_repair(self, workspace_id: str, reason: str = "worker-stop") -> None:
        wid = str(workspace_id or "")
        if not wid or wid in self._voice_suppressed or not self._workspace_has_voice_work(wid):
            return
        existing = self._fast_voice_repairs.get(wid)
        if existing and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._fast_voice_repairs[wid] = loop.create_task(
            self._fast_voice_repair(wid, reason),
            name=f"discord-fast-voice-repair-{wid[:24]}",
        )

    async def _fast_voice_repair(self, workspace_id: str, reason: str) -> None:
        wid = str(workspace_id or "")
        current = asyncio.current_task()
        try:
            await asyncio.sleep(self.FAST_REPAIR_DELAY_SECONDS)
            if wid in self._voice_suppressed or not self._workspace_has_voice_work(wid):
                return

            # A worker callback can fire while ensure_voice itself is deliberately
            # replacing a stale client.  Give that owner a short chance to finish
            # rather than racing a second repair through the same workspace lock.
            for _ in range(10):
                if wid not in self._voice_busy:
                    break
                await asyncio.sleep(0.05)
                if wid in self._voice_suppressed or not self._workspace_has_voice_work(wid):
                    return
            if wid in self._voice_busy:
                return

            self._fast_voice_repair_attempts += 1
            log.warning("Fast Discord voice repair starting for %s after %s", wid, reason)
            await self.ensure_voice(wid)
            self._fast_voice_repair_successes += 1
            log.info("Fast Discord voice repair restored %s after %s", wid, reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fast_voice_repair_failures += 1
            self._voice_errors[wid] = str(exc)[:500]
            log.error("Fast Discord voice repair failed for %s after %s: %s", wid, reason, exc)
            # The normal polling watchdog remains active as a second line of
            # defense if this immediate repair cannot recover the worker.
            self._start_voice_watchdog(wid)
        finally:
            if self._fast_voice_repairs.get(wid) is current:
                self._fast_voice_repairs.pop(wid, None)

    def _playback_after_for(self, workspace_id: str, error: Optional[Exception]) -> None:
        super()._playback_after_for(workspace_id, error)
        self._schedule_fast_repair_threadsafe(
            workspace_id, "playback-error" if error else "playback-stopped"
        )

    def _listen_after_for(self, workspace_id: str, error: Optional[Exception]) -> None:
        super()._listen_after_for(workspace_id, error)
        self._schedule_fast_repair_threadsafe(
            workspace_id, "receive-error" if error else "receive-stopped"
        )

    # ---------------- event-loop stall forensics ----------------
    def _start_event_loop_monitor(self) -> None:
        # ensure_voice invokes this from the asyncio owner thread.
        self._event_loop_thread_ident = threading.get_ident()
        self._event_loop_heartbeat = time.monotonic()
        super()._start_event_loop_monitor()

        thread = self._event_loop_stack_thread
        if thread is not None and thread.is_alive():
            return
        self._event_loop_stack_stop.clear()
        self._event_loop_stack_thread = threading.Thread(
            target=self._event_loop_stack_watchdog,
            name="discordpbx-loop-stack-watchdog",
            daemon=True,
        )
        self._event_loop_stack_thread.start()

    async def _monitor_event_loop(self) -> None:
        loop = asyncio.get_running_loop()
        interval = self.LOOP_HEARTBEAT_SECONDS
        expected = loop.time() + interval
        try:
            while True:
                await asyncio.sleep(interval)
                now_loop = loop.time()
                self._event_loop_heartbeat = time.monotonic()
                lag = max(0.0, now_loop - expected)
                self._event_loop_lag_last = lag
                self._event_loop_lag_max = max(self._event_loop_lag_max, lag)
                if lag >= self.LOOP_BLOCK_THRESHOLD_SECONDS:
                    self._event_loop_lag_warnings += 1
                    log.warning("Async event loop stalled for %.2fs", lag)
                expected = now_loop + interval
        except asyncio.CancelledError:
            pass

    def _event_loop_stack_watchdog(self) -> None:
        while not self._event_loop_stack_stop.wait(self.LOOP_HEARTBEAT_SECONDS):
            heartbeat = float(self._event_loop_heartbeat or 0.0)
            ident = int(self._event_loop_thread_ident or 0)
            if not heartbeat or not ident:
                continue
            now = time.monotonic()
            blocked = max(0.0, now - heartbeat)
            if blocked < self.LOOP_BLOCK_THRESHOLD_SECONDS:
                continue
            if now - float(self._event_loop_stack_last_at or 0.0) < self.LOOP_STACK_RATE_LIMIT_SECONDS:
                continue

            stack = _format_thread_stack(ident)
            self._event_loop_stack_last_at = now
            self._event_loop_stack_last_blocked_seconds = blocked
            self._event_loop_stack_dumps += 1
            if stack:
                # Stack formatting contains source paths/functions/lines only; no
                # request bodies, environment variables, credentials or PCM data.
                log.warning(
                    "ASYNC LOOP BLOCKED %.2fs — live event-loop stack follows:\n%s",
                    blocked,
                    stack,
                )
            else:
                log.warning(
                    "ASYNC LOOP BLOCKED %.2fs — event-loop stack unavailable for thread %s",
                    blocked,
                    ident,
                )

    async def close_voice_lifecycle(self) -> None:
        tasks = [task for task in self._fast_voice_repairs.values() if task and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._fast_voice_repairs.clear()

        self._event_loop_stack_stop.set()
        thread = self._event_loop_stack_thread
        self._event_loop_stack_thread = None
        if thread is not None and thread.is_alive():
            try:
                await asyncio.to_thread(thread.join, 0.5)
            except Exception:
                pass
        await super().close_voice_lifecycle()

    def status_dict(self) -> dict:
        payload = super().status_dict()
        reliability = payload.setdefault("voice_reliability", {})
        reliability.update(
            {
                "fast_repair_active": sorted(
                    wid for wid, task in self._fast_voice_repairs.items() if task and not task.done()
                ),
                "fast_repair_attempts": int(self._fast_voice_repair_attempts),
                "fast_repair_successes": int(self._fast_voice_repair_successes),
                "fast_repair_failures": int(self._fast_voice_repair_failures),
                "event_loop_stack_watchdog": bool(
                    self._event_loop_stack_thread is not None
                    and self._event_loop_stack_thread.is_alive()
                ),
                "event_loop_stack_dumps": int(self._event_loop_stack_dumps),
                "event_loop_last_stack_blocked_seconds": round(
                    float(self._event_loop_stack_last_blocked_seconds or 0.0), 3
                ),
                "event_loop_last_stack_seconds_ago": (
                    round(max(0.0, time.monotonic() - self._event_loop_stack_last_at), 2)
                    if self._event_loop_stack_last_at
                    else None
                ),
            }
        )
        try:
            from voice_recv_compat import diagnostics as voice_recv_diagnostics

            reliability["voice_recv_router"] = voice_recv_diagnostics()
        except Exception:
            reliability["voice_recv_router"] = {"enabled": False}
        return payload
