import asyncio
import unittest

from auto_redial_guard import (
    maybe_schedule_redial,
    normalize_reason,
    process_pending_timeouts,
    retry_allowed,
    terminal_reason,
)


class FakeHistory:
    def __init__(self, row=None):
        self.row = dict(row or {})
        self.failed = []
        self.activity = []

    def get_by_uuid(self, uid):
        return dict(self.row) if self.row else None

    def fail(self, uid, **kwargs):
        self.failed.append((uid, kwargs))

    def log_activity(self, action, detail, **kwargs):
        self.activity.append((action, detail, kwargs))


class FakeServer:
    def __init__(self, queue_results=None):
        self._auto_redial = {}
        self._redial_tasks = {}
        self.call_history = FakeHistory({
            "number": "14075550100",
            "caller_id": "14075550199",
            "contact_name": "Test",
            "workspace_ids": ["ws-1"],
            "operator_user_id": "u-1",
            "operator_name": "Operator",
        })
        self.queue_results = list(queue_results or [])
        self.queue_calls = 0
        self.published = []

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


class AutoRedialPolicyTests(unittest.TestCase):
    def test_reason_normalization_and_modes(self):
        self.assertEqual(normalize_reason("ring-timeout"), "timeout")
        self.assertEqual(normalize_reason("NOANSWER"), "no answer")
        self.assertTrue(retry_allowed({"retry_on": "no-answer"}, "busy"))
        self.assertFalse(retry_allowed({"retry_on": "no-answer"}, "disconnected"))
        self.assertTrue(retry_allowed({"retry_on": "disconnect"}, "disconnected"))

    def test_terminal_reason_prefers_retryable_outcome(self):
        self.assertEqual(terminal_reason({"outcome": "no answer", "state": "ended"}), "no answer")
        self.assertEqual(terminal_reason({"outcome": "completed", "state": "disconnected"}), "disconnected")
        self.assertEqual(terminal_reason({"outcome": "completed", "state": "ended"}), "")


class AutoRedialAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_queue_failure_retries_without_losing_policy(self):
        server = FakeServer([
            RuntimeError("voice temporarily unavailable"),
            ("14075550100", "14075550199", "Test", "new-uuid"),
        ])
        server._auto_redial["root"] = {
            "enabled": True,
            "delay": 1,
            "max_retries": 2,
            "retries": 0,
            "retry_on": "no-answer",
            "root_uuid": "root",
        }

        scheduled = await maybe_schedule_redial(server, "root", "no answer", {})
        self.assertTrue(scheduled)
        task = server._redial_tasks["root"]
        await asyncio.wait_for(task, timeout=4)

        self.assertEqual(server.queue_calls, 2)
        self.assertNotIn("root", server._auto_redial)
        self.assertIn("new-uuid", server._auto_redial)
        self.assertEqual(server._auto_redial["new-uuid"]["retries"], 2)
        self.assertTrue(server._auto_redial["new-uuid"]["enabled"])
        self.assertTrue(any(x[0] == "auto redial retry failed" for x in server.call_history.activity))

    async def test_policy_failure_stops_instead_of_hammering(self):
        server = FakeServer([ValueError("destination is on the PBX do-not-call/block list")])
        server._auto_redial["root"] = {
            "enabled": True,
            "delay": 1,
            "max_retries": 5,
            "retries": 0,
            "retry_on": "all",
            "root_uuid": "root",
        }

        await maybe_schedule_redial(server, "root", "failed", {})
        task = server._redial_tasks["root"]
        await asyncio.wait_for(task, timeout=3)

        self.assertEqual(server.queue_calls, 1)
        self.assertFalse(server._auto_redial["root"]["enabled"])
        self.assertIn("do-not-call", server._auto_redial["root"]["last_reason"])

    async def test_drained_no_answer_event_is_forwarded_to_scheduler(self):
        class TimeoutServer:
            def __init__(self):
                self.call_history = FakeHistory()
                self.scheduled = []

            def _sanitize_detail(self, value):
                return str(value)

            async def _maybe_schedule_redial(self, uid, reason, info):
                self.scheduled.append((uid, reason, dict(info)))
                return True

        server = TimeoutServer()
        count = await process_pending_timeouts(server, [{"uuid": "x", "number": "14075550100", "detail": "ring timeout"}])
        self.assertEqual(count, 1)
        self.assertEqual(server.call_history.failed[0][0], "x")
        self.assertEqual(server.call_history.failed[0][1]["outcome"], "no answer")
        self.assertEqual(server.scheduled[0][1], "no answer")


if __name__ == "__main__":
    unittest.main()
