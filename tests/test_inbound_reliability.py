from __future__ import annotations

import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import inbound_routing_guard
from bridge import BridgeManager
import voice_lifecycle
from workspace_service import WorkspaceService

inbound_routing_guard.apply()


class FakeVoiceClient:
    def __init__(self, guild, channel, connected=True):
        self.guild = guild
        self.channel = channel
        self.connected = connected
        self.playing = False
        self.listening = False
        self.disconnect_calls = 0
        self.cleaned = False

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
        self.cleaned = True
        if self.guild.voice_client is self:
            self.guild.voice_client = None


class FakeVoiceChannel:
    def __init__(self, guild, channel_id=2, failures=0):
        self.guild = guild
        self.id = channel_id
        self.name = "PBX"
        self.failures = failures
        self.connect_calls = 0
        self.last_connect_kwargs = {}

    def __str__(self):
        return self.name

    async def connect(self, cls=None, self_deaf=False, self_mute=False, reconnect=True):
        self.connect_calls += 1
        self.last_connect_kwargs = {
            "cls": cls,
            "self_deaf": self_deaf,
            "self_mute": self_mute,
            "reconnect": reconnect,
        }
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("transient voice connect failure")
        vc = FakeVoiceClient(self.guild, self, connected=True)
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
    voice_connect_attempts = 3
    voice_connect_timeout = 3.0
    voice_ready_timeout = 3.0
    voice_watchdog_interval = 2.0
    voice_unhealthy_grace = 3.0
    voice_worker_settle_timeout = 0.5
    voice_worker_settle_poll = 0.01
    inbound_pending_ttl = 30.0
    inbound_voice_prewarm = True


def make_manager(*, failures=0):
    guild = FakeGuild()
    channel = FakeVoiceChannel(guild, failures=failures)
    guild.channels[channel.id] = channel
    bot = FakeBot(guild, channel)
    manager = BridgeManager(bot, Config())
    return manager, guild, channel


class VoiceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        manager = getattr(self, "manager", None)
        if manager is not None:
            await manager.close_voice_lifecycle()

    async def test_stale_voice_client_is_discarded_before_inbound_connect(self):
        self.manager, guild, channel = make_manager()
        stale = FakeVoiceClient(guild, channel, connected=False)
        guild.voice_client = stale

        with patch.object(voice_lifecycle.discord, "VoiceChannel", FakeVoiceChannel), patch.object(
            voice_lifecycle.voice_recv, "VoiceRecvClient", FakeVoiceClient
        ):
            vc = await self.manager.ensure_voice("")

        self.assertIsNot(vc, stale)
        self.assertGreaterEqual(stale.disconnect_calls, 1)
        self.assertTrue(vc.is_connected())
        self.assertTrue(vc.is_playing())
        self.assertTrue(vc.is_listening())
        self.assertEqual(channel.connect_calls, 1)
        self.assertIs(channel.last_connect_kwargs["reconnect"], False)

    async def test_transient_voice_connect_failure_retries(self):
        self.manager, _, channel = make_manager(failures=2)
        with patch.object(voice_lifecycle.discord, "VoiceChannel", FakeVoiceChannel), patch.object(
            voice_lifecycle.voice_recv, "VoiceRecvClient", FakeVoiceClient
        ):
            vc = await self.manager.ensure_voice("")

        self.assertTrue(vc.is_connected())
        self.assertEqual(channel.connect_calls, 3)
        self.assertIs(channel.last_connect_kwargs["reconnect"], False)

    async def test_no_route_records_bridge_failure_and_cleans_pending(self):
        self.manager, _, _ = make_manager()
        call_uuid = str(uuid.uuid4())
        self.manager.prepare_inbound(call_uuid, "4075551212", "Caller", workspace_ids=[])
        events = []

        async def capture(event, payload):
            events.append((event, payload))

        self.manager.event_callback = capture
        session = SimpleNamespace(call_uuid=call_uuid, active=True)
        ok = await self.manager.call_started(session)

        self.assertFalse(ok)
        self.assertIsNone(self.manager.get_pending(call_uuid))
        self.assertIsNone(self.manager.get_session(call_uuid))
        self.assertEqual(self.manager.history[0]["event"], "connected")


class PendingRegistrationTests(unittest.TestCase):
    def test_stale_inbound_registration_is_pruned(self):
        manager, _, _ = make_manager()
        call_uuid = str(uuid.uuid4())
        manager.prepare_inbound(call_uuid, "4075551212", workspace_ids=["ws1"])
        with manager._sessions_lock:
            manager._pending[call_uuid]["deadline_ts"] = time.time() - 1

        self.assertFalse(manager._workspace_has_voice_work("ws1"))
        self.assertIsNone(manager.get_pending(call_uuid))
        self.assertEqual(manager.history[0]["event"], "inbound registration expired")


class FakeRoutingDB:
    def __init__(self, mode="manual", fallback="default"):
        self.ws = {
            "id": "ws_main",
            "guild_id": "1",
            "alias": "Main",
            "enabled": True,
            "accept_inbound": True,
            "auto_route": True,
            "priority": 1,
        }
        self.mode = mode
        self.fallback = fallback

    def get_setting(self, key, default=None):
        if key == "inbound_routing":
            return {
                "mode": self.mode,
                "targets": ["ws_deleted"],
                "fallback": self.fallback,
                "override_expires": 0,
            }
        if key == "default_workspace_id":
            return "ws_main"
        return default

    def list_workspaces(self, *args, **kwargs):
        return [dict(self.ws)]

    def get_workspace(self, workspace_id):
        return dict(self.ws) if workspace_id == "ws_main" else None


class RoutingFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_route_with_deleted_target_uses_default_fallback(self):
        service = WorkspaceService.__new__(WorkspaceService)
        service.db = FakeRoutingDB(mode="manual", fallback="default")
        service.bot = SimpleNamespace()
        service.config = SimpleNamespace()

        selected = await service.resolve_inbound_workspaces()
        self.assertEqual([x["id"] for x in selected], ["ws_main"])

    async def test_dnd_never_falls_back(self):
        service = WorkspaceService.__new__(WorkspaceService)
        service.db = FakeRoutingDB(mode="dnd", fallback="default")
        service.bot = SimpleNamespace()
        service.config = SimpleNamespace()

        selected = await service.resolve_inbound_workspaces()
        self.assertEqual(selected, [])


if __name__ == "__main__":
    unittest.main()
