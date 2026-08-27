import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from contacts import ContactsStore
from global_contacts import import_contact_rows_allow_global_create, patch_console_html


class GlobalContactPolicyTests(unittest.TestCase):
    @staticmethod
    def _server(path: Path):
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

    def test_non_admin_csv_can_create_unique_global_contact(self):
        with tempfile.TemporaryDirectory() as td:
            server = self._server(Path(td) / "contacts.json")
            result = import_contact_rows_allow_global_create(
                server,
                {"system_admin": False},
                "ws-1",
                [{"name": "Shared", "number": "4075550100", "scope": "global"}],
            )
            self.assertEqual(result, {"added": 1, "updated": 0, "invalid": 0, "forbidden": 0})
            row = server.contacts.list()[0]
            self.assertEqual(row["scope"], "global")
            self.assertEqual(row["name"], "Shared")

    def test_non_admin_csv_cannot_modify_existing_global_contact(self):
        with tempfile.TemporaryDirectory() as td:
            server = self._server(Path(td) / "contacts.json")
            item = server.contacts.create(name="Shared", number="4075550100", scope="global")
            result = import_contact_rows_allow_global_create(
                server,
                {"system_admin": False},
                "ws-1",
                [{"name": "Changed", "number": "4075550100", "scope": "global"}],
            )
            self.assertEqual(result["forbidden"], 1)
            self.assertEqual(server.contacts.get(item["id"])["name"], "Shared")

    def test_non_admin_csv_cannot_promote_existing_workspace_contact(self):
        with tempfile.TemporaryDirectory() as td:
            server = self._server(Path(td) / "contacts.json")
            item = server.contacts.create(
                name="Local",
                number="4075550100",
                workspace_id="ws-1",
                scope="workspace",
            )
            result = import_contact_rows_allow_global_create(
                server,
                {"system_admin": False},
                "ws-1",
                [{"name": "Local", "number": "4075550100", "scope": "global"}],
            )
            self.assertEqual(result["forbidden"], 1)
            self.assertEqual(server.contacts.get(item["id"])["scope"], "workspace")

    def test_system_admin_can_still_modify_global_contact(self):
        with tempfile.TemporaryDirectory() as td:
            server = self._server(Path(td) / "contacts.json")
            item = server.contacts.create(name="Shared", number="4075550100", scope="global")
            result = import_contact_rows_allow_global_create(
                server,
                {"system_admin": True},
                "ws-1",
                [{"name": "Changed", "number": "4075550100", "scope": "global"}],
            )
            self.assertEqual(result["updated"], 1)
            self.assertEqual(server.contacts.get(item["id"])["name"], "Changed")

    def test_console_enables_global_create_but_hides_global_mutation_controls(self):
        original = (
            "const g=$('#contactScope option[value=global]');if(g)g.disabled=!me?.system_admin;"
            "${canWs(contactWorkspace(c),'contacts')?`<button class=\"btn\" data-c-edit="
        )
        patched = patch_console_html(original)
        self.assertIn("if(g)g.disabled=false", patched)
        self.assertNotIn("g.disabled=!me?.system_admin", patched)
        self.assertIn("(c.scope!=='global'||me?.system_admin)", patched)


if __name__ == "__main__":
    unittest.main()
