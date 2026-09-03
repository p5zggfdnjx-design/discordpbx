from __future__ import annotations

import asyncio
import audioop
import logging
import os
import shutil
import subprocess
import time
from collections import deque
from pathlib import Path

from bridge import BridgeManager

log = logging.getLogger("discord-pbx.sound-pack")


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


SOUND_PACK_ENABLED = _env_bool("DISCORD_SOUND_PACK_ENABLED", True)
SOUND_PACK_GAIN = _env_float("DISCORD_SOUND_PACK_GAIN", 1.0, 0.0, 2.0)
SOUND_PACK_DIR = Path(os.getenv("DISCORD_SOUND_PACK_DIR", "/app/assets/sounds").strip() or "/app/assets/sounds")

# Runtime-optimized 48 kHz Opus copies of the user-supplied source sounds.
SOUND_FILES = {
    "join": "start-call.opus",
    "incoming": "phone-ring.opus",
    "outbound_ring": "call-ring.opus",
    "hold": "hold-call.opus",
    "declined": "call-declined.opus",
    "hangup": "hangup.opus",
    "failed": "call-failed.opus",
}

# The supplied hold cue is intentionally much quieter than the other assets.
# Give it a modest lift without changing the source asset on disk.
EVENT_GAINS = {
    "join": 1.0,
    "incoming": 1.0,
    "outbound_ring": 1.0,
    "hold": 1.65,
    "declined": 1.0,
    "hangup": 1.0,
    "failed": 1.0,
}


def _decode_sound(manager: BridgeManager, path: Path, gain: float = 1.0) -> list[bytes]:
    """Decode an audio asset once at startup into Discord's 48 kHz stereo PCM."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to decode the Discord sound pack")
    proc = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )
    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()[:400]
        raise RuntimeError(detail or f"ffmpeg exited with {proc.returncode}")
    pcm = proc.stdout
    effective_gain = max(0.0, min(2.0, float(gain) * SOUND_PACK_GAIN))
    if effective_gain != 1.0:
        pcm = audioop.mul(pcm, 2, effective_gain)
    return manager._split_discord_frames(pcm)


def apply() -> None:
    """Install the bundled Discord-local telephony sound pack."""
    if getattr(BridgeManager, "_discord_sound_pack_guard", False):
        return

    old_init = BridgeManager.__init__
    old_ensure_voice = BridgeManager.ensure_voice
    old_set_hold = BridgeManager.set_hold
    old_cancel_pending = BridgeManager.cancel_pending
    old_fail_pending = BridgeManager.fail_pending
    old_call_ended = BridgeManager.call_ended
    old_schedule_idle = BridgeManager.schedule_voice_idle_disconnect
    old_status = BridgeManager.status_dict

    def __init__(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        self._discord_sound_frames: dict[str, list[bytes]] = {}
        self._discord_sound_sources: dict[str, str] = {}
        self._discord_sound_counts: dict[str, int] = {}
        self._discord_sound_seen_clients: dict[str, object] = {}
        self._discord_sound_hold_until: dict[str, float] = {}

        if not SOUND_PACK_ENABLED:
            return
        for event, filename in SOUND_FILES.items():
            path = SOUND_PACK_DIR / filename
            try:
                frames = _decode_sound(self, path, EVENT_GAINS.get(event, 1.0))
                self._discord_sound_frames[event] = frames
                self._discord_sound_sources[event] = str(path)
                log.info("Loaded Discord sound %s from %s (%d frames)", event, path, len(frames))
            except Exception as exc:
                log.warning("Discord sound %s unavailable (%s): %s", event, path, exc)

        # Reuse the bridge's proven cue/ringback paths so these remain Discord-only.
        incoming = self._discord_sound_frames.get("incoming")
        if incoming and bool(getattr(self.config, "inbound_chime_enabled", True)):
            self._inbound_chime_frames = incoming
        outbound_ring = self._discord_sound_frames.get("outbound_ring")
        if outbound_ring:
            self._ringback_frames = outbound_ring
            self._ringback_frame_index = 0

    def _queue_discord_sound(self, event: str, workspace_ids=None) -> bool:
        if not SOUND_PACK_ENABLED:
            return False
        frames = self._discord_sound_frames.get(str(event), [])
        if not frames:
            return False
        queued = False
        now = time.monotonic()
        duration = len(frames) * 0.020
        ids = self._normalize_workspace_ids(workspace_ids)
        with self._local_audio_lock:
            for wid in ids:
                if wid:
                    q = self._workspace_alert_frames.setdefault(wid, deque())
                    q.extend(frames)
                    self._discord_sound_hold_until[wid] = max(
                        self._discord_sound_hold_until.get(wid, 0.0), now + duration + 0.10
                    )
                else:
                    self._alert_frames.extend(frames)
                    self._discord_sound_hold_until[""] = max(
                        self._discord_sound_hold_until.get("", 0.0), now + duration + 0.10
                    )
                queued = True
        if queued:
            self._discord_sound_counts[event] = self._discord_sound_counts.get(event, 0) + 1
        return queued

    async def ensure_voice(self, workspace_id: str = ""):
        vc = await old_ensure_voice(self, workspace_id)
        wid, _, _, _ = self._workspace_voice_config(workspace_id)
        wid = str(wid or "")
        if self._discord_sound_seen_clients.get(wid) is not vc:
            self._discord_sound_seen_clients[wid] = vc
            self._queue_discord_sound("join", [wid] if wid else [""])
        return vc

    def set_hold(self, call_uuid: str, held: bool) -> bool:
        session = self.get_session(call_uuid)
        was_held = bool(getattr(session, "held", False)) if session else False
        result = old_set_hold(self, call_uuid, held)
        if result and bool(held) and not was_held:
            session = self.get_session(call_uuid)
            self._queue_discord_sound("hold", list(getattr(session, "workspace_ids", []) or []))
        return result

    def cancel_pending(self, call_uuid: str) -> None:
        pending = self.get_pending(call_uuid) or {}
        if pending:
            self._queue_discord_sound("declined", list(pending.get("workspace_ids", []) or []))
        old_cancel_pending(self, call_uuid)

    def fail_pending(self, call_uuid: str, detail: str = "") -> None:
        pending = self.get_pending(call_uuid) or {}
        if pending:
            self._queue_discord_sound("failed", list(pending.get("workspace_ids", []) or []))
        old_fail_pending(self, call_uuid, detail)

    async def call_ended(self, session) -> None:
        call_uuid = str(getattr(session, "call_uuid", "") or "")
        # Only queue once for a call the bridge still owns. This preserves the
        # existing lifecycle guard semantics and avoids duplicate end tones.
        if call_uuid and self.get_session(call_uuid) is session:
            self._queue_discord_sound("hangup", list(getattr(session, "workspace_ids", []) or []))
        await old_call_ended(self, session)

    def schedule_voice_idle_disconnect(self, workspace_id: str, delay: float | None = None) -> None:
        wid = str(workspace_id or "")
        requested = self.config.leave_voice_after_call_seconds if delay is None else float(delay)
        remaining = max(0.0, self._discord_sound_hold_until.get(wid, 0.0) - time.monotonic())
        old_schedule_idle(self, workspace_id, delay=max(float(requested), remaining))

    def status_dict(self) -> dict:
        payload = old_status(self)
        payload["discord_sound_pack"] = {
            "enabled": SOUND_PACK_ENABLED,
            "directory": str(SOUND_PACK_DIR),
            "loaded": sorted(self._discord_sound_frames),
            "sources": dict(self._discord_sound_sources),
            "plays": dict(self._discord_sound_counts),
            "gain": SOUND_PACK_GAIN,
        }
        return payload

    BridgeManager.__init__ = __init__
    BridgeManager._queue_discord_sound = _queue_discord_sound
    BridgeManager.ensure_voice = ensure_voice
    BridgeManager.set_hold = set_hold
    BridgeManager.cancel_pending = cancel_pending
    BridgeManager.fail_pending = fail_pending
    BridgeManager.call_ended = call_ended
    BridgeManager.schedule_voice_idle_disconnect = schedule_voice_idle_disconnect
    BridgeManager.status_dict = status_dict
    BridgeManager._discord_sound_pack_guard = True
