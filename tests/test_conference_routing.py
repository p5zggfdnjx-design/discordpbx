import audioop
import struct
import unittest
from types import SimpleNamespace

from audiosocket import AudioSocketSession, DISCORD_FRAME_BYTES, PBX_FRAME_BYTES
from bridge import BridgeManager


class FakeWriter:
    def get_extra_info(self, _name):
        return None


class FakeAudioManager:
    def __init__(self):
        # Prove conference peer audio does not depend on Discord -> PBX gain.
        self.config = SimpleNamespace(discord_to_pbx_gain=0.0)
        self.discord_to_pbx_master_gain = 0.0


class FakeConferenceSession:
    def __init__(self, call_uuid, workspace_ids):
        self.call_uuid = call_uuid
        self.workspace_ids = list(workspace_ids)
        self.active = True
        self.held = False
        self.listen_enabled = True
        self.talk_enabled = True
        self.conference_group = ""
        self.conference_peer_frames_rx = 0
        self.conference_peer_sources = set()
        self.received = []

    def push_peer_pcm(self, source_id, pcm):
        self.received.append((source_id, pcm))
        self.conference_peer_frames_rx += 1
        self.conference_peer_sources.add(source_id)
        return 1


class ConferenceRoutingTests(unittest.TestCase):
    def test_workspace_conference_routes_only_same_workspace_and_stops_when_disabled(self):
        config = SimpleNamespace(inbound_chime_enabled=False)
        manager = BridgeManager(bot=None, config=config)
        caller_a = FakeConferenceSession("a", ["ws1"])
        caller_b = FakeConferenceSession("b", ["ws1"])
        other_workspace = FakeConferenceSession("c", ["ws2"])
        manager._sessions = {"a": caller_a, "b": caller_b, "c": other_workspace}

        self.assertEqual(manager.set_workspace_conference_mode("ws1", True), 2)
        pcm = b"\x01\x00" * (DISCORD_FRAME_BYTES // 2)

        self.assertEqual(manager.push_conference_pcm("a", pcm), 1)
        self.assertEqual(len(caller_b.received), 1)
        self.assertEqual(caller_b.received[0][1], pcm)
        self.assertEqual(other_workspace.received, [])

        diag = manager.conference_diagnostics("ws1")
        self.assertTrue(diag["enabled"])
        self.assertEqual(diag["eligible_calls"], 2)
        self.assertGreaterEqual(diag["routed_frames"], 1)

        manager.set_workspace_conference_mode("ws1", False)
        self.assertEqual(manager.push_conference_pcm("a", pcm), 0)
        self.assertEqual(len(caller_b.received), 1)

    def test_muted_or_held_caller_is_not_cross_fed(self):
        config = SimpleNamespace(inbound_chime_enabled=False)
        manager = BridgeManager(bot=None, config=config)
        caller_a = FakeConferenceSession("a", ["ws1"])
        caller_b = FakeConferenceSession("b", ["ws1"])
        manager._sessions = {"a": caller_a, "b": caller_b}
        manager.set_workspace_conference_mode("ws1", True)
        pcm = b"\x01\x00" * (DISCORD_FRAME_BYTES // 2)

        caller_a.listen_enabled = False
        self.assertEqual(manager.push_conference_pcm("a", pcm), 0)
        caller_a.listen_enabled = True
        caller_b.held = True
        self.assertEqual(manager.push_conference_pcm("a", pcm), 0)

    def test_peer_audio_reaches_audiosocket_mixer_even_with_discord_gain_zero(self):
        manager = FakeAudioManager()
        session = AudioSocketSession(reader=None, writer=FakeWriter(), manager=manager)

        # 20 ms of non-silent 48 kHz stereo PCM.
        sample = struct.pack("<h", 6000)
        pcm = sample * (DISCORD_FRAME_BYTES // 2)
        queued = session.push_peer_pcm(-99, pcm)

        self.assertGreaterEqual(queued, 1)
        self.assertGreaterEqual(session.conference_peer_frames_rx, 1)
        self.assertIn(-99, session.conference_peer_sources)
        mixed = session._mix_next_pbx_frame()
        self.assertEqual(len(mixed), PBX_FRAME_BYTES)
        self.assertGreater(audioop.rms(mixed, 2), 0)


if __name__ == "__main__":
    unittest.main()
