from __future__ import annotations

import asyncio
import unittest
from collections import deque
from unittest.mock import patch

import inbound_stability_guard as guard


class InboundStabilityGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_hangup_sound_burst_goes_quiet_after_limit(self):
        history = deque()
        with patch.object(guard, "HANGUP_SOUND_BURST_LIMIT", 3), patch.object(
            guard, "HANGUP_SOUND_WINDOW_SECONDS", 60.0
        ):
            self.assertTrue(guard._allow_hangup_cue(history, 0.0))
            self.assertTrue(guard._allow_hangup_cue(history, 1.0))
            self.assertTrue(guard._allow_hangup_cue(history, 2.0))
            self.assertFalse(guard._allow_hangup_cue(history, 3.0))
            self.assertFalse(guard._allow_hangup_cue(history, 4.0))
            # Once the entire burst ages out, the cue becomes audible again.
            self.assertTrue(guard._allow_hangup_cue(history, 65.0))

    def test_hangup_sound_limit_zero_is_fully_silent(self):
        history = deque()
        with patch.object(guard, "HANGUP_SOUND_BURST_LIMIT", 0), patch.object(
            guard, "HANGUP_SOUND_WINDOW_SECONDS", 60.0
        ):
            self.assertFalse(guard._allow_hangup_cue(history, 10.0))
            self.assertFalse(guard._allow_hangup_cue(history, 11.0))
            self.assertEqual(len(history), 2)

    async def test_handshake_returns_as_soon_as_voice_is_healthy(self):
        fake_server = object()
        checks = iter([False, False, True])

        def ready(_server, _wid):
            try:
                return next(checks)
            except StopIteration:
                return True

        with patch.object(guard, "HANDSHAKE_WAIT_SECONDS", 0.25), patch.object(
            guard, "HANDSHAKE_POLL_SECONDS", 0.01
        ), patch.object(guard, "_voice_is_ready", side_effect=ready):
            started = asyncio.get_running_loop().time()
            self.assertTrue(await guard._wait_for_selected_voice(fake_server, ["ws1"]))
            self.assertLess(asyncio.get_running_loop().time() - started, 0.20)

    async def test_handshake_timeout_does_not_raise_or_reject(self):
        fake_server = object()
        with patch.object(guard, "HANDSHAKE_WAIT_SECONDS", 0.04), patch.object(
            guard, "HANDSHAKE_POLL_SECONDS", 0.01
        ), patch.object(guard, "_voice_is_ready", return_value=False):
            self.assertFalse(await guard._wait_for_selected_voice(fake_server, ["ws1"]))

    async def test_empty_route_does_not_wait(self):
        fake_server = object()
        with patch.object(guard, "_voice_is_ready") as ready:
            self.assertFalse(await guard._wait_for_selected_voice(fake_server, []))
            ready.assert_not_called()


if __name__ == "__main__":
    unittest.main()
