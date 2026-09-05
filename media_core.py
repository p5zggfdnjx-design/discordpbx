from __future__ import annotations

import asyncio
import audioop
import math
import queue
import struct
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional


DISCORD_RATE = 48000
DISCORD_FRAME_BYTES = 3840  # 20 ms, signed 16-bit stereo @ 48 kHz
LEGACY_PBX_RATE = 8000


def frame_bytes(rate: int) -> int:
    """20 ms of signed 16-bit mono PCM."""
    return int(int(rate) * 0.020 * 2)


def _limit_pcm(pcm: bytes, peak_target: int = 26000) -> bytes:
    if not pcm:
        return pcm
    peak = audioop.max(pcm, 2)
    if peak > peak_target > 0:
        pcm = audioop.mul(pcm, 2, peak_target / peak)
    return pcm


def _dbfs(value: float) -> float:
    value = max(0.0, float(value or 0.0))
    if value <= 0:
        return -90.0
    return max(-90.0, min(0.0, 20.0 * math.log10(value / 32767.0)))


def measure_pcm(pcm: bytes) -> dict:
    if not pcm:
        return {"rms_dbfs": -90.0, "peak_dbfs": -90.0, "active": False, "updated": time.monotonic()}
    try:
        rms = audioop.rms(pcm, 2)
        peak = audioop.max(pcm, 2)
    except Exception:
        rms = peak = 0
    return {
        "rms_dbfs": round(_dbfs(rms), 1),
        "peak_dbfs": round(_dbfs(peak), 1),
        "active": bool(rms > 8),
        "updated": time.monotonic(),
    }


@dataclass
class _UserAudioState:
    rate_state: object = None
    buffer: bytearray = field(default_factory=bytearray)
    frames: deque[bytes] = field(default_factory=lambda: deque(maxlen=25))
    last_seen: float = field(default_factory=time.monotonic)


class _VoicemailDetector:
    """Conservative AMD-like detector normalized internally to 8 kHz."""

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
        if sample_rate != LEGACY_PBX_RATE:
            pcm, self._rate_state = audioop.ratecv(
                pcm, 2, 1, sample_rate, LEGACY_PBX_RATE, self._rate_state
            )
            sample_rate = LEGACY_PBX_RATE
        if not pcm:
            return None

        dt = len(pcm) / float(sample_rate * 2)
        self.elapsed += dt
        voiced = audioop.rms(pcm, 2) >= self.silence_threshold
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
                    if self.current_word < self.minimum_word:
                        self.words = max(0, self.words - 1)
                    self.in_word = False
                if self.speech_total >= self.minimum_word and self.silence_after_speech >= self.after_greeting_silence:
                    return self._finish("HUMAN", "AFTERGREETING")
        if self.elapsed >= self.total_analysis_limit:
            return self._finish("NOTSURE", "TOOLONG")
        return None


class PcmMediaSession:
    """Transport-independent, 48 kHz internal Discord/PBX PCM engine.

    A transport supplies signed 16-bit mono PCM from Asterisk at `media_rx_rate`
    and consumes signed 16-bit mono PCM at `media_tx_rate`. Everything inside the
    bridge remains 48 kHz, so a 16/48 kHz Asterisk transport is genuinely
    wideband rather than an 8 kHz signal relabeled at a higher sample rate.
    """

    def __init__(
        self,
        manager,
        *,
        media_transport: str,
        media_format: str,
        sample_rate: int,
    ):
        self.manager = manager
        self.call_uuid: Optional[str] = None
        self.active = True
        self.created_at = time.monotonic()
        self.listen_enabled = True
        self.talk_enabled = True
        self.direction = "inbound"
        self.remote_number = ""
        self.caller_id = ""
        self.contact_name = ""
        self.source = ""
        self.randomize_caller_id = False
        self.retry_of = ""
        self.retry_index = 0
        self.workspace_ids: list[str] = []
        self.operator_user_id = ""
        self.operator_name = ""
        self.held = False
        self.park_slot = 0
        self.conference_group = ""

        self.media_transport = str(media_transport)
        self.media_format = str(media_format)
        self.media_rx_rate = int(sample_rate)
        self.media_tx_rate = int(sample_rate)
        self.media_sample_rate = int(sample_rate)
        self.media_wideband = bool(sample_rate >= 16000)

        self._to_discord: queue.Queue[bytes] = queue.Queue(maxsize=100)
        self._ast_rate_state = None
        self._ast_out_buffer = bytearray()
        self._conference_out_buffer = bytearray()
        self._mix_lock = threading.Lock()
        self._users: dict[int, _UserAudioState] = defaultdict(_UserAudioState)
        self._hold_frame_index = 0

        self.rx_audio_bytes = 0
        self.tx_audio_bytes = 0
        self.rx_packets = 0
        self.tx_packets = 0
        self.dtmf_digits: list[str] = []
        self.conference_peer_frames_rx = 0
        self.conference_peer_sources: set[int] = set()

        self.voicemail_detection_enabled = False
        self.voicemail_detection_state = "off"
        self.voicemail_detection_result = ""
        self.voicemail_detection_cause = ""
        self.voicemail_hangup = False
        self._voicemail_detector: Optional[_VoicemailDetector] = None
        self._voicemail_result_reported = False

        self._meter_phone_to_discord = None
        self._meter_discord_to_phone = None

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.created_at)

    @property
    def tx_frame_bytes(self) -> int:
        return frame_bytes(self.media_tx_rate)

    def set_media_rate(self, sample_rate: int, media_format: str | None = None) -> None:
        sample_rate = int(sample_rate)
        if sample_rate <= 0:
            raise ValueError("media sample rate must be positive")
        if sample_rate != self.media_tx_rate:
            with self._mix_lock:
                self._users.clear()
            self.media_tx_rate = sample_rate
        self.media_rx_rate = sample_rate
        self.media_sample_rate = sample_rate
        self.media_wideband = bool(sample_rate >= 16000)
        if media_format:
            self.media_format = str(media_format)

    def enable_voicemail_detection(self) -> None:
        self.voicemail_detection_enabled = True
        self.voicemail_detection_state = "checking"
        self.voicemail_detection_result = ""
        self.voicemail_detection_cause = ""
        self.voicemail_hangup = False
        self._voicemail_detector = _VoicemailDetector()

    def _feed_pbx_audio(self, pcm: bytes, sample_rate: int) -> None:
        if not pcm:
            return
        self.media_rx_rate = int(sample_rate)

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
                return

        if sample_rate != DISCORD_RATE:
            mono_48k, self._ast_rate_state = audioop.ratecv(
                pcm, 2, 1, sample_rate, DISCORD_RATE, self._ast_rate_state
            )
        else:
            mono_48k = pcm

        conference_mono = _limit_pcm(mono_48k, 26500)
        conference_stereo = audioop.tostereo(conference_mono, 2, 1.0, 1.0)

        gain = self.manager.config.pbx_to_discord_gain * float(
            getattr(self.manager, "pbx_to_discord_master_gain", 1.0)
        )
        discord_mono = audioop.mul(mono_48k, 2, gain) if gain != 1.0 else mono_48k
        discord_mono = _limit_pcm(discord_mono, 27000)
        self._meter_phone_to_discord = measure_pcm(discord_mono)
        stereo_48k = audioop.tostereo(discord_mono, 2, 1.0, 1.0)

        self._ast_out_buffer.extend(stereo_48k)
        self._conference_out_buffer.extend(conference_stereo)
        while len(self._ast_out_buffer) >= DISCORD_FRAME_BYTES:
            frame = bytes(self._ast_out_buffer[:DISCORD_FRAME_BYTES])
            del self._ast_out_buffer[:DISCORD_FRAME_BYTES]
            conference_frame = frame
            if len(self._conference_out_buffer) >= DISCORD_FRAME_BYTES:
                conference_frame = bytes(self._conference_out_buffer[:DISCORD_FRAME_BYTES])
                del self._conference_out_buffer[:DISCORD_FRAME_BYTES]
            if self.call_uuid:
                self.manager.push_conference_pcm(self.call_uuid, conference_frame)
            try:
                self._to_discord.put_nowait(frame)
            except queue.Full:
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
                # Audio fanout failure must not destroy the telephony leg.
                pass

    def read_discord_frame(self) -> bytes:
        try:
            return self._to_discord.get_nowait()
        except queue.Empty:
            return b"\x00" * DISCORD_FRAME_BYTES

    def _push_stereo_pcm(self, user_id: int, pcm_48k_stereo: bytes, gain: float, *, meter: bool = False) -> int:
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
            if meter:
                self._meter_discord_to_phone = measure_pcm(mono)
            pbx_pcm, state.rate_state = audioop.ratecv(
                mono, 2, 1, DISCORD_RATE, self.media_tx_rate, state.rate_state
            )
            pbx_pcm = _limit_pcm(pbx_pcm, 25500)
            state.buffer.extend(pbx_pcm)
            target = self.tx_frame_bytes
            while len(state.buffer) >= target:
                frame = bytes(state.buffer[:target])
                del state.buffer[:target]
                state.frames.append(frame)
                queued += 1
        return queued

    def push_discord_pcm(self, user_id: int, pcm_48k_stereo: bytes) -> None:
        gain = self.manager.config.discord_to_pbx_gain * float(
            getattr(self.manager, "discord_to_pbx_master_gain", 1.0)
        )
        self._push_stereo_pcm(user_id, pcm_48k_stereo, gain, meter=True)

    def push_peer_pcm(self, user_id: int, pcm_48k_stereo: bytes) -> int:
        queued = self._push_stereo_pcm(user_id, pcm_48k_stereo, 1.0, meter=False)
        if queued:
            self.conference_peer_frames_rx += queued
            self.conference_peer_sources.add(int(user_id))
        return queued

    def _mix_next_pbx_frame(self) -> bytes:
        now = time.monotonic()
        target = self.tx_frame_bytes
        with self._mix_lock:
            stale = [uid for uid, st in self._users.items() if now - st.last_seen > 3.0 and not st.frames]
            for uid in stale:
                self._users.pop(uid, None)
            frames = [st.frames.popleft() for st in self._users.values() if st.frames]
        if not frames:
            return b"\x00" * target
        if len(frames) == 1:
            return frames[0]
        scale = min(1.0, 1.15 / math.sqrt(len(frames)))
        mixed = b"\x00" * target
        for frame in frames:
            mixed = audioop.add(mixed, audioop.mul(frame, 2, scale), 2)
            mixed = _limit_pcm(mixed, 27500)
        return mixed

    def _next_hold_frame(self) -> bytes:
        target = self.tx_frame_bytes
        samples = target // 2
        frame_no = self._hold_frame_index % 200
        self._hold_frame_index += 1
        if frame_no >= 10:
            return b"\x00" * target
        out = bytearray(target)
        for i in range(samples):
            t = ((frame_no * samples) + i) / float(self.media_tx_rate)
            value = int(32767 * 0.06 * math.sin(2 * math.pi * 440 * t))
            struct.pack_into("<h", out, i * 2, max(-32768, min(32767, value)))
        return bytes(out)

    def next_outbound_frame(self) -> bytes:
        if getattr(self, "held", False):
            return self._next_hold_frame()
        if self.voicemail_detection_state == "checking":
            return b"\x00" * self.tx_frame_bytes
        return self._mix_next_pbx_frame()
