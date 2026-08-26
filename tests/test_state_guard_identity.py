import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from updater.state_guard import capture, verify


class StateGuardIdentityTests(unittest.TestCase):
    @staticmethod
    def _project(root: Path):
        data = root / "data"
        data.mkdir(parents=True)
        (root / ".env").write_text("TEST=1\n", encoding="utf-8")
        db = data / "pbx_app.sqlite3"
        with sqlite3.connect(db) as con:
            con.executescript(
                """
                CREATE TABLE workspaces(id TEXT PRIMARY KEY, guild_id TEXT NOT NULL);
                CREATE TABLE workspace_roles(workspace_id TEXT, role_id TEXT, capability TEXT);
                """
            )
            con.executemany(
                "INSERT INTO workspaces(id,guild_id) VALUES(?,?)",
                [("ws-a", "111"), ("ws-b", "222")],
            )
            con.execute(
                "INSERT INTO workspace_roles(workspace_id,role_id,capability) VALUES(?,?,?)",
                ("ws-a", "role-a", "dial"),
            )
        contacts = [
            {"id": "contact-a", "name": "A", "number": "14075550100"},
            {"id": "contact-b", "name": "B", "number": "14075550101"},
        ]
        (data / "contacts.json").write_text(json.dumps(contacts), encoding="utf-8")
        return db, data / "contacts.json"

    def test_same_workspace_count_with_replaced_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db, _ = self._project(root)
            baseline = capture(root)
            with sqlite3.connect(db) as con:
                con.execute("DELETE FROM workspaces WHERE id='ws-b'")
                con.execute("INSERT INTO workspaces(id,guild_id) VALUES('ws-c','333')")
            errors = verify(root, baseline)
            self.assertTrue(any("identity loss in workspaces" in item for item in errors), errors)

    def test_same_contact_count_with_replaced_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, contacts_path = self._project(root)
            baseline = capture(root)
            rows = json.loads(contacts_path.read_text(encoding="utf-8"))
            rows[1] = {"id": "contact-c", "name": "C", "number": "14075550102"}
            contacts_path.write_text(json.dumps(rows), encoding="utf-8")
            errors = verify(root, baseline)
            self.assertTrue(any("contact identity loss" in item for item in errors), errors)

    def test_additive_changes_are_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db, contacts_path = self._project(root)
            baseline = capture(root)
            with sqlite3.connect(db) as con:
                con.execute("INSERT INTO workspaces(id,guild_id) VALUES('ws-c','333')")
            rows = json.loads(contacts_path.read_text(encoding="utf-8"))
            rows.append({"id": "contact-c", "name": "C", "number": "14075550102"})
            contacts_path.write_text(json.dumps(rows), encoding="utf-8")
            self.assertEqual(verify(root, baseline), [])


if __name__ == "__main__":
    unittest.main()
