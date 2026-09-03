from __future__ import annotations

import unittest

import discord_join_chime
from audiosocket import DISCORD_FRAME_BYTES
from bridge import BridgeManager


discord_join_chime.apply()


class Config:
    inbound_chime_enabled = False
    inbound_chime_file = ""
    inbound_chime_gain = 1.0
    ami_dial_timeout_ms = 45000
    guild_id = 1
    voice_channel_id = 2
    text_channel_id = 0
    max_simultaneous_calls = 15
    leave_voice_after_call_seconds = 0
    pbx_to_discord_gain = 1.0
    discord_to_pbx_gain = 1.0


class Bot:
    pass


class DiscordJoinChimeTests(unittest.TestCase):
    def make_manager(self):
        return BridgeManager(Bot(), Config())

    def test_builtin_chime_is_valid_discord_pcm(self):
        manager = self.make_manager()
        frames = manager._discord_join_chime_frames
        self.assertGreater(len(frames), 10)
        self.assertTrue(all(len(frame) == DISCORD_FRAME_BYTES for frame in frames))
        self.assertEqual(manager._discord_join_chime_source, "builtin-skype-style")

    def test_same_voice_client_only_announces_once(self):
        manager = self.make_manager()
        vc = object()
        self.assertTrue(manager._queue_discord_join_chime("ws-main", vc))
        first_len = len(manager._workspace_alert_frames["ws-main"])
        self.assertFalse(manager._queue_discord_join_chime("ws-main", vc))
        self.assertEqual(len(manager._workspace_alert_frames["ws-main"]), first_len)
        self.assertEqual(manager._discord_join_chime_plays["ws-main"], 1)

    def test_new_voice_client_announces_again(self):
        manager = self.make_manager()
        self.assertTrue(manager._queue_discord_join_chime("ws-main", object()))
        self.assertTrue(manager._queue_discord_join_chime("ws-main", object()))
        self.assertEqual(manager._discord_join_chime_plays["ws-main"], 2)

    def test_chime_is_local_discord_audio_only(self):
        manager = self.make_manager()
        manager._queue_discord_join_chime("ws-main", object())
        frame = manager.read_local_discord_frame("ws-main")
        self.assertNotEqual(frame, b"\x00" * DISCORD_FRAME_BYTES)


if __name__ == "__main__":
    unittest.main()
