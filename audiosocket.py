from __future__ import annotations

import asyncio
import logging
import struct
import uuid
from typing import Optional

from media_config import MediaTransportConfig
from media_core import (
    DISCORD_FRAME_BYTES,
    DISCORD_RATE,
    LEGACY_PBX_RATE,
    PcmMediaSession,
    _VoicemailDetector,
    _limit_pcm,
    frame_bytes,
)


log = logging.getLogger("discord-pbx.audiosocket")

TYPE_HANGUP = 0x00
TYPE_UUID = 0x01
TYPE_DTMF = 0x03
TYPE_ERROR = 0xFF

AUDIO_RATES = {
    0x10: 8000,
    0x11: 12000,
    0x12: 16000,
    0x13: 24000,
    0x14: 32000,
    0x15: 44100,
    0x16: 48000,
    0x17: 96000,
    0x18: 192000,
}
RATE_TO_PACKET = {rate: packet for packet, rate in AUDIO_RATES.items()}
RATE_TO_FORMAT = {
    8000: "slin",
    12000: "slin12",
    16000: "slin16",
    24000: "slin24",
    32000: "slin32",
    44100: "slin44",
    48000: "slin48",
    96000: "slin96",
    192000: "slin192",
}

# Backwards-compatible constants used by tests/bridge imports.
PBX_RATE = LEGACY_PBX_RATE
PBX_FRAME_BYTES = frame_bytes(PBX_RATE)


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(3)
    packet_type = header[0]
    length = struct.unpack(">H", header[1:])[0]
    payload = await reader.readexactly(length) if length else b""
    return packet_type, payload


async def write_packet(writer: asyncio.StreamWriter, packet_type: int, payload: bytes = b"") -> None:
    writer.write(bytes((packet_type,)) + struct.pack(">H", len(payload)) + payload)
    await writer.drain()


class AudioSocketSession(PcmMediaSession):
    """Legacy Asterisk AudioSocket transport.

    The standard Asterisk AudioSocket() dialplan application is 8 kHz. This
    class now uses the same transport-independent PCM engine as the HD WebSocket
    path and mirrors an actually received AudioSocket sample rate natively. It
    never fabricates an HD classification by merely upsampling 8 kHz input.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, manager):
        super().__init__(manager, media_transport="audiosocket", media_format="slin", sample_rate=PBX_RATE)
        self.reader = reader
        self.writer = writer
        self.peer = writer.get_extra_info("peername")
        self._sender_task: Optional[asyncio.Task] = None
        self._write_lock = asyncio.Lock()
        self._manager_started = False

    async def run(self) -> None:
        try:
            packet_type, payload = await asyncio.wait_for(read_packet(self.reader), timeout=5.0)
            if packet_type != TYPE_UUID or len(payload) != 16:
                raise RuntimeError("first AudioSocket packet must be a 16-byte UUID packet")
            self.call_uuid = str(uuid.UUID(bytes=payload))
            log.info("AudioSocket call %s connected from %s", self.call_uuid, self.peer)

            accepted = await self.manager.call_started(self)
            if not accepted:
                await write_packet(self.writer, TYPE_HANGUP)
                return
            self._manager_started = True
            self._sender_task = asyncio.create_task(
                self._discord_to_pbx_sender(), name=f"pbx-send-{self.call_uuid}"
            )

            while self.active:
                packet_type, payload = await read_packet(self.reader)
                self.rx_packets += 1
                if packet_type == TYPE_HANGUP:
                    break
                if packet_type in AUDIO_RATES:
                    rate = AUDIO_RATES[packet_type]
                    # This is actual transport negotiation/evidence. Standard
                    # app_audiosocket will remain at 8 kHz; a genuinely wider
                    # AudioSocket channel may advertise a higher packet type.
                    if rate != self.media_sample_rate:
                        self.set_media_rate(rate, RATE_TO_FORMAT.get(rate, f"slin{rate // 1000}"))
                    self.rx_audio_bytes += len(payload)
                    self._feed_pbx_audio(payload, rate)
                elif packet_type == TYPE_DTMF and payload:
                    digit = payload[:1].decode("ascii", errors="ignore")
                    if digit:
                        self.dtmf_digits.append(digit)
                        await self.manager.dtmf_received(self, digit)
                elif packet_type == TYPE_ERROR:
                    log.warning("AudioSocket error packet on %s: %r", self.call_uuid, payload)
                else:
                    log.debug("Ignoring AudioSocket packet type=0x%02x len=%d", packet_type, len(payload))
        except asyncio.IncompleteReadError:
            log.info("AudioSocket peer closed call %s", self.call_uuid or "unknown")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("AudioSocket call failed (%s)", self.call_uuid or self.peer)
        finally:
            self.active = False
            if self._sender_task:
                self._sender_task.cancel()
                try:
                    await self._sender_task
                except asyncio.CancelledError:
                    pass
            if self._manager_started:
                await self.manager.call_ended(self)
                self._manager_started = False
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            log.info("AudioSocket call %s ended", self.call_uuid or "unknown")

    async def send_dtmf(self, digit: str) -> None:
        digit = str(digit or "")[:1]
        if digit not in "0123456789*#ABCDabcd":
            raise ValueError("invalid DTMF digit")
        async with self._write_lock:
            await write_packet(self.writer, TYPE_DTMF, digit.upper().encode("ascii"))

    async def _discord_to_pbx_sender(self) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while self.active:
            frame = self.next_outbound_frame()
            packet_type = RATE_TO_PACKET.get(self.media_tx_rate, TYPE_HANGUP)
            if packet_type == TYPE_HANGUP:
                raise RuntimeError(f"AudioSocket does not support {self.media_tx_rate} Hz PCM")
            try:
                async with self._write_lock:
                    await write_packet(self.writer, packet_type, frame)
                self.tx_audio_bytes += len(frame)
                self.tx_packets += 1
            except (ConnectionError, BrokenPipeError, asyncio.CancelledError):
                raise
            except Exception:
                log.exception("Failed sending audio to PBX on %s", self.call_uuid)
                return
            next_tick += 0.020
            delay = next_tick - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_tick = loop.time()

    async def close(self) -> None:
        if not self.active:
            return
        self.active = False
        try:
            async with self._write_lock:
                await write_packet(self.writer, TYPE_HANGUP)
        except Exception:
            pass
        try:
            self.writer.close()
        except Exception:
            pass


class AudioSocketServer:
    """PBX media service host.

    The historical class name is retained for startup compatibility. It always
    hosts legacy AudioSocket and, when configured, also starts the first-class
    Asterisk chan_websocket media server on its own port. No runtime class or
    method patching is used for the HD path.
    """

    def __init__(self, manager, bind: str, port: int):
        self.manager = manager
        self.bind = bind
        self.port = port
        self.server: Optional[asyncio.AbstractServer] = None
        self.media_config = MediaTransportConfig.from_env()
        self.websocket_server = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle_client, self.bind, self.port)
        sockets = ", ".join(str(sock.getsockname()) for sock in self.server.sockets or [])
        log.info("Legacy AudioSocket listening on %s", sockets)

        if self.media_config.websocket_server_enabled:
            from websocket_media import WebSocketMediaServer

            self.websocket_server = WebSocketMediaServer(self.manager, self.media_config)
            await self.websocket_server.start()
        elif self.media_config.transport == "websocket":
            log.error(
                "PBX_MEDIA_TRANSPORT=websocket but secure WebSocket media is not configured; "
                "set MEDIA_WS_USERNAME, MEDIA_WS_PASSWORD and ASTERISK_MEDIA_CONNECTION"
            )
        else:
            log.info("HD WebSocket media is not configured; legacy AudioSocket remains available")

    async def close(self) -> None:
        if self.websocket_server:
            await self.websocket_server.close()
            self.websocket_server = None
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = AudioSocketSession(reader, writer, self.manager)
        await session.run()
