from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

import discord_sound_pack
from audiosocket import DISCORD_FRAME_BYTES
from bridge import BridgeManager


class Config:
    guild_id = 1
    voice_channel_id = 2
    text_channel_id = 0
    leave_voice_after_call_seconds = 0
    max_simultaneous_calls = 15
    ami_dial_timeout_ms = 45000
    pbx_to_discord_gain = 1.0
    discord_to_pbx_gain = 1.0
    inbound_chime_enabled = False
    inbound_chime_file = ""
    inbound_chime_gain = 1.0


class FakeBot:
    def is_ready(self):
        return True

    def get_guild(self, _guild_id):
        return None

    def get_channel(self, _channel_id):
        return None

    async def fetch_channel(self, _channel_id):
        return None


class Session:
    def __init__(self, uid="call-1"):
        self.call_uuid = uid
        self.active = True
        self.held = False
        self.listen_enabled = True
        self.talk_enabled = True
        self.workspace_ids = ["ws"]
        self.direction = "inbound"
        self.remote_number = "4075550100"
        self.caller_id = ""
        self.contact_name = "Caller"
        self.source = "inbound"
        self.rx_audio_bytes = 0
        self.tx_audio_bytes = 0
        self.voicemail_hangup = False
        self.voicemail_detection_result = ""
        self.voicemail_detection_cause = ""
        self.randomize_caller_id = False
        self.retry_of = ""
        self.retry_index = 0
        self.operator_user_id = ""
        self.operator_name = ""
        self._started = time.monotonic()

    @property
    def age_seconds(self):
        return max(0.0, time.monotonic() - self._started)


class DiscordSoundPackTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._originals = {
            name: getattr(BridgeManager, name, None)
            for name in (
                "__init__", "ensure_voice", "set_hold", "cancel_pending", "fail_pending",
                "call_ended", "schedule_voice_idle_disconnect", "status_dict",
                "_queue_discord_sound", "_discord_sound_pack_guard",
            )
        }
        fake_frames = [b"\x01\x00" * (DISCORD_FRAME_BYTES // 2)] * 10
        cls._decode_patch = patch.object(discord_sound_pack, "_decode_sound", return_value=fake_frames)
        cls._decode_patch.start()
        discord_sound_pack.apply()

    @classmethod
    def tearDownClass(cls):
        cls._decode_patch.stop()
        for name, value in cls._originals.items():
            if value is None:
                try:
                    delattr(BridgeManager, name)
                except AttributeError:
                    pass
            else:
                setattr(BridgeManager, name, value)

    def make_manager(self):
        manager = BridgeManager(FakeBot(), Config())
        manager._workspace_voice_config = lambda workspace_id="": (str(workspace_id or "ws"), 1, 2, 0)
        return manager

    async def asyncTearDown(self):
        await asyncio.sleep(0)

    async def test_pack_loads_all_bundled_events_and_replaces_ringback(self):
        manager = self.make_manager()
        self.assertEqual(set(manager._discord_sound_frames), set(discord_sound_pack.SOUND_FILES))
        self.assertIs(manager._ringback_frames, manager._discord_sound_frames["outbound_ring"])

    async def test_hold_decline_and_failure_queue_local_discord_cues(self):
        manager = self.make_manager()
        session = Session()
        manager._sessions[session.call_uuid] = session

        self.assertTrue(manager.set_hold(session.call_uuid, True))
        self.assertEqual(manager._discord_sound_counts.get("hold"), 1)
        self.assertTrue(manager._workspace_alert_frames.get("ws"))

        manager._pending["decline-1"] = {"direction": "inbound", "workspace_ids": ["ws"]}
        manager.cancel_pending("decline-1")
        self.assertEqual(manager._discord_sound_counts.get("declined"), 1)

        manager._pending["fail-1"] = {"direction": "outbound", "workspace_ids": ["ws"]}
        manager.fail_pending("fail-1", "test failure")
        self.assertEqual(manager._discord_sound_counts.get("failed"), 1)

    async def test_hangup_queues_before_idle_disconnect_and_preserves_cue_time(self):
        manager = self.make_manager()
        session = Session("ended-1")
        manager._sessions[session.call_uuid] = session

        await manager.call_ended(session)
        self.assertEqual(manager._discord_sound_counts.get("hangup"), 1)
        self.assertGreater(manager._discord_sound_hold_until.get("ws", 0), time.monotonic())
        task = manager._leave_tasks.get("ws")
        self.assertIsNotNone(task)
        self.assertFalse(task.done())
        manager._cancel_voice_leave("ws")

    async def test_cues_live_in_local_mixer_not_pbx_session_audio(self):
        manager = self.make_manager()
        session = Session("isolation-1")
        manager._sessions[session.call_uuid] = session
        before = int(session.tx_audio_bytes)
        manager._queue_discord_sound("hold", ["ws"])
        self.assertTrue(manager._workspace_alert_frames.get("ws"))
        self.assertEqual(session.tx_audio_bytes, before)


if __name__ == "__main__":
    unittest.main()
