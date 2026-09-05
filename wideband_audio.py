from __future__ import annotations

import asyncio
import audioop
import math
import os
import time


_PATCHED = False
SUPPORTED_RATES = (8000, 12000, 16000, 24000, 32000, 44100, 48000)
RATE_TO_PACKET = {
    8000: 0x10,
    12000: 0x11,
    16000: 0x12,
    24000: 0x13,
    32000: 0x14,
    44100: 0x15,
    48000: 0x16,
}

QUALITY_STYLE = r"""
<style id="audioQualityStyle">
.audioQualityBadge{grid-column:1/-1;display:flex;align-items:center;justify-content:center;gap:7px;min-height:25px;padding:3px 8px;border:1px solid var(--border);border-radius:9px;background:rgba(4,10,9,.55);font:9px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);letter-spacing:.02em}
.audioQualityBadge .qdot{width:7px;height:7px;border-radius:50%;background:var(--muted);box-shadow:0 0 7px rgba(145,160,183,.2)}
.audioQualityBadge.wide .qdot{background:#00ff66;box-shadow:0 0 8px rgba(0,255,102,.35)}
.audioQualityBadge .qmode{font-weight:800;color:var(--text)}
@media(max-width:760px){.audioQualityBadge{min-height:23px;font-size:8px}}
</style>
"""

QUALITY_HTML = r"""
<div class="audioQualityBadge" id="audioQualityBadge" title="Discord remains 48 kHz internally. The PBX side uses the highest rate the AudioSocket path actually provides, or a configured override.">
  <span class="qdot"></span><span class="qmode" id="audioQualityMode">AUDIO PATH</span><span id="audioQualityText">Discord 48 kHz · PBX —</span>
</div>
"""

QUALITY_SCRIPT = r"""
<script id="audioQualityScript">
(() => {
  const fmt = (hz) => {
    const n = Number(hz || 0);
    if (!n) return '—';
    if (n === 44100) return '44.1 kHz';
    return `${Math.round(n / 1000)} kHz`;
  };
  const tick = async () => {
    try {
      const headers = {};
      const ws = document.getElementById('workspaceSelect');
      if (ws?.value) headers['X-PBX-Workspace'] = ws.value;
      const r = await fetch('/api/status', {headers, cache:'no-store', credentials:'same-origin'});
      if (!r.ok) return;
      const s = await r.json();
      const f = s.audio_format || {};
      const root = document.getElementById('audioQualityBadge');
      const mode = document.getElementById('audioQualityMode');
      const text = document.getElementById('audioQualityText');
      if (!root || !mode || !text) return;
      const wide = Boolean(f.wideband_active);
      root.classList.toggle('wide', wide);
      mode.textContent = wide ? 'WIDEBAND' : 'VOICE';
      const rx = Number(f.pbx_rx_hz || 0);
      const tx = Number(f.pbx_tx_hz || 0);
      const pbx = rx && tx && rx !== tx ? `${fmt(rx)} in / ${fmt(tx)} out` : fmt(tx || rx);
      text.textContent = `Discord 48 kHz · PBX ${pbx}`;
      root.title = wide
        ? 'Wideband AudioSocket audio is active. Final call quality can still be limited by the SIP/carrier codec.'
        : 'Discord stays 48 kHz internally; this call is currently narrowband on the PBX side. PSTN/carrier codecs may impose this limit.';
    } catch (_) {}
  };
  tick();
  window.setInterval(tick, 1000);
})();
</script>
"""


def _configured_rate() -> int | None:
    raw = os.getenv("PBX_AUDIO_RATE", "auto").strip().lower()
    if raw in {"", "auto", "adaptive"}:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value in SUPPORTED_RATES else None


def choose_auto_rate(incoming_rate: int | None) -> int:
    """Mirror a supported AudioSocket rate; stay at 8 kHz for the classic app path."""
    try:
        rate = int(incoming_rate or 0)
    except (TypeError, ValueError):
        rate = 0
    return rate if rate in SUPPORTED_RATES else 8000


def frame_bytes(rate: int) -> int:
    return int(rate * 0.020 * 2)  # 20 ms, signed 16-bit mono


def inject_quality_ui(page: str) -> str:
    if not page or 'id="audioQualityBadge"' in page:
        return page
    page = page.replace("</head>", QUALITY_STYLE + "</head>", 1)
    marker = '<div class="liveAudioMeters" id="liveAudioMeters" aria-label="Live call audio levels">'
    if marker in page:
        page = page.replace(marker, marker + QUALITY_HTML, 1)
    else:
        page = page.replace('<div class="commandbar">', QUALITY_HTML + '<div class="commandbar">', 1)
    page = page.replace("</body>", QUALITY_SCRIPT + "</body>", 1)
    return page


def apply() -> None:
    global _PATCHED
    if _PATCHED:
        return

    import audiosocket
    import bridge
    import webui

    session_cls = audiosocket.AudioSocketSession
    manager_cls = bridge.BridgeManager

    original_init = session_cls.__init__
    original_feed_pbx = session_cls._feed_pbx_audio
    original_status = manager_cls.status_dict
    original_index = webui.WebControlServer.index

    forced_rate = _configured_rate()

    def session_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._pbx_rx_rate = 0
        self._pbx_tx_rate = int(forced_rate or 8000)
        self._pbx_tx_forced = bool(forced_rate)

    def _set_tx_rate(self, rate: int) -> None:
        rate = choose_auto_rate(rate)
        if getattr(self, "_pbx_tx_forced", False):
            return
        if rate == getattr(self, "_pbx_tx_rate", 8000):
            return
        with self._mix_lock:
            self._pbx_tx_rate = rate
            # Resampler state and queued frames are rate-specific. A negotiated
            # rate transition is rare and should be clean rather than mixing frame sizes.
            self._users.clear()

    def feed_pbx_audio(self, pcm: bytes, sample_rate: int) -> None:
        if sample_rate in SUPPORTED_RATES:
            self._pbx_rx_rate = int(sample_rate)
            _set_tx_rate(self, int(sample_rate))
        return original_feed_pbx(self, pcm, sample_rate)

    def push_stereo_pcm(self, user_id: int, pcm_48k_stereo: bytes, gain: float) -> int:
        if not self.active or not pcm_48k_stereo:
            return 0
        queued = 0
        rate = int(getattr(self, "_pbx_tx_rate", 8000) or 8000)
        target_bytes = frame_bytes(rate)
        with self._mix_lock:
            state = self._users[user_id]
            state.last_seen = time.monotonic()
            mono = audioop.tomono(pcm_48k_stereo, 2, 0.5, 0.5)
            if gain != 1.0:
                mono = audioop.mul(mono, 2, gain)
            mono = audiosocket._limit_pcm(mono, 25500)
            pbx_pcm, state.rate_state = audioop.ratecv(
                mono, 2, 1, audiosocket.DISCORD_RATE, rate, state.rate_state
            )
            pbx_pcm = audiosocket._limit_pcm(pbx_pcm, 25500)
            state.buffer.extend(pbx_pcm)
            while len(state.buffer) >= target_bytes:
                frame = bytes(state.buffer[:target_bytes])
                del state.buffer[:target_bytes]
                state.frames.append(frame)
                queued += 1
        return queued

    def mix_next_pbx_frame(self) -> bytes:
        now = time.monotonic()
        rate = int(getattr(self, "_pbx_tx_rate", 8000) or 8000)
        target_bytes = frame_bytes(rate)
        with self._mix_lock:
            stale = [uid for uid, st in self._users.items() if now - st.last_seen > 3.0 and not st.frames]
            for uid in stale:
                self._users.pop(uid, None)
            frames = [st.frames.popleft() for st in self._users.values() if st.frames]
        if not frames:
            return b"\x00" * target_bytes
        if len(frames) == 1:
            return frames[0]
        scale = min(1.0, 1.15 / math.sqrt(len(frames)))
        mixed = b"\x00" * target_bytes
        for frame in frames:
            mixed = audioop.add(mixed, audioop.mul(frame, 2, scale), 2)
            mixed = audiosocket._limit_pcm(mixed, 27500)
        return mixed

    def next_hold_frame(self) -> bytes:
        rate = int(getattr(self, "_pbx_tx_rate", 8000) or 8000)
        target_bytes = frame_bytes(rate)
        samples = target_bytes // 2
        frame_no = self._hold_frame_index % 200
        self._hold_frame_index += 1
        if frame_no >= 10:
            return b"\x00" * target_bytes
        out = bytearray(target_bytes)
        import struct
        for i in range(samples):
            t = ((frame_no * samples) + i) / float(rate)
            value = int(32767 * 0.06 * math.sin(2 * math.pi * 440 * t))
            struct.pack_into("<h", out, i * 2, max(-32768, min(32767, value)))
        return bytes(out)

    async def discord_to_pbx_sender(self) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while self.active:
            if getattr(self, "held", False):
                frame = self._next_hold_frame()
            elif self.voicemail_detection_state == "checking":
                frame = b"\x00" * frame_bytes(int(getattr(self, "_pbx_tx_rate", 8000) or 8000))
            else:
                frame = self._mix_next_pbx_frame()
            rate = int(getattr(self, "_pbx_tx_rate", 8000) or 8000)
            packet_type = RATE_TO_PACKET.get(rate, 0x10)
            try:
                async with self._write_lock:
                    await audiosocket.write_packet(self.writer, packet_type, frame)
                self.tx_audio_bytes += len(frame)
                self.tx_packets += 1
            except (ConnectionError, BrokenPipeError, asyncio.CancelledError):
                raise
            except Exception:
                audiosocket.log.exception("Could not send audio to Asterisk call %s", self.call_uuid)
                return
            next_tick += 0.020
            await asyncio.sleep(max(0.0, next_tick - loop.time()))

    def status_dict(self) -> dict:
        data = original_status(self)
        sessions = [s for s in self.get_sessions() if getattr(s, "active", False)]
        rx_rates = [int(getattr(s, "_pbx_rx_rate", 0) or 0) for s in sessions]
        tx_rates = [int(getattr(s, "_pbx_tx_rate", 8000) or 8000) for s in sessions]
        rx = max(rx_rates, default=0)
        tx = max(tx_rates, default=(forced_rate or 8000))
        data["audio_format"] = {
            "discord_hz": 48000,
            "pbx_rx_hz": rx,
            "pbx_tx_hz": tx,
            "wideband_active": bool(rx >= 16000 or tx >= 16000),
            "mode": "forced" if forced_rate else "adaptive",
        }
        return data

    async def index(self, request):
        response = await original_index(self, request)
        try:
            if getattr(response, "status", 200) == 200 and "text/html" in str(getattr(response, "content_type", "")):
                response.text = inject_quality_ui(response.text)
        except Exception:
            pass
        return response

    session_cls.__init__ = session_init
    session_cls._feed_pbx_audio = feed_pbx_audio
    session_cls._push_stereo_pcm = push_stereo_pcm
    session_cls._mix_next_pbx_frame = mix_next_pbx_frame
    session_cls._next_hold_frame = next_hold_frame
    session_cls._discord_to_pbx_sender = discord_to_pbx_sender
    manager_cls.status_dict = status_dict
    webui.WebControlServer.index = index
    _PATCHED = True
