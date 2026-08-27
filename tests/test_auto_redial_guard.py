import asyncio
import tempfile
import unittest
from pathlib import Path

from appdb import AppDatabase
from auto_redial_guard import (
    _create_job,
    _dial_due_job,
    _ensure_schema,
    _inject_ui,
    _job_by_call,
    _job_by_root,
    _normalize_reason,
    _process_pending_timeouts,
    _recover_jobs,
    _schedule_retry,
    _update_job,
)


class FakeHistory:
    def __init__(self):
        self.rows = {}
        self.failed = []
        self.activity = []

    def get_by_uuid(self, uid):
        return dict(self.rows.get(uid, {})) or None

    def fail(self, uid, **kwargs):
        self.failed.append((uid, kwargs))
        self.rows.setdefault(uid, {}).update({"outcome": kwargs.get("outcome", "failed")})

    def log_activity(self, action, detail, **kwargs):
        self.activity.append((action, detail, kwargs))


class FakeBridge:
    def __init__(self):
        self.sessions = {}
        self.pending = {}

    def get_session(self, uid):
        return self.sessions.get(uid)

    def get_pending(self, uid):
        return self.pending.get(uid)


class FakeBot:
    def __init__(self):
        self.bridge = FakeBridge()


class FakeServer:
    def __init__(self, root, queue_results=None):
        self.db = AppDatabase(str(Path(root) / "pbx_app.sqlite3"))
        self.call_history = FakeHistory()
        self.bot = FakeBot()
        self.queue_results = list(queue_results or [])
        self.queue_calls = 0
        self.published = []
        self._auto_redial = {}
        self._redial_tasks = {}
        _ensure_schema(self)

    def _sanitize_detail(self, value):
        return str(value)

    def _queue_web_outbound(self, *args, **kwargs):
        self.queue_calls += 1
        result = self.queue_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def _publish(self, event, payload):
        self.published.append((event, payload))

    async def _maybe_schedule_redial(self, uid, reason, info):
        job = _job_by_call(self, uid)
        if not job:
            return False
        return _schedule_retry(self, job, reason)


class AutoRedialPersistentJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def make_job(self, server, **overrides):
        values = dict(
            root_uuid="root-uuid",
            workspace_id="ws-1",
            number="14075550100",
            caller_id="14075550199",
            contact_name="Test",
            randomize_caller_id=False,
            interval_seconds=5,
            max_attempts=10,
            attempts_made=1,
            current_call_uuid="root-uuid",
            state="dialing",
            operator_user_id="u-1",
            operator_name="Operator",
        )
        values.update(overrides)
        return _create_job(server, **values)

    async def test_no_answer_schedules_persistent_job(self):
        server = FakeServer(self.tmp.name)
        self.make_job(server)
        await _process_pending_timeouts(server, [{"uuid": "root-uuid", "detail": "ring timeout"}])
        job = _job_by_root(server, "root-uuid")
        self.assertEqual(job["state"], "waiting")
        self.assertEqual(job["attempts_made"], 1)
        self.assertGreater(job["next_attempt_at"], 0)
        self.assertEqual(server.call_history.failed[0][1]["outcome"], "no answer")

    async def test_infrastructure_failure_does_not_consume_phone_attempt(self):
        server = FakeServer(self.tmp.name, [
            RuntimeError("Discord voice temporarily unavailable"),
            ("14075550100", "14075550199", "Test", "attempt-2"),
        ])
        job = self.make_job(server, current_call_uuid="", state="waiting")
        _update_job(server, job["job_id"], next_attempt_at=0)
        await _dial_due_job(server, _job_by_root(server, "root-uuid"))
        after_failure = _job_by_root(server, "root-uuid")
        self.assertEqual(after_failure["attempts_made"], 1)
        self.assertEqual(after_failure["queue_failures"], 1)
        self.assertEqual(after_failure["state"], "waiting")

        _update_job(server, job["job_id"], next_attempt_at=0)
        await _dial_due_job(server, _job_by_root(server, "root-uuid"))
        after_success = _job_by_root(server, "root-uuid")
        self.assertEqual(after_success["attempts_made"], 2)
        self.assertEqual(after_success["queue_failures"], 0)
        self.assertEqual(after_success["current_call_uuid"], "attempt-2")
        self.assertIsNotNone(_job_by_call(server, "attempt-2"))

    async def test_policy_failure_enters_visible_error_state(self):
        server = FakeServer(self.tmp.name, [ValueError("destination is on the PBX do-not-call/block list")])
        job = self.make_job(server, current_call_uuid="", state="waiting")
        _update_job(server, job["job_id"], next_attempt_at=0)
        await _dial_due_job(server, _job_by_root(server, "root-uuid"))
        failed = _job_by_root(server, "root-uuid")
        self.assertEqual(failed["state"], "error")
        self.assertIn("do-not-call", failed["last_error"])
        self.assertEqual(failed["attempts_made"], 1)

    async def test_restart_recovers_in_flight_job_without_losing_attempt_count(self):
        server = FakeServer(self.tmp.name)
        job = self.make_job(server, current_call_uuid="dead-call", state="dialing", attempts_made=4)
        _recover_jobs(server)
        recovered = _job_by_root(server, "root-uuid")
        self.assertEqual(recovered["state"], "waiting")
        self.assertEqual(recovered["attempts_made"], 4)
        self.assertEqual(recovered["current_call_uuid"], "")
        self.assertIn("restart", recovered["last_reason"])

    async def test_attempt_limit_is_total_calls_not_retries_plus_original(self):
        server = FakeServer(self.tmp.name)
        job = self.make_job(server, attempts_made=3, max_attempts=3, current_call_uuid="", state="waiting")
        self.assertFalse(_schedule_retry(server, job, "no answer"))
        self.assertEqual(_job_by_root(server, "root-uuid")["state"], "exhausted")


class AutoRedialDesignTests(unittest.TestCase):
    def test_reason_normalization(self):
        self.assertEqual(_normalize_reason("ring-timeout"), "no answer")
        self.assertEqual(_normalize_reason("MACHINE"), "voicemail")
        self.assertEqual(_normalize_reason("USER BUSY"), "busy")

    def test_ui_replaces_per_call_editor_with_persistent_job_panel(self):
        base = '<html><body><section id="calls"></section><button data-redial>old</button></body></html>'
        first = _inject_ui(base)
        second = _inject_ui(first)
        self.assertEqual(first, second)
        self.assertIn("pbx-redial-jobs-v2", first)
        self.assertIn("Persistent retry jobs", first)
        self.assertIn("[data-redial]", first)
        self.assertIn("Start Auto Redial", first)


if __name__ == "__main__":
    unittest.main()
