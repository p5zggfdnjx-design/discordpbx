from __future__ import annotations

import asyncio
import unittest
import uuid
from unittest.mock import patch

from bridge import BridgeManager
import voice_lifecycle


class FakeVoiceClient:
    def __init__(self, guild, channel, *, delayed_workers: int = 0):
        self.guild = guild
        self.channel = channel
        self.connected = True
        self._play_started = False
        self._listen_started = False
        self._play_delay = int(delayed_workers)
        self._listen_delay = int(delayed_workers)
        self.disconnect_calls = 0

    def is_connected(self):
        return self.connected

    def is_playing(self):
        if not self._play_started:
            return False
        if self._play_delay > 0:
            self._play_delay -= 1
            return False
        return True

    def is_listening(self):
        if not self._listen_started:
            return False
        if self._listen_delay > 0:
            self._listen_delay -= 1
            return False
        return True

    def play(self, source, after=None):
        self._play_started = True
        self.play_after = after

    def listen(self, sink, after=None):
        self._listen_started = True
        self.listen_after = after

    def stop_playing(self):
        self._play_started = False

    def stop_listening(self):
        self._listen_started = False

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
    def __init__(self, guild, channel_id=2, *, delayed_workers=0, failures=0):
        self.guild = guild
        self.id = channel_id
        self.name = "PBX"
        self.delayed_workers = int(delayed_workers)
        self.failures = int(failures)
        self.connect_calls = 0
        self.connect_started = asyncio.Event()
        self.last_reconnect = None

    async def connect(self, cls=None, self_deaf=False, self_mute=False, reconnect=True):
        self.connect_calls += 1
        self.connect_started.set()
        self.last_reconnect = reconnect
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("transient voice connect failure")
        vc = FakeVoiceClient(self.guild, self, delayed_workers=self.delayed_workers)
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
    voice_connect_attempts = 3
    voice_connect_timeout = 3.0
    voice_ready_timeout = 3.0
    voice_watchdog_interval = 2.0
    voice_unhealthy_grace = 3.0
    voice_worker_settle_timeout = 2.5
    voice_worker_settle_poll = 0.05
    inbound_pending_ttl = 30.0
    inbound_voice_prewarm = True


def make_manager(*, delayed_workers=0, failures=0):
    guild = FakeGuild()
    channel = FakeVoiceChannel(
        guild,
        delayed_workers=delayed_workers,
        failures=failures,
    )
    guild.channels[channel.id] = channel
    manager = BridgeManager(FakeBot(guild, channel), Config())
    return manager, guild, channel


class FirstCallPickupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        manager = getattr(self, "manager", None)
        if manager is not None:
            await manager.close_voice_lifecycle()

    async def test_cold_voice_workers_get_settle_time_before_reconnect(self):
        self.manager, guild, channel = make_manager(delayed_workers=3)
        self.manager.config.voice_worker_settle_timeout = 0.5
        self.manager.config.voice_worker_settle_poll = 0.01
        with patch.object(voice_lifecycle.discord, "VoiceChannel", FakeVoiceChannel), patch.object(
            voice_lifecycle.voice_recv, "VoiceRecvClient", FakeVoiceClient
        ):
            vc = await self.manager.ensure_voice("")

        self.assertIs(vc, guild.voice_client)
        self.assertTrue(vc.is_connected())
        self.assertTrue(vc.is_playing())
        self.assertTrue(vc.is_listening())
        self.assertEqual(channel.connect_calls, 1)
        self.assertIs(channel.last_reconnect, False)
        await self.manager.disconnect_voice("")

    async def test_inbound_registration_starts_voice_prewarm(self):
        self.manager, _, channel = make_manager()
        self.manager._workspace_voice_config = lambda workspace_id="": (str(workspace_id), 1, 2, 0)
        call_uuid = str(uuid.uuid4())

        with patch.object(voice_lifecycle.discord, "VoiceChannel", FakeVoiceChannel), patch.object(
            voice_lifecycle.voice_recv, "VoiceRecvClient", FakeVoiceClient
        ):
            self.manager.prepare_inbound(call_uuid, "4075551212", "Caller", workspace_ids=["ws_main"])
            await asyncio.wait_for(channel.connect_started.wait(), timeout=0.5)
            tasks = list(self.manager._inbound_prewarm_tasks.values())
            if tasks:
                await asyncio.gather(*tasks)

        self.assertEqual(channel.connect_calls, 1)
        self.assertIsNotNone(self.manager.get_pending(call_uuid))
        self.assertEqual(self.manager._inbound_prewarm_successes.get("ws_main"), 1)
        self.manager.cancel_pending(call_uuid)
        await self.manager.disconnect_voice("ws_main")

    async def test_failed_prewarm_does_not_reject_pending_call(self):
        self.manager, _, channel = make_manager(failures=6)
        self.manager.config.voice_connect_attempts = 2
        self.manager._workspace_voice_config = lambda workspace_id="": (str(workspace_id), 1, 2, 0)
        call_uuid = str(uuid.uuid4())

        with patch.object(voice_lifecycle.discord, "VoiceChannel", FakeVoiceChannel), patch.object(
            voice_lifecycle.voice_recv, "VoiceRecvClient", FakeVoiceClient
        ):
            self.manager.prepare_inbound(call_uuid, "4075551212", "Caller", workspace_ids=["ws_main"])
            await asyncio.wait_for(channel.connect_started.wait(), timeout=0.5)
            tasks = list(self.manager._inbound_prewarm_tasks.values())
            if tasks:
                await asyncio.gather(*tasks)

        self.assertIsNotNone(self.manager.get_pending(call_uuid))
        self.assertEqual(channel.connect_calls, 2)
        self.assertEqual(self.manager._inbound_prewarm_failures.get("ws_main"), 1)
        self.manager.cancel_pending(call_uuid)


if __name__ == "__main__":
    unittest.main()
