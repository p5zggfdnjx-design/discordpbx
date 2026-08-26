import unittest
from pathlib import Path


class ManagedBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = Path("bootstrap-managed-install.sh").read_text(encoding="utf-8")

    def test_managed_adoption_captures_and_verifies_full_state(self):
        self.assertIn('updater/state_guard.py" capture "$OLD_DIR" "$STATE_BASELINE"', self.script)
        self.assertIn('updater/state_guard.py" verify "$INSTALL_DIR" "$STATE_BASELINE"', self.script)
        self.assertIn("Persistent state continuity: verified", self.script)

    def test_cutover_still_builds_before_removing_old_container(self):
        build_at = self.script.index('log "Building replacement image before touching the old service"')
        cutover_at = self.script.index('log "Cutting over to the managed service"')
        self.assertLess(build_at, cutover_at)

    def test_host_override_is_preserved_by_release_copy(self):
        self.assertIn("--exclude 'docker-compose.override.yml'", self.script)


if __name__ == "__main__":
    unittest.main()
