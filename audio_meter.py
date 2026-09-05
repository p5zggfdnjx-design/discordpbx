from __future__ import annotations

import audioop
import math
import time


_PATCHED = False


METER_STYLE = r"""
<style id="liveAudioMeterStyle">
.liveAudioMeters{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
.liveAudioMeter{display:grid;grid-template-columns:auto minmax(90px,1fr) auto;gap:8px;align-items:center;min-height:30px;padding:5px 8px;border:1px solid var(--border);border-radius:10px;background:rgba(4,10,9,.72)}
.liveAudioMeter .label{font-size:10px;font-weight:800;color:var(--muted);white-space:nowrap}
.liveAudioMeter .track{height:8px;border-radius:999px;background:#09110f;border:1px solid #173b32;overflow:hidden;position:relative}
.liveAudioMeter .fill{height:100%;width:0%;border-radius:inherit;background:linear-gradient(90deg,#00aff0 0%,#00ff66 72%,#f5bd59 90%,#ff6b78 100%);transition:width .12s linear}
.liveAudioMeter .value{min-width:50px;text-align:right;font:10px ui-monospace,SFMono-Regular,Menlo,monospace;color:#91caba}
.liveAudioMeter.hot .track{box-shadow:0 0 0 1px rgba(245,189,89,.25)}
.liveAudioMeter.clip .track{box-shadow:0 0 0 1px rgba(255,107,120,.7),0 0 10px rgba(255,107,120,.22)}
@media(max-width:760px){.liveAudioMeters{grid-template-columns:1fr;gap:5px;margin-top:6px}.liveAudioMeter{min-height:27px;padding:4px 7px}.liveAudioMeter .label{font-size:9px}.liveAudioMeter .value{font-size:9px}}
</style>
"""


METER_HTML = r"""
<div class="liveAudioMeters" id="liveAudioMeters" aria-label="Live call audio levels">
  <div class="liveAudioMeter" id="meterPhoneIn" title="Current phone/PBX audio being sent into Discord">
    <span class="label">PHONE → DISCORD</span>
    <span class="track"><span class="fill" id="meterPhoneInFill"></span></span>
    <span class="value" id="meterPhoneInValue">— dB</span>
  </div>
  <div class="liveAudioMeter" id="meterDiscordOut" title="Current Discord audio being sent to the phone/PBX">
    <span class="label">DISCORD → PHONE</span>
    <span class="track"><span class="fill" id="meterDiscordOutFill"></span></span>
    <span class="value" id="meterDiscordOutValue">— dB</span>
  </div>
</div>
"""


METER_SCRIPT = r"""
<script id="liveAudioMeterScript">
(() => {
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, Number(n) || 0));
  const meterPercent = (db) => {
    if (!Number.isFinite(Number(db)) || Number(db) <= -60) return 0;
    return clamp(((Number(db) + 60) / 60) * 100, 0, 100);
  };
  const render = (rootId, fillId, valueId, level) => {
    const root = document.getElementById(rootId);
    const fill = document.getElementById(fillId);
    const value = document.getElementById(valueId);
    if (!root || !fill || !value) return;
    const db = Number(level?.rms_dbfs ?? -90);
    const peak = Number(level?.peak_dbfs ?? -90);
    const active = Boolean(level?.active);
    const pct = active ? meterPercent(db) : 0;
    fill.style.width = `${pct.toFixed(1)}%`;
    value.textContent = active ? `${db.toFixed(1)} dB` : '— dB';
    root.classList.toggle('hot', peak > -6);
    root.classList.toggle('clip', peak > -1.0);
    root.title = active
      ? `RMS ${db.toFixed(1)} dBFS · peak ${peak.toFixed(1)} dBFS`
      : 'No current audio';
  };
  const tick = async () => {
    try {
      const headers = {};
      const ws = document.getElementById('workspaceSelect');
      if (ws?.value) headers['X-PBX-Workspace'] = ws.value;
      const response = await fetch('/api/status', {headers, cache: 'no-store', credentials: 'same-origin'});
      if (!response.ok) return;
      const status = await response.json();
      const levels = status.audio_levels || {};
      render('meterPhoneIn', 'meterPhoneInFill', 'meterPhoneInValue', levels.phone_to_discord);
      render('meterDiscordOut', 'meterDiscordOutFill', 'meterDiscordOutValue', levels.discord_to_phone);
    } catch (_) {
      // Status polling elsewhere in the console remains authoritative. Meter failures
      // must never interfere with call controls.
    }
  };
  tick();
  window.setInterval(tick, 300);
})();
</script>
"""


def _dbfs(sample_value: float) -> float:
    value = max(0.0, float(sample_value or 0.0))
    if value <= 0.0:
        return -90.0
    return max(-90.0, min(0.0, 20.0 * math.log10(value / 32767.0)))


def _measure_pcm(pcm: bytes) -> dict:
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


def _apply_gain_and_limit(pcm: bytes, gain: float, peak_target: int) -> bytes:
    if not pcm:
        return pcm
    if gain != 1.0:
        pcm = audioop.mul(pcm, 2, gain)
    peak = audioop.max(pcm, 2)
    if peak > peak_target > 0:
        pcm = audioop.mul(pcm, 2, peak_target / peak)
    return pcm


def _fresh(level: dict | None, now: float) -> dict:
    level = dict(level or {})
    updated = float(level.get("updated", 0.0) or 0.0)
    if not updated or now - updated > 0.55:
        return {"rms_dbfs": -90.0, "peak_dbfs": -90.0, "active": False}
    return {
        "rms_dbfs": float(level.get("rms_dbfs", -90.0)),
        "peak_dbfs": float(level.get("peak_dbfs", -90.0)),
        "active": bool(level.get("active", False)),
    }


def _combine(levels: list[dict]) -> dict:
    if not levels:
        return {"rms_dbfs": -90.0, "peak_dbfs": -90.0, "active": False}
    return {
        "rms_dbfs": max(float(x.get("rms_dbfs", -90.0)) for x in levels),
        "peak_dbfs": max(float(x.get("peak_dbfs", -90.0)) for x in levels),
        "active": any(bool(x.get("active", False)) for x in levels),
    }


def inject_meter_ui(page: str) -> str:
    if not page or 'id="liveAudioMeters"' in page:
        return page
    page = page.replace("</head>", METER_STYLE + "</head>", 1)
    marker = '<div class="commandbar">'
    if marker in page:
        page = page.replace(marker, METER_HTML + marker, 1)
    else:
        page = page.replace("<main class=\"shell\">", METER_HTML + '<main class="shell">', 1)
    page = page.replace("</body>", METER_SCRIPT + "</body>", 1)
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

    original_session_init = session_cls.__init__
    original_feed_pbx = session_cls._feed_pbx_audio
    original_push_discord = session_cls.push_discord_pcm
    original_status = manager_cls.status_dict
    original_index = webui.WebControlServer.index

    def session_init(self, *args, **kwargs):
        original_session_init(self, *args, **kwargs)
        self._meter_phone_to_discord = None
        self._meter_discord_to_phone = None

    def feed_pbx_audio(self, pcm: bytes, sample_rate: int) -> None:
        # Measure the same gain-limited telephone audio that is intended for Discord.
        # Skip the answering-machine classification prefix because it is deliberately
        # not bridged to Discord.
        if pcm and getattr(self, "voicemail_detection_state", "off") != "checking":
            gain = float(getattr(self.manager.config, "pbx_to_discord_gain", 1.0)) * float(
                getattr(self.manager, "pbx_to_discord_master_gain", 1.0)
            )
            conditioned = _apply_gain_and_limit(pcm, gain, 27000)
            self._meter_phone_to_discord = _measure_pcm(conditioned)
        return original_feed_pbx(self, pcm, sample_rate)

    def push_discord_pcm(self, user_id: int, pcm_48k_stereo: bytes) -> None:
        if pcm_48k_stereo:
            try:
                mono = audioop.tomono(pcm_48k_stereo, 2, 0.5, 0.5)
                gain = float(getattr(self.manager.config, "discord_to_pbx_gain", 1.0)) * float(
                    getattr(self.manager, "discord_to_pbx_master_gain", 1.0)
                )
                conditioned = _apply_gain_and_limit(mono, gain, 25500)
                self._meter_discord_to_phone = _measure_pcm(conditioned)
            except Exception:
                pass
        return original_push_discord(self, user_id, pcm_48k_stereo)

    def status_dict(self) -> dict:
        data = original_status(self)
        now = time.monotonic()
        sessions = [s for s in self.get_sessions() if getattr(s, "active", False)]
        phone_levels = [_fresh(getattr(s, "_meter_phone_to_discord", None), now) for s in sessions]
        discord_levels = [_fresh(getattr(s, "_meter_discord_to_phone", None), now) for s in sessions]
        data["audio_levels"] = {
            "phone_to_discord": _combine(phone_levels),
            "discord_to_phone": _combine(discord_levels),
        }
        return data

    async def index(self, request):
        response = await original_index(self, request)
        try:
            if getattr(response, "status", 200) == 200 and "text/html" in str(getattr(response, "content_type", "")):
                response.text = inject_meter_ui(response.text)
        except Exception:
            # UI decoration is non-critical. The operator console must still load if
            # aiohttp internals change or another skin replaces the page.
            pass
        return response

    session_cls.__init__ = session_init
    session_cls._feed_pbx_audio = feed_pbx_audio
    session_cls.push_discord_pcm = push_discord_pcm
    manager_cls.status_dict = status_dict
    webui.WebControlServer.index = index
    _PATCHED = True
