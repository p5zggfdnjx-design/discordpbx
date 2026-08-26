from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class SoundboardStore:
    SLOT_COUNT = 5

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self.audio_dir = self.base_dir / "soundboard"
        self.meta_path = self.base_dir / "soundboard.json"
        self._lock = threading.RLock()
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        if not self.meta_path.exists():
            self._write_meta({})

    def _read_meta(self) -> dict:
        try:
            raw = json.loads(self.meta_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _write_meta(self, meta: dict) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.meta_path)

    @classmethod
    def validate_slot(cls, slot: int) -> int:
        slot = int(slot)
        if slot < 1 or slot > cls.SLOT_COUNT:
            raise ValueError(f"soundboard slot must be 1-{cls.SLOT_COUNT}")
        return slot

    def path_for(self, slot: int) -> Path:
        slot = self.validate_slot(slot)
        return self.audio_dir / f"slot-{slot}.audio"

    def list(self) -> list[dict]:
        with self._lock:
            meta = self._read_meta()
            result = []
            for slot in range(1, self.SLOT_COUNT + 1):
                entry = meta.get(str(slot), {}) if isinstance(meta.get(str(slot), {}), dict) else {}
                path = self.path_for(slot)
                result.append({
                    "slot": slot,
                    "label": str(entry.get("label") or f"Sound {slot}"),
                    "configured": path.is_file() and path.stat().st_size > 0,
                    "size_bytes": path.stat().st_size if path.is_file() else 0,
                })
            return result

    def get(self, slot: int) -> dict:
        slot = self.validate_slot(slot)
        return self.list()[slot - 1]

    def save(self, slot: int, *, label: str = "", data: bytes | None = None) -> dict:
        slot = self.validate_slot(slot)
        label = (label or "").strip()[:80] or f"Sound {slot}"
        with self._lock:
            meta = self._read_meta()
            meta[str(slot)] = {"label": label}
            self._write_meta(meta)
            if data is not None:
                path = self.path_for(slot)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, path)
        return self.get(slot)

    def delete(self, slot: int) -> None:
        slot = self.validate_slot(slot)
        with self._lock:
            try:
                self.path_for(slot).unlink()
            except FileNotFoundError:
                pass
            meta = self._read_meta()
            meta.pop(str(slot), None)
            self._write_meta(meta)
