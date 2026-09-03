import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from appdb import AppDatabase
from secrets_store import SecretStore
from updater.migrate_state import run
from updater.state_guard import capture, verify

REPO_ROOT = Path(__file__).resolve().parent.parent


class UpdateMigrationTests(unittest.TestCase):
    def test_startup_migration_imports_legacy_env_once(self):
        with tempfile.TemporaryDirectory() as td:
            env = {
                "DATA_DIR": td,
                "PUBLIC_BASE_URL": "https://pbx.example.test",
                "DISCORD_CLIENT_ID": "1234567890",
                "DISCORD_CLIENT_SECRET": "oauth-secret-one",
                "DISCORD_TOKEN": "bot-token-one",
                "ASTERISK_AMI_HOST": "192.0.2.10",
                "ASTERISK_AMI_PORT": "5038",
                "ASTERISK_AMI_USER": "pbx",
                "ASTERISK_AMI_SECRET": "ami-secret-one",
                "PBX_INGRESS_TOKEN": "ingress-one",
                "BOT_OWNER_IDS": "111,222",
                "PBX_TARGET_VERSION": "3.3.6",
            }
            with patch.dict(os.environ, env, clear=True):
                result = run()
                self.assertIn("discord_oauth_client_secret", result["imported_secrets"])
                db = AppDatabase(str(Path(td) / "pbx_app.sqlite3"))
                store = SecretStore(td)
                self.assertEqual(db.get_setting("public_base_url"), "https://pbx.example.test")
                self.assertEqual(db.get_setting("discord_client_id"), "1234567890")
                self.assertEqual(db.get_setting("system_admin_discord_ids"), ["111", "222"])
                self.assertEqual(store.get("discord_oauth_client_secret"), "oauth-secret-one")
                self.assertEqual(store.get("asterisk_ami_secret"), "ami-secret-one")

            changed = dict(env)
            changed["PUBLIC_BASE_URL"] = "https://wrong.example.test"
            changed["DISCORD_CLIENT_SECRET"] = "oauth-secret-two"
            with patch.dict(os.environ, changed, clear=True):
                run()
                db = AppDatabase(str(Path(td) / "pbx_app.sqlite3"))
                store = SecretStore(td)
                self.assertEqual(db.get_setting("public_base_url"), "https://pbx.example.test")
                self.assertEqual(store.get("discord_oauth_client_secret"), "oauth-secret-one")

    def test_runs_as_standalone_script_like_the_managed_updater(self):
        """Reproduces the managed updater's real invocation.

        managed-update-agent.sh runs this file as
        `docker compose run --entrypoint python discord-pbx /app/updater/migrate_state.py`,
        i.e. `python /app/updater/migrate_state.py`. Unlike importing
        `updater.migrate_state` (as the test above does, and as pytest's
        rootdir-on-sys.path behavior allows), invoking the file directly only
        puts its own directory (updater/) on sys.path, not the application
        root containing appdb.py/secrets_store.py. This must not raise
        ModuleNotFoundError.
        """
        script = REPO_ROOT / "updater" / "migrate_state.py"
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ)
            env.update({
                "DATA_DIR": td,
                "PUBLIC_BASE_URL": "https://standalone.example.test",
                "BOT_OWNER_IDS": "999",
                "PBX_TARGET_VERSION": "3.3.17",
            })
            result = subprocess.run(
                [sys.executable, str(script)],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                result.returncode, 0,
                msg=f"stdout={result.stdout!r} stderr={result.stderr!r}",
            )
            self.assertNotIn("ModuleNotFoundError", result.stderr)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertTrue(payload["ok"])
            self.assertIn("public_base_url", payload["imported_settings"])

            db = AppDatabase(str(Path(td) / "pbx_app.sqlite3"))
            self.assertEqual(db.get_setting("public_base_url"), "https://standalone.example.test")

    def test_state_guard_allows_additions_but_detects_loss(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            data = project / "data"
            data.mkdir()
            (project / ".env").write_text("PUBLIC_BASE_URL=https://pbx.example.test\n", encoding="utf-8")
            (data / "master.key").write_text("stable-key\n", encoding="utf-8")
            (data / "secrets.enc.json").write_text(
                json.dumps({"discord_bot_token": "cipher", "asterisk_ami_secret": "cipher2"}),
                encoding="utf-8",
            )
            (data / "call_history.sqlite3").touch()
            con = sqlite3.connect(data / "pbx_app.sqlite3")
            con.executescript(
                "CREATE TABLE settings(key TEXT PRIMARY KEY);"
                "CREATE TABLE workspaces(id TEXT PRIMARY KEY);"
                "CREATE TABLE local_users(id TEXT PRIMARY KEY);"
                "INSERT INTO settings VALUES('public_base_url');"
                "INSERT INTO workspaces VALUES('south');"
                "INSERT INTO local_users VALUES('operator');"
            )
            con.commit()
            con.close()

            baseline = capture(project)
            self.assertEqual(verify(project, baseline), [])

            raw = json.loads((data / "secrets.enc.json").read_text(encoding="utf-8"))
            raw["discord_oauth_client_secret"] = "new-cipher"
            (data / "secrets.enc.json").write_text(json.dumps(raw), encoding="utf-8")
            self.assertEqual(verify(project, baseline), [])

            raw.pop("discord_bot_token")
            (data / "secrets.enc.json").write_text(json.dumps(raw), encoding="utf-8")
            errors = verify(project, baseline)
            self.assertTrue(any("secret_keys lost" in x for x in errors))


if __name__ == "__main__":
    unittest.main()
