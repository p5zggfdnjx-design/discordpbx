import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from runtime_hotfix import _queue_update, _system_update_status, inject_updater_ui


class _AuditDB:
    def __init__(self):
        self.rows = []

    def audit(self, action, **kwargs):
        self.rows.append((action, kwargs))


class _FakeServer:
    def __init__(self, root: Path):
        self._updates_dir = root
        self.config = SimpleNamespace(version="3.3.5")
        self.db = _AuditDB()

    async def _system_admin(self, request):
        return {"user_id": "admin", "name": "Admin"}

    def _update_status_path(self):
        return self._updates_dir / "status.json"

    @staticmethod
    def _read_update_json(path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default


class UpdaterHotfixTests(unittest.TestCase):
    def test_ui_injection_is_idempotent_and_one_click(self):
        page = "<html><body><div>PBX</div></body></html>"
        once = inject_updater_ui(page)
        twice = inject_updater_ui(once)
        self.assertEqual(once, twice)
        self.assertEqual(once.count('pbx-updater-hotfix-v333'), 1)
        self.assertIn('/api/system/update/upload', once)
        self.assertIn('/api/system/update/apply', once)
        self.assertIn('/api/system/update/github/install', once)
        self.assertIn('Check & Install Latest', once)
        self.assertIn('Install Selected ZIP', once)
        self.assertIn("'migrating':'Migrating settings/data'", once)
        self.assertNotIn("confirm(", once)

    def test_queue_does_not_require_status_json_when_shared_queue_is_writable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "pending.zip").write_bytes(b"zip-placeholder")
            (root / "pending_meta.json").write_text(
                json.dumps({"version": "3.3.6", "sha256": "abc", "source": "github"}),
                encoding="utf-8",
            )
            server = _FakeServer(root)
            meta = _queue_update(server, {"user_id": "admin", "name": "Admin", "auth_type": "session"})
            self.assertEqual(meta["version"], "3.3.6")
            marker = json.loads((root / "apply.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["target_version"], "3.3.6")
            self.assertFalse(marker["agent_confirmed"])
            self.assertEqual(server.db.rows[0][0], "system.update.requested")

    def test_status_treats_writable_shared_queue_as_actionable(self):
        try:
            import aiohttp  # noqa: F401
        except Exception:
            self.skipTest("aiohttp not installed")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "pending_meta.json").write_text(json.dumps({"version": "3.3.6"}), encoding="utf-8")
            server = _FakeServer(root)
            response = asyncio.run(_system_update_status(server, None))
            payload = json.loads(response.text)
            self.assertTrue(payload["queue_writable"])
            self.assertTrue(payload["managed_agent_ready"])
            self.assertFalse(payload["agent_confirmed"])
            self.assertEqual(payload["pending"]["version"], "3.3.6")


if __name__ == "__main__":
    unittest.main()
