from __future__ import annotations

import asyncio
import base64
import struct
import uuid
import unittest

import aiohttp

from media_config import MediaTransportConfig
from websocket_media import WebSocketMediaServer


class BridgeConfig:
    pbx_to_discord_gain = 1.0
    discord_to_pbx_gain = 1.0


class FakeManager:
    config = BridgeConfig()

    def __init__(self):
        self.session = None
        self.started = asyncio.Event()
        self.ended = asyncio.Event()
        self.published_frames = 0

    async def call_started(self, session):
        self.session = session
        self.started.set()
        return True

    async def call_ended(self, session):
        self.ended.set()

    async def dtmf_received(self, session, digit):
        pass

    async def voicemail_classified(self, session, result, cause):
        pass

    def publish_pbx_frame(self, session, frame):
        self.published_frames += 1

    def push_conference_pcm(self, call_uuid, frame):
        pass


class WebSocketMediaIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = FakeManager()
        self.cfg = MediaTransportConfig(
            transport="websocket",
            websocket_bind="127.0.0.1",
            websocket_port=0,
            websocket_username="discordpbx",
            websocket_password="test-media-password",
            websocket_format="slin16",
            websocket_control_format="plain-text",
            asterisk_connection_id="discordpbx_media",
        )
        self.server = WebSocketMediaServer(self.manager, self.cfg)
        await self.server.start()
        sockets = self.server.site._server.sockets
        self.port = int(sockets[0].getsockname()[1])

    async def asyncTearDown(self):
        if self.manager.session and self.manager.session.active:
            await self.manager.session.close()
        await self.server.close()

    def _auth_header(self, username="discordpbx", password="test-media-password"):
        raw = base64.b64encode(f"{username}:{password}".encode()).decode()
        return {"Authorization": f"Basic {raw}"}

    def _media_start(self) -> str:
        return (
            "MEDIA_START connection_id:test channel:WebSocket/discordpbx_media "
            "channel_id:pbx-test format:slin16 optimal_frame_size:640 ptime:20"
        )

    async def _connect(self, call_uuid: str):
        client = aiohttp.ClientSession()
        ws = await client.ws_connect(
            f"http://127.0.0.1:{self.port}/media?call_uuid={call_uuid}",
            headers=self._auth_header(),
            protocols=("media",),
        )
        return client, ws

    async def test_rejects_bad_media_credentials_before_upgrade(self):
        call_uuid = str(uuid.uuid4())
        async with aiohttp.ClientSession() as client:
            async with client.get(
                f"http://127.0.0.1:{self.port}/media?call_uuid={call_uuid}",
                headers=self._auth_header(password="wrong"),
            ) as response:
                self.assertEqual(response.status, 401)

    async def test_slin16_handshake_and_bidirectional_binary_media(self):
        call_uuid = str(uuid.uuid4())
        client, ws = await self._connect(call_uuid)
        try:
            await ws.send_str(self._media_start())
            await asyncio.wait_for(self.manager.started.wait(), timeout=1.0)

            session = self.manager.session
            self.assertIsNotNone(session)
            self.assertEqual(session.call_uuid, call_uuid)
            self.assertEqual(session.media_transport, "websocket")
            self.assertEqual(session.media_format, "slin16")
            self.assertEqual(session.media_rx_rate, 16000)
            self.assertEqual(session.media_tx_rate, 16000)
            self.assertTrue(session.media_wideband)

            # The transport should immediately produce real 20 ms slin16 frames
            # to Asterisk, even when Discord is currently silent.
            outbound = await asyncio.wait_for(ws.receive(), timeout=1.0)
            self.assertEqual(outbound.type, aiohttp.WSMsgType.BINARY)
            self.assertEqual(len(outbound.data), 640)

            # Stateful sample-rate conversion may produce a few fewer 48 kHz
            # samples on its first call while the filter state initializes.
            # Send two real 20 ms slin16 frames and assert that the canonical
            # 48 kHz Discord frame is subsequently published without ever
            # passing through an 8 kHz transport stage.
            inbound = struct.pack("<h", 5000) * 320
            await ws.send_bytes(inbound)
            await ws.send_bytes(inbound)
            for _ in range(100):
                if session.rx_audio_bytes >= len(inbound) * 2 and self.manager.published_frames >= 1:
                    break
                await asyncio.sleep(0.01)
            self.assertGreaterEqual(session.rx_audio_bytes, len(inbound) * 2)
            self.assertGreaterEqual(self.manager.published_frames, 1)
        finally:
            await ws.close()
            await client.close()

        await asyncio.wait_for(self.manager.ended.wait(), timeout=1.0)

    async def test_binary_media_before_media_start_is_buffered_not_rejected(self):
        """Reproduce the Asterisk 22.6/22.7 startup ordering seen in production."""
        call_uuid = str(uuid.uuid4())
        client, ws = await self._connect(call_uuid)
        early = struct.pack("<h", 2300) * 320
        try:
            # Production Asterisk can emit several BINARY frames before the
            # MEDIA_START control frame becomes visible to the application.
            await ws.send_bytes(early)
            await ws.send_bytes(early)
            await ws.send_bytes(early)
            await asyncio.sleep(0.02)
            self.assertFalse(self.manager.started.is_set())

            await ws.send_str(self._media_start())
            await asyncio.wait_for(self.manager.started.wait(), timeout=1.0)

            session = self.manager.session
            self.assertIsNotNone(session)
            self.assertEqual(session.media_format, "slin16")
            self.assertEqual(session.media_rx_rate, 16000)
            self.assertTrue(session.media_wideband)
            self.assertEqual(session.prestart_media_dropped_frames, 0)

            for _ in range(100):
                if session.rx_audio_bytes >= len(early) * 3:
                    break
                await asyncio.sleep(0.01)
            self.assertGreaterEqual(session.rx_audio_bytes, len(early) * 3)
            self.assertGreaterEqual(session.rx_packets, 3)
        finally:
            await ws.close()
            await client.close()

        await asyncio.wait_for(self.manager.ended.wait(), timeout=1.0)

    async def test_prestart_buffer_is_bounded_and_keeps_recent_audio(self):
        call_uuid = str(uuid.uuid4())
        client, ws = await self._connect(call_uuid)
        early = struct.pack("<h", 1700) * 320
        try:
            # 40 * 20 ms exceeds the 500 ms startup tail. The call must survive,
            # memory remains bounded, and the oldest startup frames are dropped.
            for _ in range(40):
                await ws.send_bytes(early)
            await ws.send_str(self._media_start())
            await asyncio.wait_for(self.manager.started.wait(), timeout=1.0)

            session = self.manager.session
            self.assertIsNotNone(session)
            self.assertGreater(session.prestart_media_dropped_frames, 0)
            self.assertLessEqual(session.prestart_media_frames, 25)
            self.assertLessEqual(session.prestart_media_bytes, 25 * 640)
            self.assertTrue(session.media_wideband)
        finally:
            await ws.close()
            await client.close()

        await asyncio.wait_for(self.manager.ended.wait(), timeout=1.0)


if __name__ == "__main__":
    unittest.main()
