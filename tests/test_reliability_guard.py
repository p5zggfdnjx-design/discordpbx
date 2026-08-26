import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from appdb import AppDatabase
from contacts import ContactsStore
from reliability_guard import (
    _patch_appdb_revision_restore,
    clear_workspace_capability_cache,
    import_contact_rows,
)


class ReliabilityGuardTests(unittest.TestCase):
    def test_capability_cache_can_clear_one_workspace_or_all(self):
        cache = {
            ("u1", "ws-1"): (0, {"dial"}, []),
            ("u2", "ws-1"): (0, {"contacts"}, []),
            ("u1", "ws-2"): (0, {"history"}, []),
        }
        server = SimpleNamespace(workspaces=SimpleNamespace(_member_caps_cache=cache))
        self.assertEqual(clear_workspace_capability_cache(server, "ws-1"), 2)
        self.assertEqual(set(cache), {("u1", "ws-2")})
        self.assertEqual(clear_workspace_capability_cache(server), 1)
        self.assertEqual(cache, {})

    @staticmethod
    def _contact_server(path: Path):
        store = ContactsStore(str(path))

        def values(body, wsid, existing=None):
            existing = existing or {}
            scope = str(body.get("scope", existing.get("scope", "workspace")) or "workspace").lower()
            if scope not in {"workspace", "global"}:
                scope = "workspace"
            return {
                "name": str(body.get("name", existing.get("name", ""))).strip(),
                "number": str(body.get("number", existing.get("number", ""))).strip(),
                "group": str(body.get("group", existing.get("group", ""))),
                "notes": str(body.get("notes", existing.get("notes", ""))),
                "favorite": bool(existing.get("favorite", False)),
                "bypass_voicemail_detection": bool(existing.get("bypass_voicemail_detection", False)),
                "workspace_id": wsid,
                "scope": scope,
                "tags": [],
            }

        return SimpleNamespace(contacts=store, _contact_values_v3=values)

    def test_non_admin_csv_cannot_create_global_contact(self):
        with tempfile.TemporaryDirectory() as td:
            server = self._contact_server(Path(td) / "contacts.json")
            result = import_contact_rows(
                server,
                {"system_admin": False},
                "ws-1",
                [{"name": "Shared", "number": "4075550100", "scope": "global"}],
            )
            self.assertEqual(result["forbidden"], 1)
            self.assertEqual(server.contacts.list(), [])

    def test_non_admin_csv_cannot_mutate_existing_global_contact(self):
        with tempfile.TemporaryDirectory() as td:
            server = self._contact_server(Path(td) / "contacts.json")
            item = server.contacts.create(name="Shared", number="4075550100", scope="global")
            result = import_contact_rows(
                server,
                {"system_admin": False},
                "ws-1",
                [{"name": "Hijacked", "number": "4075550100"}],
            )
            self.assertEqual(result["forbidden"], 1)
            self.assertEqual(server.contacts.get(item["id"])["name"], "Shared")

    def test_system_admin_csv_can_create_global_contact(self):
        with tempfile.TemporaryDirectory() as td:
            server = self._contact_server(Path(td) / "contacts.json")
            result = import_contact_rows(
                server,
                {"system_admin": True},
                "ws-1",
                [{"name": "Shared", "number": "4075550100", "scope": "global"}],
            )
            self.assertEqual(result["added"], 1)
            self.assertEqual(server.contacts.list()[0]["scope"], "global")

    def test_revision_restore_preserves_local_user_workspace_access(self):
        _patch_appdb_revision_restore()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pbx.sqlite3"
            db = AppDatabase(str(path))
            ws = db.upsert_workspace({"guild_id": "123456789012345678", "alias": "Main"})
            with sqlite3.connect(path) as con:
                con.execute(
                    "INSERT INTO local_users(id,username,display_name,password_hash,salt,enabled,is_system_admin,created_at,updated_at,last_login) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    ("user-1", "operator", "Operator", "hash", "salt", 1, 0, 1.0, 1.0, 0.0),
                )
                con.execute(
                    "INSERT INTO local_user_workspaces(user_id,workspace_id,capabilities_json) VALUES(?,?,?)",
                    ("user-1", ws["id"], '["dial","contacts"]'),
                )
            revision = db.save_revision("known good")
            db.upsert_workspace({**ws, "alias": "Changed"})
            self.assertTrue(db.restore_revision(revision))
            with sqlite3.connect(path) as con:
                row = con.execute(
                    "SELECT capabilities_json FROM local_user_workspaces WHERE user_id=? AND workspace_id=?",
                    ("user-1", ws["id"]),
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertIn("dial", row[0])


if __name__ == "__main__":
    unittest.main()
