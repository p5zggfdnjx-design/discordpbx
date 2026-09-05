import unittest
from collections import deque
from types import SimpleNamespace

from bridge import BridgeManager
from media_core import DISCORD_FRAME_BYTES, PcmMediaSession
from media_smoothing_guard import (
    DISCORD_TO_PBX_PREBUFFER_FRAMES,
    PBX_TO_DISCORD_PREBUFFER_FRAMES,
    _tune_page_polling,
    apply,
)


class FakeSession:
    def __init__(self, call_uuid="a", workspace_ids=None):
        self.call_uuid = call_uuid
        self.workspace_ids = list(workspace_ids or ["ws1"])
        self.active = True
        self.listen_enabled = True
        self.talk_enabled = True
        self.held = False
        self.conference_group = ""
        self.conference_peer_frames_rx = 0
        self.conference_peer_sources = set()


class FakeMediaManager:
    def __init__(self):
        self.config = SimpleNamespace(discord_to_pbx_gain=1.0, pbx_to_discord_gain=1.0)
        self.discord_to_pbx_master_gain = 1.0
        self.pbx_to_discord_master_gain = 1.0


class MediaSmoothingGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        apply()

    def test_repeated_conference_state_is_idempotent(self):
        manager = BridgeManager(bot=None, config=SimpleNamespace(inbound_chime_enabled=False))
        manager._sessions = {
            "a": FakeSession("a"),
            "b": FakeSession("b"),
        }
        self.assertEqual(manager.set_workspace_conference_mode("ws1", True), 2)
        manager._workspace_conference_flow_logged.add("ws1")

        # A status refresh re-applying the already-enabled state must not reset
        # the one-shot flow log guard or otherwise mutate conference routing.
        self.assertEqual(manager.set_workspace_conference_mode("ws1", True), 2)
        self.assertIn("ws1", manager._workspace_conference_flow_logged)

    def test_pbx_to_discord_waits_for_small_playout_cushion(self):
        manager = BridgeManager(bot=None, config=SimpleNamespace(inbound_chime_enabled=False))
        session = FakeSession("a")
        manager._sessions = {"a": session}
        frame = b"\x01\x00" * (DISCORD_FRAME_BYTES // 2)
        q = deque([frame] * (PBX_TO_DISCORD_PREBUFFER_FRAMES - 1), maxlen=10)
        manager._workspace_call_audio = {"ws1": {"a": q}}

        self.assertEqual(manager.read_workspace_discord_frame("ws1"), b"\x00" * DISCORD_FRAME_BYTES)
        q.append(frame)
        self.assertEqual(manager.read_workspace_discord_frame("ws1"), frame)
        self.assertGreaterEqual(manager._pbx_to_discord_rebuffers, 1)

    def test_discord_to_pbx_waits_for_small_playout_cushion(self):
        session = PcmMediaSession(
            FakeMediaManager(),
            media_transport="websocket",
            media_format="slin16",
            sample_rate=16000,
        )
        frame = b"\x01\x00" * (session.tx_frame_bytes // 2)
        state = session._users[123]
        for _ in range(DISCORD_TO_PBX_PREBUFFER_FRAMES - 1):
            state.frames.append(frame)

        self.assertEqual(session.next_outbound_frame(), b"\x00" * session.tx_frame_bytes)
        state.frames.append(frame)
        self.assertEqual(session.next_outbound_frame(), frame)
        self.assertGreaterEqual(session._discord_to_pbx_rebuffers, 1)

    def test_operator_polling_is_reduced_without_touching_normal_refresh(self):
        page = (
            "window.setInterval(tick, 300);"
            "tick();setInterval(tick,1000);"
            "refreshTimer=setInterval(refreshStatus,10000);"
        )
        tuned = _tune_page_polling(page)
        self.assertIn("window.setInterval(tick, 750);", tuned)
        self.assertIn("tick();setInterval(tick,2000);", tuned)
        self.assertIn("refreshTimer=setInterval(refreshStatus,10000);", tuned)


if __name__ == "__main__":
    unittest.main()
