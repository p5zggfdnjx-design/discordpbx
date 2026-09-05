from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class OperatorSettingsStore:
    """Tiny persistent server-side store for operator-console preferences."""

    # Discord voice is commonly several dB quieter than a normal telephone leg.
    # Keep the existing per-call limiter in audiosocket.py as the hard safety net,
    # but start the Discord -> caller master path with modest makeup gain so users
    # do not have to shout.  Existing installations that are still on the old
    # untouched 1.0 default are migrated once; explicitly customized values are
    # preserved.
    AUDIO_LEVEL_SCHEMA = 1
    DEFAULTS = {
        "ringback_muted": False,
        "caller_to_discord_gain": 1.0,
        "discord_to_caller_gain": 1.35,
        "inbound_chime_gain": 1.0,
        "voicemail_detection_enabled": True,
        "audio_level_schema": AUDIO_LEVEL_SCHEMA,
    }

    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(dict(self.DEFAULTS))

    def _read(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return dict(self.DEFAULTS)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return dict(self.DEFAULTS)

        # v3.3.18 audio-level migration: only raise the Discord -> caller gain
        # when the installation is still using the historical untouched default.
        # If an operator deliberately selected any other value, leave it alone.
        try:
            schema = int(raw.get("audio_level_schema", 0) or 0)
        except (TypeError, ValueError):
            schema = 0
        if schema < self.AUDIO_LEVEL_SCHEMA:
            try:
                existing_gain = float(raw.get("discord_to_caller_gain", 1.0) or 1.0)
            except (TypeError, ValueError):
                existing_gain = 1.0
            if abs(existing_gain - 1.0) < 1e-9:
                raw["discord_to_caller_gain"] = self.DEFAULTS["discord_to_caller_gain"]
            raw["audio_level_schema"] = self.AUDIO_LEVEL_SCHEMA
            # Persist the marker immediately so an intentionally restored 1.0
            # value later is not repeatedly changed on every restart.
            try:
                self._write(raw)
            except OSError:
                # Runtime can continue with the migrated in-memory value even if
                # the filesystem is temporarily read-only; a later settings save
                # will persist it.
                pass

        out = dict(self.DEFAULTS)
        out.update(raw)
        return out

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self) -> dict:
        with self._lock:
            return dict(self._read())

    def set_ringback_muted(self, muted: bool) -> dict:
        with self._lock:
            data = self._read()
            data["ringback_muted"] = bool(muted)
            self._write(data)
            return dict(data)

    def set_audio_gains(self, *, caller_to_discord: float, discord_to_caller: float, inbound_chime: float) -> dict:
        with self._lock:
            data = self._read()
            data["caller_to_discord_gain"] = float(caller_to_discord)
            data["discord_to_caller_gain"] = float(discord_to_caller)
            data["inbound_chime_gain"] = float(inbound_chime)
            data["audio_level_schema"] = self.AUDIO_LEVEL_SCHEMA
            self._write(data)
            return dict(data)

    def set_voicemail_detection(self, enabled: bool) -> dict:
        with self._lock:
            data = self._read()
            data["voicemail_detection_enabled"] = bool(enabled)
            self._write(data)
            return dict(data)
