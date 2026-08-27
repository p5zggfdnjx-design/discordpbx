import json
import tempfile
import unittest
from pathlib import Path

from appdb import AppDatabase
from call_history import CallHistoryStore
from database_consolidation import (
    _inject_online_users_ui,
    backup_and_catalog_legacy_data,
    ensure_schema,
    known_user_count,
    list_online_users,
    migrate_call_history,
    sync_identity_directory,
)
from history_mirror import MirroredCallHistoryStore


class DatabaseConsolidationTests(unittest.TestCase):
    def test_history_migrates_atomically_into_app_database(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app_path = root / "pbx_app.sqlite3"
            old_path = root / "call_history.sqlite3"
            db = AppDatabase(str(app_path))
            old = CallHistoryStore(str(old_path))
            old.start_call(uuid="call-1", direction="outbound", number="14075550100")
            old.finish("call-1", outcome="completed", duration=3.5)

            copied = migrate_call_history(db, old_path)
            self.assertGreater(copied, 0)
            merged = CallHistoryStore(str(app_path))
            row = merged.get_by_uuid("call-1")
            self.assertIsNotNone(row)
            self.assertEqual(row["number"], "14075550100")
            self.assertEqual(row["outcome"], "completed")
            self.assertEqual(migrate_call_history(db, old_path), 0)

    def test_identity_directory_and_online_sessions(self):
        with tempfile.TemporaryDirectory() as td:
            db = AppDatabase(str(Path(td) / "pbx_app.sqlite3"))
            ensure_schema(db)
            db.upsert_user("12345", "tester", "Test User", "")
            db.create_session("12345", "discord", ttl_seconds=3600)
            sync_identity_directory(db)
            self.assertGreaterEqual(known_user_count(db), 1)
            online = list_online_users(db)
            self.assertTrue(any(x["user_id"] == "12345" for x in online))

    def test_legacy_sources_are_backed_up_and_cataloged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            data.mkdir()
            (data / "contacts.json").write_text(
                json.dumps([{"id": "c1", "name": "A", "number": "14075550100"}]),
                encoding="utf-8",
            )
            history = CallHistoryStore(str(data / "call_history.sqlite3"))
            history.start_call(uuid="legacy-call", direction="inbound", number="14075550101")
            db = AppDatabase(str(data / "pbx_app.sqlite3"))
            ensure_schema(db)
            backup_and_catalog_legacy_data(db, data)

            backup = data / "migration-backups" / "database-consolidation-v1"
            self.assertTrue((backup / "contacts.json").is_file())
            self.assertTrue((backup / "call_history.sqlite3").is_file())
            with db._connect() as con:
                count = con.execute("SELECT COUNT(*) FROM legacy_data_catalog").fetchone()[0]
            self.assertGreaterEqual(count, 2)

    def test_history_mirror_keeps_rollback_database_current(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            primary = CallHistoryStore(str(root / "app.sqlite3"))
            mirror = CallHistoryStore(str(root / "legacy.sqlite3"))
            store = MirroredCallHistoryStore(primary, mirror)
            store.start_call(uuid="call-2", direction="outbound", number="14075550102")
            store.finish("call-2", outcome="completed", duration=1)
            self.assertIsNotNone(primary.get_by_uuid("call-2"))
            self.assertIsNotNone(mirror.get_by_uuid("call-2"))

    def test_online_user_ui_injection_is_idempotent(self):
        html = '<html><body><section id="workspaces"></section></body></html>'
        first = _inject_online_users_ui(html)
        second = _inject_online_users_ui(first)
        self.assertIn('id="pbx-online-users-script"', first)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
