from __future__ import annotations

import audioop
import math
import os
import wave
from collections import deque

from bridge import BridgeManager


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


JOIN_CHIME_ENABLED = _env_bool("DISCORD_JOIN_CHIME_ENABLED", True)
JOIN_CHIME_GAIN = _env_float("DISCORD_JOIN_CHIME_GAIN", 0.8, 0.0, 2.0)
JOIN_CHIME_FILE = os.getenv("DISCORD_JOIN_CHIME_FILE", "").strip()


def _envelope(pos: float, duration: float) -> float:
    attack = min(0.018, duration * 0.25)
    release = min(0.055, duration * 0.4)
    if pos < attack:
        return max(0.0, pos / max(attack, 0.001))
    if pos > duration - release:
        return max(0.0, (duration - pos) / max(release, 0.001))
    return 1.0


def _build_builtin_chime_frames(manager: BridgeManager) -> list[bytes]:
    """Create a short original Skype-style bubbly join chime.

    This is intentionally synthesized rather than bundling Microsoft's Skype
    sound asset. A user-supplied WAV can be selected with DISCORD_JOIN_CHIME_FILE.
    """
    rate = 48000
    notes = [
        (0.000, 0.120, 659.25),
        (0.095, 0.145, 830.61),
        (0.205, 0.170, 987.77),
        (0.340, 0.205, 1318.51),
    ]
    total = 0.60
    samples = int(rate * total)
    out = bytearray(samples * 4)
    base_gain = 0.16 * JOIN_CHIME_GAIN

    for i in range(samples):
        t = i / rate
        value = 0.0
        for start, duration, freq in notes:
            pos = t - start
            if pos < 0.0 or pos >= duration:
                continue
            env = _envelope(pos, duration)
            phase = (
                2.0 * math.pi * freq * pos
                + 0.22 * math.sin(2.0 * math.pi * 5.2 * pos)
            )
            tone = math.sin(phase) + 0.22 * math.sin(phase * 2.0)
            value += base_gain * env * tone
        value = max(-0.95, min(0.95, value))
        sample = int(value * 32767)
        encoded = int(sample).to_bytes(2, "little", signed=True)
        off = i * 4
        out[off:off + 2] = encoded
        out[off + 2:off + 4] = encoded

    return manager._split_discord_frames(bytes(out))


def _load_chime_frames(manager: BridgeManager) -> tuple[list[bytes], str]:
    if JOIN_CHIME_FILE:
        try:
            with wave.open(JOIN_CHIME_FILE, "rb") as wav:
                if (
                    wav.getnchannels() != 2
                    or wav.getsampwidth() != 2
                    or wav.getframerate() != 48000
                ):
                    raise ValueError("join chime WAV must be 48 kHz, 16-bit, stereo PCM")
                pcm = wav.readframes(wav.getnframes())
            if JOIN_CHIME_GAIN != 1.0:
                pcm = audioop.mul(pcm, 2, JOIN_CHIME_GAIN)
            return manager._split_discord_frames(pcm), JOIN_CHIME_FILE
        except Exception:
            pass
    return _build_builtin_chime_frames(manager), "builtin-skype-style"


def apply() -> None:
    if getattr(BridgeManager, "_discord_join_chime_guard", False):
        return

    old_init = BridgeManager.__init__
    old_ensure_voice = BridgeManager.ensure_voice
    old_status = BridgeManager.status_dict

    def __init__(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        frames, source = _load_chime_frames(self)
        self._discord_join_chime_frames = frames
        self._discord_join_chime_source = source
        self._discord_join_chime_seen_clients: dict[str, object] = {}
        self._discord_join_chime_plays: dict[str, int] = {}

    def _queue_discord_join_chime(self, workspace_id: str, vc: object) -> bool:
        if not JOIN_CHIME_ENABLED or not self._discord_join_chime_frames:
            return False
        wid = str(workspace_id or "")
        if self._discord_join_chime_seen_clients.get(wid) is vc:
            return False
        self._discord_join_chime_seen_clients[wid] = vc
        with self._local_audio_lock:
            if wid:
                q = self._workspace_alert_frames.setdefault(wid, deque())
                q.extend(self._discord_join_chime_frames)
            else:
                self._alert_frames.extend(self._discord_join_chime_frames)
        self._discord_join_chime_plays[wid] = self._discord_join_chime_plays.get(wid, 0) + 1
        return True

    async def ensure_voice(self, workspace_id: str = ""):
        vc = await old_ensure_voice(self, workspace_id)
        wid, _, _, _ = self._workspace_voice_config(workspace_id)
        self._queue_discord_join_chime(wid, vc)
        return vc

    def status_dict(self) -> dict:
        payload = old_status(self)
        payload["discord_join_chime"] = {
            "enabled": JOIN_CHIME_ENABLED,
            "source": self._discord_join_chime_source,
            "gain": JOIN_CHIME_GAIN,
            "plays": dict(self._discord_join_chime_plays),
        }
        return payload

    BridgeManager.__init__ = __init__
    BridgeManager._queue_discord_join_chime = _queue_discord_join_chime
    BridgeManager.ensure_voice = ensure_voice
    BridgeManager.status_dict = status_dict
    BridgeManager._discord_join_chime_guard = True
