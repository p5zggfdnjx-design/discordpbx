import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from appdb import AppDatabase
from auto_redial_guard import _create_job, _job_by_root, _update_job
from redial_start_race import reconcile_initial_job


class History:
    def __init__(self):
        self.rows = {}
        self.activity = []

    def get_by_uuid(self, uid):
        return dict(self.rows.get(uid, {})) or None

    def log_activity(self, action, detail, **kwargs):
        self.activity.append((action, detail, kwargs))


class Bridge:
    def __init__(self):
        self.sessions = {}
        self.pending = {}

    def get_session(self, uid):
        return self.sessions.get(uid)

    def get_pending(self, uid):
        return self.pending.get(uid)


class Server:
    def __init__(self, root):
        self.db = AppDatabase(str(Path(root) / "pbx_app.sqlite3"))
        self.call_history = History()
        self.bot = SimpleNamespace(bridge=Bridge())

    def make_job(self):
        return _create_job(
            self,
            root_uuid="root",
            workspace_id="ws-1",
            number="14075550100",
            caller_id="14075550199",
            contact_name="Test",
            randomize_caller_id=False,
            interval_seconds=5,
            max_attempts=10,
            attempts_made=1,
            current_call_uuid="root",
            state="dialing",
        )


class RedialStartRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = Server(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fast_failure_becomes_waiting_instead_of_stuck_dialing(self):
        self.server.make_job()
        self.server.call_history.rows["root"] = {"outcome": "no answer", "diagnostic": "ring timeout"}
        job = reconcile_initial_job(self.server, "root")
        self.assertEqual(job["state"], "waiting")
        self.assertEqual(job["attempts_made"], 1)
        self.assertGreater(job["next_attempt_at"], 0)

    def test_fast_answer_completes_job(self):
        self.server.make_job()
        self.server.bot.bridge.sessions["root"] = SimpleNamespace(voicemail_detection_enabled=False)
        job = reconcile_initial_job(self.server, "root")
        self.assertEqual(job["state"], "answered")
        self.assertEqual(job["current_call_uuid"], "")

    def test_fast_answer_with_screening_enters_screening(self):
        self.server.make_job()
        self.server.bot.bridge.sessions["root"] = SimpleNamespace(voicemail_detection_enabled=True)
        job = reconcile_initial_job(self.server, "root")
        self.assertEqual(job["state"], "screening")
        self.assertEqual(job["current_call_uuid"], "root")

    def test_still_pending_is_left_alone(self):
        self.server.make_job()
        self.server.bot.bridge.pending["root"] = {"uuid": "root"}
        before = _job_by_root(self.server, "root")
        after = reconcile_initial_job(self.server, "root")
        self.assertEqual(after["state"], before["state"])
        self.assertEqual(after["attempts_made"], before["attempts_made"])

    def test_existing_waiting_state_is_not_rescheduled(self):
        job = self.server.make_job()
        _update_job(self.server, job["job_id"], state="waiting", next_attempt_at=12345.0, current_call_uuid="")
        after = reconcile_initial_job(self.server, "root")
        self.assertEqual(after["state"], "waiting")
        self.assertEqual(after["next_attempt_at"], 12345.0)


if __name__ == "__main__":
    unittest.main()
