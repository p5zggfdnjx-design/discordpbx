from __future__ import annotations

import asyncio
import audioop
import logging
import math
import queue
import struct
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("discord-pbx.audiosocket")

# Asterisk AudioSocket packet types.
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

PBX_RATE = 8000
PBX_FRAME_BYTES = 320        # 20 ms, 16-bit mono @ 8 kHz
DISCORD_RATE = 48000
DISCORD_FRAME_BYTES = 3840   # 20 ms, 16-bit stereo @ 48 kHz


def _limit_pcm(pcm: bytes, peak_target: int = 26000) -> bytes:
    """Apply conservative peak limiting to 16-bit PCM to avoid hard clipping/static."""
    if not pcm:
        return pcm
    peak = audioop.max(pcm, 2)
    if peak > peak_target > 0:
        pcm = audioop.mul(pcm, 2, peak_target / peak)
    return pcm


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(3)
    packet_type = header[0]
    length = struct.unpack(">H", header[1:])[0]
    payload = await reader.readexactly(length) if length else b""
    return packet_type, payload


async def write_packet(writer: asyncio.StreamWriter, packet_type: int, payload: bytes = b"") -> None:
    writer.write(bytes((packet_type,)) + struct.pack(">H", len(payload)) + payload)
    await writer.drain()


@dataclass
class _UserAudioState:
    rate_state: object = None
    buffer: bytearray = field(default_factory=bytearray)
    frames: deque[bytes] = field(default_factory=lambda: deque(maxlen=25))
    last_seen: float = field(default_factory=time.monotonic)


class _VoicemailDetector:
    """Conservative AMD-like detector for the first seconds after answer.

    It intentionally mirrors the high-level Asterisk AMD rules but runs on the
    AudioSocket PCM stream, so the feature works without modifying FreePBX
    dialplan files. MACHINE is only returned for strong machine-like patterns;
    NOTSURE is treated as a human call by the bridge.
    """

    def __init__(
        self,
        *,
        initial_silence: float = 2.5,
        greeting: float = 1.8,
        after_greeting_silence: float = 0.80,
        total_analysis: float = 5.0,
        minimum_word: float = 0.10,
        between_words_silence: float = 0.08,
        maximum_words: int = 4,
        silence_threshold: int = 420,
        maximum_word_length: float = 3.0,
    ):
        self.initial_silence_limit = float(initial_silence)
        self.greeting_limit = float(greeting)
        self.after_greeting_silence = float(after_greeting_silence)
        self.total_analysis_limit = float(total_analysis)
        self.minimum_word = float(minimum_word)
        self.between_words_silence = float(between_words_silence)
        self.maximum_words = int(maximum_words)
        self.silence_threshold = int(silence_threshold)
        self.maximum_word_length = float(maximum_word_length)

        self.elapsed = 0.0
        self.initial_silence = 0.0
        self.speech_total = 0.0
        self.current_word = 0.0
        self.silence_after_speech = 0.0
        self.words = 0
        self.have_speech = False
        self.in_word = False
        self._rate_state = None
        self.result = ""
        self.cause = ""

    def _finish(self, result: str, cause: str) -> tuple[str, str]:
        self.result = result
        self.cause = cause
        return result, cause

    def feed(self, pcm: bytes, sample_rate: int) -> tuple[str, str] | None:
        if self.result:
            return self.result, self.cause
        if not pcm or sample_rate <= 0:
            return None

        # Normalize solely for detector consistency. The normal audio path keeps
        # its own higher-quality resampler state.
        if sample_rate != PBX_RATE:
            pcm, self._rate_state = audioop.ratecv(pcm, 2, 1, sample_rate, PBX_RATE, self._rate_state)
            sample_rate = PBX_RATE
        if not pcm:
            return None

        dt = len(pcm) / float(sample_rate * 2)
        self.elapsed += dt
        rms = audioop.rms(pcm, 2)
        voiced = rms >= self.silence_threshold

        if voiced:
            if not self.have_speech:
                self.have_speech = True
                self.words = 1
                self.in_word = True
                self.current_word = 0.0
            elif not self.in_word and self.silence_after_speech >= self.between_words_silence:
                self.words += 1
                self.in_word = True
                self.current_word = 0.0

            self.current_word += dt
            self.speech_total += dt
            self.silence_after_speech = 0.0

            if self.current_word >= self.maximum_word_length:
                return self._finish("MACHINE", "MAXWORDLENGTH")
            if self.speech_total >= self.greeting_limit:
                return self._finish("MACHINE", "LONGGREETING")
            if self.words > self.maximum_words:
                return self._finish("MACHINE", "MAXWORDS")
        else:
            if not self.have_speech:
                self.initial_silence += dt
                if self.initial_silence >= self.initial_silence_limit:
                    return self._finish("MACHINE", "INITIALSILENCE")
            else:
                self.silence_after_speech += dt
                if self.in_word and self.silence_after_speech >= self.between_words_silence:
                    # Very tiny clicks/noise are not counted as words.
                    if self.current_word < self.minimum_word:
                        self.words = max(0, self.words - 1)
                    self.in_word = False
                if self.speech_total >= self.minimum_word and self.silence_after_speech >= self.after_greeting_silence:
                    return self._finish("HUMAN", "AFTERGREETING")

        if self.elapsed >= self.total_analysis_limit:
            return self._finish("NOTSURE", "TOOLONG")
        return None


class AudioSocketSession:
    """One bidirectional Asterisk AudioSocket call."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, manager):
        self.reader = reader
        self.writer = writer
        self.manager = manager
        self.call_uuid: Optional[str] = None
        self.peer = writer.get_extra_info("peername")
        self.active = True
        self.created_at = time.monotonic()

        # Per-call routing controls used by the multi-call bridge.
        self.listen_enabled = True   # PBX/caller -> Discord
        self.talk_enabled = True     # Discord -> PBX/caller
        self.direction = "inbound"
        self.remote_number = ""
        self.caller_id = ""

        # PBX -> Discord
        self._to_discord: queue.Queue[bytes] = queue.Queue(maxsize=100)
        self._ast_rate_state = None
        self._ast_out_buffer = bytearray()

        # Discord -> PBX
        self._mix_lock = threading.Lock()
        self._users: dict[int, _UserAudioState] = defaultdict(_UserAudioState)

        self.rx_audio_bytes = 0
        self.tx_audio_bytes = 0
        self.rx_packets = 0
        self.tx_packets = 0
        self.dtmf_digits: list[str] = []
        # Caller-to-caller conference diagnostics. These counters make the
        # conference path observable from the web status page without logging
        # every 20 ms audio frame.
        self.conference_peer_frames_rx = 0
        self.conference_peer_sources: set[int] = set()
        self._sender_task: Optional[asyncio.Task] = None
        self._write_lock = asyncio.Lock()
        self.conference_group = ""
        self.source = ""
        self.randomize_caller_id = False
        self.retry_of = ""
        self.retry_index = 0
        self.workspace_ids: list[str] = []
        self.operator_user_id = ""
        self.operator_name = ""
        self.held = False
        self.park_slot = 0
        self._hold_frame_index = 0

        # Optional outbound answering-machine detection. It is enabled by the
        # operator setting copied onto this session by BridgeManager.call_started.
        self.voicemail_detection_enabled = False
        self.voicemail_detection_state = "off"
        self.voicemail_detection_result = ""
        self.voicemail_detection_cause = ""
        self.voicemail_hangup = False
        self._voicemail_detector: Optional[_VoicemailDetector] = None
        self._voicemail_result_reported = False

    def enable_voicemail_detection(self) -> None:
        self.voicemail_detection_enabled = True
        self.voicemail_detection_state = "checking"
        self.voicemail_detection_result = ""
        self.voicemail_detection_cause = ""
        self.voicemail_hangup = False
        self._voicemail_detector = _VoicemailDetector()

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.created_at)

    async def run(self) -> None:
        try:
            packet_type, payload = await asyncio.wait_for(read_packet(self.reader), timeout=5.0)
            if packet_type != TYPE_UUID or len(payload) != 16:
                raise RuntimeError("first AudioSocket packet must be a 16-byte UUID packet")

            self.call_uuid = str(uuid.UUID(bytes=payload))
            log.info("AudioSocket call %s connected from %s", self.call_uuid, self.peer)

            accepted = await self.manager.call_started(self)
            if not accepted:
                log.warning("Rejecting AudioSocket call %s because the simultaneous-call limit was reached", self.call_uuid)
                await write_packet(self.writer, TYPE_HANGUP)
                return

            self._sender_task = asyncio.create_task(self._discord_to_pbx_sender(), name=f"pbx-send-{self.call_uuid}")

            while self.active:
                packet_type, payload = await read_packet(self.reader)
                self.rx_packets += 1

                if packet_type == TYPE_HANGUP:
                    break
                if packet_type in AUDIO_RATES:
                    self.rx_audio_bytes += len(payload)
                    self._feed_pbx_audio(payload, AUDIO_RATES[packet_type])
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
            await self.manager.call_ended(self)
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            log.info("AudioSocket call %s ended", self.call_uuid or "unknown")

    def _feed_pbx_audio(self, pcm: bytes, sample_rate: int) -> None:
        """Convert Asterisk mono PCM into Discord's 48 kHz stereo 20 ms frames."""
        if not pcm:
            return

        # During voicemail detection neither side is bridged. This prevents
        # Discord chatter from leaking into a machine greeting and avoids playing
        # the greeting into Discord while classification is in progress.
        if self.voicemail_detection_state == "checking" and self._voicemail_detector is not None:
            decision = self._voicemail_detector.feed(pcm, sample_rate)
            if decision is None:
                return
            result, cause = decision
            self.voicemail_detection_result = result
            self.voicemail_detection_cause = cause
            self.voicemail_detection_state = "machine" if result == "MACHINE" else ("human" if result == "HUMAN" else "not sure")
            if not self._voicemail_result_reported:
                self._voicemail_result_reported = True
                asyncio.create_task(
                    self.manager.voicemail_classified(self, result, cause),
                    name=f"voicemail-{(self.call_uuid or 'unknown')[:8]}",
                )
            if result == "MACHINE":
                # The manager closes the session immediately. Drop this frame.
                return
            # HUMAN/NOTSURE: resume live audio from this point onward. The small
            # classification prefix is intentionally discarded rather than delayed.
        if sample_rate != DISCORD_RATE:
            mono_48k, self._ast_rate_state = audioop.ratecv(
                pcm, 2, 1, sample_rate, DISCORD_RATE, self._ast_rate_state
            )
        else:
            mono_48k = pcm

        # Keep conference audio independent from the Caller -> Discord volume.
        # v3.2.5/3.2.6 accidentally cross-fed the *post Discord-gain* frame, so a
        # low caller-monitor volume could make callers nearly inaudible to each
        # other even though conference mode was enabled.
        conference_mono_48k = _limit_pcm(mono_48k, 26500)
        conference_stereo_48k = audioop.tostereo(conference_mono_48k, 2, 1.0, 1.0)

        gain = self.manager.config.pbx_to_discord_gain * float(getattr(self.manager, "pbx_to_discord_master_gain", 1.0))
        discord_mono_48k = mono_48k
        if gain != 1.0:
            discord_mono_48k = audioop.mul(discord_mono_48k, 2, gain)
        discord_mono_48k = _limit_pcm(discord_mono_48k, 27000)

        stereo_48k = audioop.tostereo(discord_mono_48k, 2, 1.0, 1.0)
        self._ast_out_buffer.extend(stereo_48k)

        # AudioSocket commonly arrives in 20 ms chunks, but do not assume that.
        # Maintain a separate frame buffer for the clean conference feed so the
        # peer path stays aligned with the Discord path.
        conf_buf = getattr(self, "_conference_out_buffer", None)
        if conf_buf is None:
            conf_buf = bytearray()
            self._conference_out_buffer = conf_buf
        conf_buf.extend(conference_stereo_48k)

        while len(self._ast_out_buffer) >= DISCORD_FRAME_BYTES:
            frame = bytes(self._ast_out_buffer[:DISCORD_FRAME_BYTES])
            del self._ast_out_buffer[:DISCORD_FRAME_BYTES]
            conference_frame = frame
            if len(conf_buf) >= DISCORD_FRAME_BYTES:
                conference_frame = bytes(conf_buf[:DISCORD_FRAME_BYTES])
                del conf_buf[:DISCORD_FRAME_BYTES]
            # Caller-to-caller audio is injected directly into peer AudioSocket
            # output mixers and never loops through Discord voice.
            if self.call_uuid:
                self.manager.push_conference_pcm(self.call_uuid, conference_frame)
            try:
                self._to_discord.put_nowait(frame)
            except queue.Full:
                # Preserve low latency: discard the oldest audio instead of building delay.
                try:
                    self._to_discord.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._to_discord.put_nowait(frame)
                except queue.Full:
                    pass
            try:
                self.manager.publish_pbx_frame(self, frame)
            except Exception:
                log.exception("Workspace audio fanout failed for %s", self.call_uuid)

    def read_discord_frame(self) -> bytes:
        try:
            return self._to_discord.get_nowait()
        except queue.Empty:
            return b"\x00" * DISCORD_FRAME_BYTES

    def _push_stereo_pcm(self, user_id: int, pcm_48k_stereo: bytes, gain: float) -> int:
        if not self.active or not pcm_48k_stereo:
            return 0
        queued = 0
        with self._mix_lock:
            state = self._users[user_id]
            state.last_seen = time.monotonic()
            mono = audioop.tomono(pcm_48k_stereo, 2, 0.5, 0.5)
            if gain != 1.0:
                mono = audioop.mul(mono, 2, gain)
            mono = _limit_pcm(mono, 25500)
            pbx_pcm, state.rate_state = audioop.ratecv(
                mono, 2, 1, DISCORD_RATE, PBX_RATE, state.rate_state
            )
            pbx_pcm = _limit_pcm(pbx_pcm, 25500)
            state.buffer.extend(pbx_pcm)
            while len(state.buffer) >= PBX_FRAME_BYTES:
                frame = bytes(state.buffer[:PBX_FRAME_BYTES])
                del state.buffer[:PBX_FRAME_BYTES]
                state.frames.append(frame)
                queued += 1
        return queued

    def push_discord_pcm(self, user_id: int, pcm_48k_stereo: bytes) -> None:
        """Called by the Discord receive thread. Convert and queue for the PBX mixer."""
        gain = self.manager.config.discord_to_pbx_gain * float(getattr(self.manager, "discord_to_pbx_master_gain", 1.0))
        self._push_stereo_pcm(user_id, pcm_48k_stereo, gain)

    def push_peer_pcm(self, user_id: int, pcm_48k_stereo: bytes) -> int:
        """Route clean caller audio into another caller's AudioSocket mixer.

        Conference gain is intentionally independent from the Discord -> Caller
        master gain. Conference callers are phone participants, not Discord
        speakers, and should remain audible when the operator changes Discord
        monitoring volume.
        """
        queued = self._push_stereo_pcm(user_id, pcm_48k_stereo, 1.0)
        if queued:
            self.conference_peer_frames_rx += queued
            self.conference_peer_sources.add(int(user_id))
        return queued

    async def send_dtmf(self, digit: str) -> None:
        digit = str(digit or "")[:1]
        if digit not in "0123456789*#ABCDabcd":
            raise ValueError("invalid DTMF digit")
        async with self._write_lock:
            await write_packet(self.writer, TYPE_DTMF, digit.upper().encode("ascii"))

    def _mix_next_pbx_frame(self) -> bytes:
        now = time.monotonic()
        with self._mix_lock:
            stale = [uid for uid, st in self._users.items() if now - st.last_seen > 3.0 and not st.frames]
            for uid in stale:
                self._users.pop(uid, None)

            frames = [st.frames.popleft() for st in self._users.values() if st.frames]

        if not frames:
            return b"\x00" * PBX_FRAME_BYTES

        if len(frames) == 1:
            return frames[0]

        # Preserve intelligibility with Discord + caller conference sources. A
        # straight 1/N average made a two-party conference 6 dB quieter. Use
        # square-root normalization and a limiter instead.
        scale = min(1.0, 1.15 / math.sqrt(len(frames)))
        mixed = b"\x00" * PBX_FRAME_BYTES
        for frame in frames:
            mixed = audioop.add(mixed, audioop.mul(frame, 2, scale), 2)
            mixed = _limit_pcm(mixed, 27500)
        return mixed

    def _next_hold_frame(self) -> bytes:
        """Low-level hold cue: a short gentle tone every 4 seconds with silence between.

        This avoids needing a licensed music-on-hold asset while still making it clear
        that the line is intentionally held. Deployments can later replace this with
        an uploaded MOH asset without changing the call state machine.
        """
        samples = PBX_FRAME_BYTES // 2
        frame_no = self._hold_frame_index % 200  # 4 seconds at 20 ms/frame
        self._hold_frame_index += 1
        if frame_no >= 10:  # 200 ms tone, 3.8 s silence
            return b"\x00" * PBX_FRAME_BYTES
        out = bytearray(PBX_FRAME_BYTES)
        for i in range(samples):
            t = ((frame_no * samples) + i) / PBX_RATE
            value = int(32767 * 0.06 * math.sin(2 * math.pi * 440 * t))
            struct.pack_into("<h", out, i * 2, max(-32768, min(32767, value)))
        return bytes(out)

    async def _discord_to_pbx_sender(self) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time()

        while self.active:
            # Keep the called party isolated from Discord while AMD-like
            # classification is running. Silence maintains AudioSocket timing.
            if getattr(self, "held", False):
                frame = self._next_hold_frame()
            elif self.voicemail_detection_state == "checking":
                frame = b"\x00" * PBX_FRAME_BYTES
            else:
                frame = self._mix_next_pbx_frame()
            try:
                async with self._write_lock:
                    await write_packet(self.writer, 0x10, frame)
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
                # If delayed badly, resynchronize instead of trying to catch up in a burst.
                next_tick = loop.time()

    async def close(self) -> None:
        self.active = False
        try:
            await write_packet(self.writer, TYPE_HANGUP)
        except Exception:
            pass
        try:
            self.writer.close()
        except Exception:
            pass


class AudioSocketServer:
    def __init__(self, manager, bind: str, port: int):
        self.manager = manager
        self.bind = bind
        self.port = port
        self.server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle_client, self.bind, self.port)
        sockets = ", ".join(str(sock.getsockname()) for sock in self.server.sockets or [])
        log.info("AudioSocket listening on %s", sockets)

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = AudioSocketSession(reader, writer, self.manager)
        await session.run()
