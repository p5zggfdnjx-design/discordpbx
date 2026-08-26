from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


PERSISTENT_APP_TABLES = (
    "settings",
    "workspaces",
    "workspace_roles",
    "users",
    "local_admin",
    "local_users",
    "local_user_workspaces",
    "api_tokens",
    "webhooks",
    "dnc_numbers",
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _json_list_count(path: Path) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    return len(raw) if isinstance(raw, list) else 0


def _table_counts(path: Path, tables: tuple[str, ...] | None = None) -> dict[str, int]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    out: dict[str, int] = {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        if tables is None:
            rows = con.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            names = [str(r[0]) for r in rows]
        else:
            existing = {
                str(r[0])
                for r in con.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            names = [name for name in tables if name in existing]
        for name in names:
            safe = name.replace('"', '""')
            out[name] = int(con.execute(f'SELECT COUNT(*) FROM "{safe}"').fetchone()[0])
    finally:
        con.close()
    return out


def capture(project_dir: str | Path) -> dict[str, Any]:
    project = Path(project_dir)
    data = project / "data"
    env = project / ".env"
    key = data / "master.key"
    secrets_path = data / "secrets.enc.json"
    app_db = data / "pbx_app.sqlite3"
    history_db = data / "call_history.sqlite3"
    contacts_path = data / "contacts.json"

    secret_keys: list[str] = []
    if secrets_path.exists():
        try:
            raw = json.loads(secrets_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                secret_keys = sorted(str(k) for k in raw)
        except Exception:
            secret_keys = []

    return {
        "env_present": env.exists(),
        "env_sha256": _sha256(env) if env.exists() else "",
        "master_key_present": key.exists(),
        "master_key_sha256": _sha256(key) if key.exists() else "",
        "secret_store_present": secrets_path.exists(),
        "secret_keys": secret_keys,
        "app_db_present": app_db.exists(),
        "app_table_counts": _table_counts(app_db, PERSISTENT_APP_TABLES),
        "history_db_present": history_db.exists(),
        "history_table_counts": _table_counts(history_db, None),
        "contacts_present": contacts_path.exists(),
        "contacts_count": _json_list_count(contacts_path),
    }


def verify(project_dir: str | Path, baseline: dict[str, Any]) -> list[str]:
    current = capture(project_dir)
    errors: list[str] = []

    for label, present_key, hash_key in (
        (".env", "env_present", "env_sha256"),
        ("master.key", "master_key_present", "master_key_sha256"),
    ):
        if baseline.get(present_key) and not current.get(present_key):
            errors.append(f"{label} was lost")
        elif baseline.get(present_key) and baseline.get(hash_key) != current.get(hash_key):
            errors.append(f"{label} changed unexpectedly")

    if baseline.get("secret_store_present") and not current.get("secret_store_present"):
        errors.append("encrypted secret store was lost")
    old_secret_keys = set(baseline.get("secret_keys") or [])
    new_secret_keys = set(current.get("secret_keys") or [])
    lost_secret_keys = sorted(old_secret_keys - new_secret_keys)
    if lost_secret_keys:
        errors.append("secret_keys lost: " + ", ".join(lost_secret_keys))

    for key, label in (
        ("app_table_counts", "application database"),
        ("history_table_counts", "call history database"),
    ):
        before = baseline.get(key) or {}
        after = current.get(key) or {}
        for table, count in before.items():
            if table not in after:
                errors.append(f"{label} table lost: {table}")
            elif int(after[table]) < int(count):
                errors.append(f"{label} row loss in {table}: {count} -> {after[table]}")

    if baseline.get("contacts_present") and not current.get("contacts_present"):
        errors.append("contacts.json was lost")
    before_contacts = int(baseline.get("contacts_count", 0) or 0)
    after_contacts = int(current.get("contacts_count", 0) or 0)
    if after_contacts < before_contacts:
        errors.append(f"contact row loss: {before_contacts} -> {after_contacts}")

    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in {"capture", "verify"}:
        print("usage: state_guard.py capture|verify PROJECT_DIR BASELINE_JSON", file=sys.stderr)
        return 2
    action, project_raw, baseline_raw = argv[1:]
    project = Path(project_raw)
    baseline_path = Path(baseline_raw)
    if action == "capture":
        baseline_path.write_text(json.dumps(capture(project), indent=2, sort_keys=True), encoding="utf-8")
        return 0
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"could not read baseline: {exc}", file=sys.stderr)
        return 2
    errors = verify(project, baseline)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("persistent state continuity verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
