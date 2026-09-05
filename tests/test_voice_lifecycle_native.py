from __future__ import annotations

import asyncio
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

# Import the public bridge first. bridge.py publishes its core primitives before
# loading the native lifecycle subclass, which is the same order used in production.
from bridge import BridgeManager
import voice_lifecycle
from voice_lifecycle import ReliableBridgeManager
from voice_resilience import VoiceResilientBridgeManager


class Config:
    inbound_chime_enabled = False
    inbound_chime_file = ""
    inbound_chime_gain = 1.0
    ami_dial_timeout_ms = 45000
    guild_id = 1
    voice_channel_id = 2
    text_channel_id = 0
    max_simultaneous_calls = 15
    leave_voice_after_call_seconds = 0.02
    voice_connect_attempts = 2
    voice_connect_timeout = 3.0
    voice_ready_timeout = 3.0
    voice_watchdog_interval = 0.5
    voice_unhealthy_grace = 1.0
    voice_worker_settle_timeout = 0.2
    voice_worker_settle_poll = 0.01
    inbound_pending_ttl = 5.0
    inbound_voice_prewarm = False


class FakeVoiceClient:
    def __init__(self, guild, channel):
        self.guild = guild
        self.channel = channel
        self.connected = True
        self.playing = False
        self.listening = False
        self.disconnect_calls = 0

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self.playing

    def is_listening(self):
        return self.listening

    def play(self, source, after=None):
        self.playing = True
        self.play_after = after

    def listen(self, sink, after=None):
        self.listening = True
        self.listen_after = after

    def stop_playing(self):
        self.playing = False

    def stop_listening(self):
        self.listening = False

    async def move_to(self, channel):
        self.channel = channel

    async def disconnect(self, force=False):
        self.disconnect_calls += 1
        self.connected = False
        if self.guild.voice_client is self:
            self.guild.voice_client = None

    def cleanup(self):
        if self.guild.voice_client is self:
            self.guild.voice_client = None


class FakeVoiceChannel:
    def __init__(self, guild, channel_id=2):
        self.guild = guild
        self.id = channel_id
        self.name = "PBX"
        self.connect_calls = []

    def __str__(self):
        return self.name

    async def connect(self, **kwargs):
        self.connect_calls.append(dict(kwargs))
        vc = FakeVoiceClient(self.guild, self)
        self.guild.voice_client = vc
        return vc


class FakeGuild:
    def __init__(self):
        self.voice_client = None
        self.channels = {}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)


class FakeBot:
    def __init__(self, guild, channel):
        self.guild = guild
        self.channel = channel

    async def wait_until_ready(self):
        return None

    def get_guild(self, guild_id):
        return self.guild if guild_id == 1 else None

    def get_channel(self, channel_id):
        return self.channel if channel_id == self.channel.id else None

    async def fetch_channel(self, channel_id):
        return self.get_channel(channel_id)

    def is_ready(self):
        return True

    @property
    def voice_clients(self):
        return [self.guild.voice_client] if self.guild.voice_client else []


class FakeWorkspaceDB:
    def get_workspace(self, workspace_id):
        if workspace_id == "ws1":
            return {
                "id": "ws1",
                "guild_id": 1,
                "voice_channel_id": 2,
                "text_channel_id": 0,
            }
        return None

    def list_workspaces(self):
        return [self.get_workspace("ws1")]


class FakeWorkspaceProvider:
    def __init__(self):
        self.db = FakeWorkspaceDB()

    def default_workspace(self):
        return self.db.get_workspace("ws1")

    def workspace_for_guild(self, guild_id):
        return self.db.get_workspace("ws1") if guild_id == 1 else None


def make_manager():
    guild = FakeGuild()
    channel = FakeVoiceChannel(guild)
    guild.channels[channel.id] = channel
    manager = ReliableBridgeManager(FakeBot(guild, channel), Config())
    manager.workspace_provider = FakeWorkspaceProvider()
    return manager, guild, channel


class NativeVoiceArchitectureTests(unittest.TestCase):
    def test_public_bridge_uses_native_lifecycle_manager(self):
        self.assertIs(BridgeManager, VoiceResilientBridgeManager)
        self.assertTrue(issubclass(BridgeManager, ReliableBridgeManager))

    def test_inactive_session_does_not_keep_workspace_busy(self):
        manager, _, _ = make_manager()
        manager._sessions["dead"] = SimpleNamespace(active=False, workspace_ids=["ws1"])
        self.assertFalse(manager._workspace_has_voice_work("ws1"))

    def test_live_session_keeps_workspace_busy(self):
        manager, _, _ = make_manager()
        manager._sessions["live"] = SimpleNamespace(active=True, workspace_ids=["ws1"])
        self.assertTrue(manager._workspace_has_voice_work("ws1"))

    def test_inbound_registration_has_bounded_ttl(self):
        manager, _, _ = make_manager()
        call_uuid = str(uuid.uuid4())
        manager.prepare_inbound(call_uuid, "4075551212", workspace_ids=["ws1"])
        pending = manager.get_pending(call_uuid)
        self.assertIsNotNone(pending)
        self.assertGreater(float(pending["deadline_ts"]), time.time())
        self.assertGreater(float(pending["created_ts"]), 0)
        manager._cancel_inbound_expiry(call_uuid)


class NativeVoiceAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        manager = getattr(self, "manager", None)
        if manager is not None:
            await manager.close_voice_lifecycle()

    async def test_discord_builtin_reconnect_is_explicitly_disabled(self):
        self.manager, _, channel = make_manager()
        with patch.object(voice_lifecycle.discord, "VoiceChannel", FakeVoiceChannel), patch.object(
            voice_lifecycle.voice_recv, "VoiceRecvClient", FakeVoiceClient
        ):
            vc = await self.manager.ensure_voice("ws1")
        self.assertTrue(vc.is_connected())
        self.assertEqual(len(channel.connect_calls), 1)
        self.assertIs(channel.connect_calls[0].get("reconnect"), False)

    async def test_idle_timer_is_not_pushed_back_by_duplicate_schedule(self):
        self.manager, _, _ = make_manager()
        calls = []

        async def fake_disconnect(workspace_id=None, _from_idle_task=False):
            calls.append((workspace_id, _from_idle_task, asyncio.get_running_loop().time()))

        self.manager.disconnect_voice = fake_disconnect
        started = asyncio.get_running_loop().time()
        self.manager.schedule_voice_idle_disconnect("ws1", delay=0.03)
        await asyncio.sleep(0.015)
        # A later status/call-ended pass must not restart the existing clock.
        self.manager.schedule_voice_idle_disconnect("ws1", delay=0.20)
        await asyncio.sleep(0.05)
        self.assertEqual(len(calls), 1)
        self.assertLess(calls[0][2] - started, 0.10)

    async def test_ended_call_cannot_trigger_watchdog_rejoin(self):
        self.manager, _, _ = make_manager()
        session = SimpleNamespace(active=False, workspace_ids=["ws1"])
        self.manager._sessions["dead"] = session
        self.manager._start_voice_watchdog("ws1")
        await asyncio.sleep(0)
        self.assertNotIn("ws1", self.manager._voice_watchdogs)


if __name__ == "__main__":
    unittest.main()
