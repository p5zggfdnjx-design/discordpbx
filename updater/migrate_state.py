from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from appdb import AppDatabase
from secrets_store import SecretStore


_MISSING = object()

SETTING_MAP: dict[str, tuple[str, str]] = {
    "PUBLIC_BASE_URL": ("public_base_url", "str"),
    "DISCORD_CLIENT_ID": ("discord_client_id", "str"),
    "ASTERISK_AMI_HOST": ("asterisk_host", "str"),
    "ASTERISK_AMI_PORT": ("asterisk_port", "int"),
    "ASTERISK_AMI_USER": ("asterisk_user", "str"),
    "ASTERISK_DIAL_CONTEXT": ("asterisk_dial_context", "str"),
    "AUDIOSOCKET_ADVERTISE_HOST": ("audiosocket_advertise_host", "str"),
    "MAX_SIMULTANEOUS_CALLS": ("max_simultaneous_calls", "int"),
    "WEB_AUTH_MODE": ("web_auth_mode", "str"),
    "GITHUB_REPO": ("github_repo", "str"),
}

SECRET_MAP = {
    "DISCORD_TOKEN": "discord_bot_token",
    "DISCORD_CLIENT_SECRET": "discord_oauth_client_secret",
    "ASTERISK_AMI_SECRET": "asterisk_ami_secret",
    "PBX_INGRESS_TOKEN": "pbx_ingress_token",
    "WEB_PASSWORD": "legacy_web_password",
}


def _convert(raw: str, kind: str) -> Any:
    if kind == "int":
        return int(raw)
    return raw


def run() -> dict[str, Any]:
    """Idempotently migrate legacy/env configuration into persistent v3 state.

    Existing database settings and encrypted secrets win; environment values are
    imported only when the persistent equivalent is missing. AppDatabase startup
    performs schema migrations before the PBX starts accepting requests.
    """
    data_dir = Path(os.getenv("DATA_DIR", "/app/data").strip() or "/app/data")
    data_dir.mkdir(parents=True, exist_ok=True)
    db = AppDatabase(str(data_dir / "pbx_app.sqlite3"))
    secrets = SecretStore(str(data_dir))

    imported_settings: list[str] = []
    imported_secrets: list[str] = []

    for env_name, (setting_name, kind) in SETTING_MAP.items():
        raw = os.getenv(env_name, "").strip()
        if not raw:
            continue
        existing = db.get_setting(setting_name, _MISSING)
        if existing is not _MISSING:
            continue
        try:
            value = _convert(raw, kind)
        except (TypeError, ValueError):
            continue
        db.set_setting(setting_name, value)
        imported_settings.append(setting_name)

    owner_ids = [x.strip() for x in os.getenv("BOT_OWNER_IDS", "").replace("\n", ",").split(",") if x.strip().isdigit()]
    if owner_ids and db.get_setting("system_admin_discord_ids", _MISSING) is _MISSING:
        db.set_setting("system_admin_discord_ids", owner_ids)
        imported_settings.append("system_admin_discord_ids")

    for env_name, secret_name in SECRET_MAP.items():
        raw = os.getenv(env_name, "").strip()
        if raw and not secrets.has(secret_name):
            secrets.set(secret_name, raw)
            imported_secrets.append(secret_name)

    target = os.getenv("PBX_TARGET_VERSION", "").strip()
    result = {
        "ran_at": time.time(),
        "target_version": target,
        "imported_settings": imported_settings,
        "imported_secrets": imported_secrets,
    }
    db.set_setting("last_update_migration", result)
    return result


def main() -> int:
    result = run()
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
