import unittest
from pathlib import Path


class BootstrapUpdateScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = Path("bootstrap-update.sh").read_text(encoding="utf-8")

    def test_missing_installer_is_recovered_from_github(self):
        self.assertIn("install-managed-updater.sh", self.script)
        self.assertIn("updater/managed-update-agent.sh", self.script)
        self.assertIn("raw.githubusercontent.com", self.script)
        self.assertIn("Installing/repairing managed updater files", self.script)

    def test_script_does_not_abort_when_installer_is_missing(self):
        self.assertNotIn("Managed updater installer is missing from $PROJECT_DIR", self.script)

    def test_downloaded_updater_scripts_are_syntax_checked(self):
        self.assertIn('bash -n "$INSTALLER_TMP"', self.script)
        self.assertIn('bash -n "$AGENT_TMP"', self.script)


if __name__ == "__main__":
    unittest.main()
