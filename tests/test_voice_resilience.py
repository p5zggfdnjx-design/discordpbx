from __future__ import annotations

import asyncio
import threading
import unittest

from voice_resilience import VoiceResilientBridgeManager, _format_thread_stack


class _RepairHarness(VoiceResilientBridgeManager):
    FAST_REPAIR_DELAY_SECONDS = 0.01

    def __init__(self):
        # Deliberately avoid constructing Discord objects; these tests exercise
        # only the resilience layer's coalescing and guard conditions.
        self._voice_loop = None
        self._voice_suppressed = set()
        self._voice_busy = set()
        self._voice_errors = {}
        self._fast_voice_repairs = {}
        self._fast_voice_repair_attempts = 0
        self._fast_voice_repair_successes = 0
        self._fast_voice_repair_failures = 0
        self.has_work = True
        self.ensure_calls = 0

    def _workspace_has_voice_work(self, workspace_id: str) -> bool:
        return self.has_work

    async def ensure_voice(self, workspace_id: str = ""):
        self.ensure_calls += 1
        await asyncio.sleep(0.01)
        return object()

    def _start_voice_watchdog(self, workspace_id: str) -> None:
        pass


class VoiceResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_repair_coalesces_duplicate_worker_callbacks(self):
        manager = _RepairHarness()
        manager._voice_loop = asyncio.get_running_loop()
        manager._start_fast_voice_repair("ws-test", "receive-stopped")
        manager._start_fast_voice_repair("ws-test", "playback-stopped")
        await asyncio.sleep(0.08)

        self.assertEqual(manager.ensure_calls, 1)
        self.assertEqual(manager._fast_voice_repair_attempts, 1)
        self.assertEqual(manager._fast_voice_repair_successes, 1)
        self.assertEqual(manager._fast_voice_repair_failures, 0)
        self.assertFalse(manager._fast_voice_repairs)

    async def test_fast_repair_does_not_rejoin_idle_or_suppressed_workspace(self):
        manager = _RepairHarness()
        manager._voice_loop = asyncio.get_running_loop()
        manager.has_work = False
        manager._start_fast_voice_repair("ws-idle", "receive-stopped")
        await asyncio.sleep(0.03)
        self.assertEqual(manager.ensure_calls, 0)

        manager.has_work = True
        manager._voice_suppressed.add("ws-idle")
        manager._start_fast_voice_repair("ws-idle", "receive-stopped")
        await asyncio.sleep(0.03)
        self.assertEqual(manager.ensure_calls, 0)

    async def test_stack_formatter_can_capture_current_python_thread(self):
        stack = _format_thread_stack(threading.get_ident(), limit=10)
        self.assertIn("test_stack_formatter_can_capture_current_python_thread", stack)


if __name__ == "__main__":
    unittest.main()
