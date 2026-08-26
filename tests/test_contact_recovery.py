import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from contact_recovery import contact_ownership_summary, repair_contact_ownership
from contacts import ContactsStore


class _DB:
    def __init__(self, ids):
        self.ids = list(ids)

    def list_workspaces(self):
        return [{"id": value, "alias": value} for value in self.ids]


class _Workspaces:
    def __init__(self, default_id=""):
        self.default_id = default_id

    def default_workspace(self):
        return {"id": self.default_id} if self.default_id else None


class ContactRecoveryTests(unittest.TestCase):
    def test_repairs_blank_but_preserves_remembered_orphan_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            store = ContactsStore(str(Path(td) / "contacts.json"))
            a = store.create(name="Legacy", number="4075550100", workspace_id="")
            b = store.create(name="Orphan", number="4075550101", workspace_id="deleted-ws")
            c = store.create(name="Other", number="4075550102", workspace_id="ws-2")
            g = store.create(name="Global", number="4075550103", workspace_id="", scope="global")
            server = SimpleNamespace(
                contacts=store,
                db=_DB(["ws-1", "ws-2"]),
                workspaces=_Workspaces("ws-1"),
            )

            changed = repair_contact_ownership(server)
            self.assertEqual(changed, 1)

            rows = {row["id"]: row for row in store.list()}
            self.assertEqual(rows[a["id"]]["workspace_id"], "ws-1")
            self.assertEqual(rows[b["id"]]["workspace_id"], "deleted-ws")
            self.assertEqual(rows[c["id"]]["workspace_id"], "ws-2")
            self.assertEqual(rows[g["id"]]["scope"], "global")
            self.assertEqual(rows[g["id"]]["workspace_id"], "")

    def test_explicit_orphan_repair_is_available_for_admin_tooling(self):
        with tempfile.TemporaryDirectory() as td:
            store = ContactsStore(str(Path(td) / "contacts.json"))
            item = store.create(name="Orphan", number="4075550199", workspace_id="deleted-ws")
            server = SimpleNamespace(
                contacts=store,
                db=_DB(["ws-1"]),
                workspaces=_Workspaces("ws-1"),
            )
            self.assertEqual(repair_contact_ownership(server, repair_orphans=True), 1)
            self.assertEqual(store.get(item["id"])["workspace_id"], "ws-1")

    def test_preferred_workspace_is_used_when_valid(self):
        with tempfile.TemporaryDirectory() as td:
            store = ContactsStore(str(Path(td) / "contacts.json"))
            item = store.create(name="Legacy", number="3525550100", workspace_id="")
            server = SimpleNamespace(
                contacts=store,
                db=_DB(["ws-1", "ws-2"]),
                workspaces=_Workspaces("ws-1"),
            )
            self.assertEqual(repair_contact_ownership(server, "ws-2"), 1)
            self.assertEqual(store.get(item["id"])["workspace_id"], "ws-2")

    def test_multiple_workspaces_without_default_does_not_guess(self):
        with tempfile.TemporaryDirectory() as td:
            store = ContactsStore(str(Path(td) / "contacts.json"))
            item = store.create(name="Legacy", number="3525550104", workspace_id="")
            server = SimpleNamespace(
                contacts=store,
                db=_DB(["ws-1", "ws-2"]),
                workspaces=_Workspaces(""),
            )
            self.assertEqual(repair_contact_ownership(server), 0)
            self.assertEqual(store.get(item["id"])["workspace_id"], "")

    def test_no_workspace_means_no_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            store = ContactsStore(str(Path(td) / "contacts.json"))
            item = store.create(name="Legacy", number="3215550100", workspace_id="")
            server = SimpleNamespace(
                contacts=store,
                db=_DB([]),
                workspaces=_Workspaces(""),
            )
            self.assertEqual(repair_contact_ownership(server), 0)
            self.assertEqual(store.get(item["id"])["workspace_id"], "")

    def test_ownership_summary_reports_orphans_without_phone_data(self):
        with tempfile.TemporaryDirectory() as td:
            store = ContactsStore(str(Path(td) / "contacts.json"))
            store.create(name="Assigned", number="3215550101", workspace_id="ws-1")
            store.create(name="Orphan", number="3215550102", workspace_id="gone")
            store.create(name="Global", number="3215550103", scope="global")
            store.create(name="Blank", number="3215550104", workspace_id="")
            server = SimpleNamespace(
                contacts=store,
                db=_DB(["ws-1"]),
                workspaces=_Workspaces("ws-1"),
            )
            summary = contact_ownership_summary(server)
            self.assertEqual(summary["total"], 4)
            self.assertEqual(summary["assigned"], 1)
            self.assertEqual(summary["orphaned"], 1)
            self.assertEqual(summary["global"], 1)
            self.assertEqual(summary["unassigned"], 1)
            self.assertEqual(summary["orphaned_workspace_ids"], ["gone"])


if __name__ == "__main__":
    unittest.main()
