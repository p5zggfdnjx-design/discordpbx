import json
import tempfile
import unittest
from pathlib import Path

from updater.state_guard import capture, verify


class StateGuardContactTests(unittest.TestCase):
    def test_contacts_are_counted_and_loss_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            data.mkdir()
            contacts = data / "contacts.json"
            contacts.write_text(json.dumps([
                {"id": "a", "name": "A", "number": "14075550100"},
                {"id": "b", "name": "B", "number": "14075550101"},
            ]), encoding="utf-8")
            baseline = capture(root)
            self.assertEqual(baseline["contacts_count"], 2)

            contacts.write_text(json.dumps([
                {"id": "a", "name": "A", "number": "14075550100"},
            ]), encoding="utf-8")
            errors = verify(root, baseline)
            self.assertTrue(any("contact row loss" in x for x in errors), errors)


if __name__ == "__main__":
    unittest.main()
