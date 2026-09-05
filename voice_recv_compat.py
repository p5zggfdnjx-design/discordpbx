from __future__ import annotations

"""Narrow compatibility guard for ``discord-ext-voice-recv``.

DiscordPBX intentionally keeps Discord connection/reconnect ownership in
``voice_lifecycle.py``.  This module does *not* own that lifecycle.  It only
hardens a known-fragile third-party packet-router hot path:

* one malformed/DAVE/Opus packet must not terminate the entire receive worker;
* DiscordPBX's comparatively expensive PCM routing must not run while the
  third-party ``PacketRouter`` lock is held.

The pinned dependency currently holds ``PacketRouter._lock`` across both packet
decode and ``sink.write`` and lets any exception escape the router loop.  The
latter causes ``stop_listening()`` and multi-second voice cut-outs while the
normal lifecycle watchdog notices and repairs the worker.
"""

import logging
import threading
import time
from typing import Any


log = logging.getLogger("discord-pbx.voice-recv-compat")
_STATE_LOCK = threading.Lock()
_PATCHED = False
_ERROR_COUNT = 0
_DELIVERED_COUNT = 0
_LAST_ERROR = ""
_LAST_ERROR_AT = 0.0
_LAST_WARNING_AT = 0.0


def _record_error(stage: str, exc: BaseException) -> None:
    global _ERROR_COUNT, _LAST_ERROR, _LAST_ERROR_AT, _LAST_WARNING_AT
    now = time.monotonic()
    text = f"{stage}: {type(exc).__name__}: {exc}"[:500]
    should_warn = False
    with _STATE_LOCK:
        _ERROR_COUNT += 1
        _LAST_ERROR = text
        _LAST_ERROR_AT = now
        if now - _LAST_WARNING_AT >= 5.0:
            _LAST_WARNING_AT = now
            should_warn = True
    if should_warn:
        log.warning("Voice receive router recovered from %s", text)
    else:
        log.debug("Voice receive router recovered from %s", text)


def _record_delivery() -> None:
    global _DELIVERED_COUNT
    with _STATE_LOCK:
        _DELIVERED_COUNT += 1


def diagnostics() -> dict[str, Any]:
    now = time.monotonic()
    with _STATE_LOCK:
        return {
            "enabled": bool(_PATCHED),
            "recovered_errors": int(_ERROR_COUNT),
            "delivered_frames": int(_DELIVERED_COUNT),
            "last_error": str(_LAST_ERROR),
            "last_error_seconds_ago": (
                round(max(0.0, now - _LAST_ERROR_AT), 2) if _LAST_ERROR_AT else None
            ),
        }


def _process_decoder(router: object, decoder: object) -> bool:
    """Process one ready decoder without holding the router lock in sink DSP.

    The pinned voice-recv jitter buffer is not independently locked, therefore
    packet extraction/decode remains under the router lock exactly as upstream
    expects.  We deliberately release that lock *before* ``sink.write`` enters
    DiscordPBX's 48 kHz mixing/resampling path.
    """

    try:
        lock = getattr(router, "_lock")
        with lock:
            data = decoder.pop_data()
            sink = getattr(router, "sink")
    except Exception as exc:
        _record_error("decode", exc)
        return False

    if data is None or getattr(data, "source", None) is None:
        return False

    try:
        sink.write(data.source, data)
    except Exception as exc:
        # A single sink/DSP failure must never kill PacketRouter.run(), which
        # would otherwise call VoiceRecvClient.stop_listening().
        _record_error("sink", exc)
        return False

    _record_delivery()
    return True


def _guarded_router_loop(self) -> None:
    while not self._end_thread.is_set():
        self.waiter.wait()
        if self._end_thread.is_set():
            break

        try:
            # Snapshot the ready set while protected, but do not hold the router
            # lock for the whole batch.  RTP ingestion gets a chance between
            # decoders and, critically, during DiscordPBX sink processing.
            with self._lock:
                decoders = tuple(self.waiter.items)
        except Exception as exc:
            _record_error("snapshot", exc)
            continue

        for decoder in decoders:
            if self._end_thread.is_set():
                break
            _process_decoder(self, decoder)


def apply() -> None:
    global _PATCHED
    if _PATCHED:
        return

    try:
        from discord.ext.voice_recv.router import PacketRouter
    except Exception as exc:
        log.warning("Voice receive compatibility guard unavailable: %s", exc)
        return

    if getattr(PacketRouter, "_discordpbx_cutout_guard", False):
        _PATCHED = True
        return

    PacketRouter._do_run = _guarded_router_loop
    PacketRouter._discordpbx_cutout_guard = True
    _PATCHED = True
    log.info(
        "Installed Discord voice receive cutout guard (per-packet error isolation; sink outside router lock)"
    )
