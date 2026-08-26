from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class OperatorSettingsStore:
    """Tiny persistent server-side store for operator-console preferences."""

    DEFAULTS = {
        "ringback_muted": False,
        "caller_to_discord_gain": 1.0,
        "discord_to_caller_gain": 1.0,
        "inbound_chime_gain": 1.0,
        "voicemail_detection_enabled": True,
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
            self._write(data)
            return dict(data)

    def set_voicemail_detection(self, enabled: bool) -> dict:
        with self._lock:
            data = self._read()
            data["voicemail_detection_enabled"] = bool(enabled)
            self._write(data)
            return dict(data)

