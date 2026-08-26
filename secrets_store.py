from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretStore:
    """Small encrypted secret store.

    A user-supplied PBX_MASTER_KEY is preferred. If absent, a local 0600 Fernet
    key is generated in the persistent data directory. This protects secrets from
    accidental disclosure in backups/logs and keeps them out of the browser/API.
    """

    def __init__(self, data_dir: str = "/app/data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.key_path = self.data_dir / "master.key"
        self.path = self.data_dir / "secrets.enc.json"
        self._lock = threading.RLock()
        self._fernet = Fernet(self._load_key())

    def _load_key(self) -> bytes:
        raw = os.getenv("PBX_MASTER_KEY", "").strip().encode()
        if raw:
            # Fernet keys are urlsafe base64-encoded 32-byte values.
            Fernet(raw)  # validates
            return raw
        if self.key_path.exists():
            key = self.key_path.read_bytes().strip()
            Fernet(key)
            return key
        key = Fernet.generate_key()
        self.key_path.write_bytes(key + b"\n")
        try:
            os.chmod(self.key_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return key

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in raw.items()}
        except Exception:
            return {}

    def _save(self, values: dict[str, str]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(values, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def set(self, key: str, value: str) -> None:
        with self._lock:
            values = self._load()
            if value:
                values[key] = self._fernet.encrypt(value.encode()).decode()
            else:
                values.pop(key, None)
            self._save(values)

    def get(self, key: str, default: str = "") -> str:
        with self._lock:
            token = self._load().get(key)
        if not token:
            return default
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            return default

    def has(self, key: str) -> bool:
        return bool(self.get(key, ""))

    def status(self) -> dict[str, bool]:
        values = self._load()
        return {k: bool(v) for k, v in values.items()}
