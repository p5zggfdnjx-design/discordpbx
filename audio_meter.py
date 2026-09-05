from __future__ import annotations

import audioop
import math
import time


_PATCHED = False


METER_STYLE = r"""
<style id="liveAudioMeterStyle">
.liveAudioMeters{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:8px}
.liveAudioMeter{display:grid;grid-template-columns:minmax(118px,auto) minmax(140px,1fr) 62px;grid-template-areas:"label track value" "gain gain gain";gap:6px 9px;align-items:center;min-height:58px;padding:7px 9px;border:1px solid var(--border);border-radius:12px;background:linear-gradient(180deg,rgba(7,14,20,.88),rgba(4,10,9,.74));box-shadow:inset 0 1px rgba(255,255,255,.025)}
.liveAudioMeter .label{grid-area:label;font-size:10px;font-weight:850;color:var(--muted);white-space:nowrap;letter-spacing:.025em}
.liveAudioMeter .track{grid-area:track;height:12px;border-radius:999px;background:linear-gradient(90deg,#07110e,#0b1615);border:1px solid #1e3b35;overflow:hidden;position:relative;box-shadow:inset 0 2px 5px rgba(0,0,0,.28)}
.liveAudioMeter .track::after{content:"";position:absolute;inset:0;background:repeating-linear-gradient(90deg,transparent 0,transparent calc(16.666% - 1px),rgba(255,255,255,.11) calc(16.666% - 1px),rgba(255,255,255,.11) 16.666%);pointer-events:none}
.liveAudioMeter .fill{height:100%;width:0%;border-radius:inherit;background:linear-gradient(90deg,#00aff0 0%,#00e680 58%,#84e15c 72%,#f5bd59 87%,#ff6b78 100%);transition:width .09s linear;box-shadow:0 0 10px rgba(0,255,102,.15)}
.liveAudioMeter .peak{position:absolute;top:1px;bottom:1px;width:2px;left:0%;background:#fff;border-radius:2px;opacity:0;box-shadow:0 0 5px rgba(255,255,255,.6);transition:left .09s linear,opacity .15s}
.liveAudioMeter .value{grid-area:value;min-width:62px;text-align:right;font:10px ui-monospace,SFMono-Regular,Menlo,monospace;color:#a8d9cc}
.liveAudioMeter.hot .track{box-shadow:inset 0 2px 5px rgba(0,0,0,.28),0 0 0 1px rgba(245,189,89,.3)}
.liveAudioMeter.clip .track{box-shadow:inset 0 2px 5px rgba(0,0,0,.28),0 0 0 1px rgba(255,107,120,.85),0 0 12px rgba(255,107,120,.25)}
.liveAudioGain{grid-area:gain;display:grid;grid-template-columns:auto minmax(110px,1fr) auto;gap:8px;align-items:center;min-width:0}
.liveAudioGain .gainLabel{font-size:9px;font-weight:800;color:var(--muted);letter-spacing:.05em}
.liveAudioGain input[type=range]{min-height:18px;height:18px;margin:0;accent-color:var(--accent)}
.liveAudioGain .gainValue{min-width:92px;text-align:right;font:9px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted)}
.liveAudioGain.saving .gainValue{color:var(--amber)}
.liveAudioGain.saved .gainValue{color:var(--green)}
.liveAudioGain.error .gainValue{color:var(--red)}
#settings #callerGain,#settings #discordGain,#settings #chimeGain{height:22px;accent-color:var(--accent)}
#settings #callerGainVal,#settings #discordGainVal,#settings #chimeGainVal{font:10px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--text)}
@media(max-width:760px){.liveAudioMeters{grid-template-columns:1fr;gap:6px;margin-top:6px}.liveAudioMeter{min-height:54px;padding:6px 8px;grid-template-columns:minmax(108px,auto) minmax(100px,1fr) 58px}.liveAudioMeter .label{font-size:9px}.liveAudioMeter .value{font-size:9px}.liveAudioGain .gainValue{min-width:86px;font-size:8px}}
</style>
"""


METER_HTML = r"""
<div class="liveAudioMeters" id="liveAudioMeters" aria-label="Live call audio levels">
  <div class="liveAudioMeter" id="meterPhoneIn" title="Current phone/PBX audio being sent into Discord">
    <span class="label">PHONE → DISCORD</span>
    <span class="track"><span class="fill" id="meterPhoneInFill"></span><span class="peak" id="meterPhoneInPeak"></span></span>
    <span class="value" id="meterPhoneInValue">— dB</span>
    <label class="liveAudioGain" id="gainPhoneInWrap"><span class="gainLabel">GAIN</span><input id="gainPhoneIn" type="range" min="0" max="2" step="0.05" value="1"><span class="gainValue" id="gainPhoneInValue">1.00× · 0.0 dB</span></label>
  </div>
  <div class="liveAudioMeter" id="meterDiscordOut" title="Current Discord audio being sent to the phone/PBX">
    <span class="label">DISCORD → PHONE</span>
    <span class="track"><span class="fill" id="meterDiscordOutFill"></span><span class="peak" id="meterDiscordOutPeak"></span></span>
    <span class="value" id="meterDiscordOutValue">— dB</span>
    <label class="liveAudioGain" id="gainDiscordOutWrap"><span class="gainLabel">GAIN</span><input id="gainDiscordOut" type="range" min="0" max="2" step="0.05" value="1.35"><span class="gainValue" id="gainDiscordOutValue">1.35× · +2.6 dB</span></label>
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
  const dbForGain = (gain) => {
    const n = Number(gain);
    if (!Number.isFinite(n) || n <= 0) return '-∞ dB';
    const db = 20 * Math.log10(n);
    return `${db >= 0 ? '+' : ''}${db.toFixed(1)} dB`;
  };
  const gainText = (gain) => `${Number(gain || 0).toFixed(2)}× · ${dbForGain(gain)}`;
  const cookie = (name) => document.cookie.split('; ').find(x => x.startsWith(name + '='))?.split('=').slice(1).join('=') || '';
  const headers = (json=false) => {
    const out = {};
    const ws = document.getElementById('workspaceSelect');
    if (ws?.value) out['X-PBX-Workspace'] = ws.value;
    const csrf = cookie('pbx_csrf');
    if (csrf) out['X-CSRF-Token'] = decodeURIComponent(csrf);
    if (json) out['Content-Type'] = 'application/json';
    return out;
  };

  const peakHold = new Map();
  const render = (rootId, fillId, peakId, valueId, level) => {
    const root = document.getElementById(rootId);
    const fill = document.getElementById(fillId);
    const peakLine = document.getElementById(peakId);
    const value = document.getElementById(valueId);
    if (!root || !fill || !peakLine || !value) return;
    const db = Number(level?.rms_dbfs ?? -90);
    const peak = Number(level?.peak_dbfs ?? -90);
    const active = Boolean(level?.active);
    const pct = active ? meterPercent(db) : 0;
    fill.style.width = `${pct.toFixed(1)}%`;
    value.textContent = active ? `${db.toFixed(1)} dB` : '— dB';
    const now = performance.now();
    const prior = peakHold.get(rootId) || {db:-90, until:0};
    let held = peak;
    let until = now + 1200;
    if (now < prior.until && prior.db > peak) { held = prior.db; until = prior.until; }
    peakHold.set(rootId, {db:held, until});
    if (active || now < prior.until) {
      peakLine.style.left = `${meterPercent(held).toFixed(1)}%`;
      peakLine.style.opacity = '0.9';
    } else {
      peakLine.style.opacity = '0';
    }
    root.classList.toggle('hot', peak > -6);
    root.classList.toggle('clip', peak > -1.0);
    root.title = active
      ? `RMS ${db.toFixed(1)} dBFS · peak ${peak.toFixed(1)} dBFS · peak hold ${held.toFixed(1)} dBFS`
      : 'No current audio';
  };

  const gains = {
    caller_to_discord_gain: {top:'gainPhoneIn', value:'gainPhoneInValue', wrap:'gainPhoneInWrap', legacy:'callerGain', legacyValue:'callerGainVal', confirmed:1, saving:false, editing:false},
    discord_to_caller_gain: {top:'gainDiscordOut', value:'gainDiscordOutValue', wrap:'gainDiscordOutWrap', legacy:'discordGain', legacyValue:'discordGainVal', confirmed:1.35, saving:false, editing:false},
    inbound_chime_gain: {legacy:'chimeGain', legacyValue:'chimeGainVal', confirmed:1, saving:false, editing:false},
  };

  const setGainVisual = (key, value, {force=false}={}) => {
    const state = gains[key];
    if (!state) return;
    const n = clamp(value, 0, 2);
    const top = state.top ? document.getElementById(state.top) : null;
    const legacy = document.getElementById(state.legacy);
    if (top && (force || (!state.editing && !state.saving && document.activeElement !== top))) top.value = String(n);
    if (legacy && (force || (!state.editing && !state.saving && document.activeElement !== legacy))) legacy.value = String(n);
    const text = gainText(n);
    if (state.value) { const el = document.getElementById(state.value); if (el) el.textContent = text; }
    const legacyText = document.getElementById(state.legacyValue);
    if (legacyText) legacyText.textContent = text;
  };

  const mark = (key, cls, textValue) => {
    const state = gains[key];
    const wrap = state?.wrap ? document.getElementById(state.wrap) : null;
    if (wrap) {
      wrap.classList.remove('saving','saved','error');
      if (cls) wrap.classList.add(cls);
    }
    if (textValue !== undefined) setGainVisual(key, textValue, {force:true});
  };

  const saveGain = async (key, value) => {
    const state = gains[key];
    if (!state || state.saving) return;
    const requested = clamp(value, 0, 2);
    state.saving = true;
    mark(key, 'saving');
    try {
      const response = await fetch('/api/operator/audio', {
        method:'POST', headers:headers(true), credentials:'same-origin', cache:'no-store',
        body:JSON.stringify({[key]: requested}),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      const responseKey = key === 'caller_to_discord_gain' ? 'caller_to_discord' : key === 'discord_to_caller_gain' ? 'discord_to_caller' : 'inbound_chime';
      state.confirmed = clamp(body[responseKey] ?? requested, 0, 2);
      setGainVisual(key, state.confirmed, {force:true});
      mark(key, 'saved');
      window.setTimeout(() => mark(key, ''), 700);
    } catch (err) {
      setGainVisual(key, state.confirmed, {force:true});
      mark(key, 'error');
      const wrap = state.wrap ? document.getElementById(state.wrap) : null;
      if (wrap) wrap.title = `Gain save failed: ${err?.message || err}`;
      window.setTimeout(() => mark(key, ''), 1800);
    } finally {
      state.saving = false;
      state.editing = false;
    }
  };

  const bindSlider = (key, id) => {
    const state = gains[key];
    const el = document.getElementById(id);
    if (!state || !el || el.dataset.audioMeterBound === '1') return;
    el.dataset.audioMeterBound = '1';
    el.onpointerdown = () => { state.editing = true; };
    el.onfocus = () => { state.editing = true; };
    el.oninput = () => { state.editing = true; setGainVisual(key, +el.value, {force:true}); };
    el.onchange = () => saveGain(key, +el.value);
    el.onblur = () => { if (!state.saving) state.editing = false; };
  };

  // Replace the old percentage-only Settings handlers with the same persistent,
  // race-safe gain control used by the new top mixer strip.
  bindSlider('caller_to_discord_gain', 'callerGain');
  bindSlider('discord_to_caller_gain', 'discordGain');
  bindSlider('inbound_chime_gain', 'chimeGain');
  bindSlider('caller_to_discord_gain', 'gainPhoneIn');
  bindSlider('discord_to_caller_gain', 'gainDiscordOut');

  const tick = async () => {
    try {
      const response = await fetch('/api/status', {headers:headers(false), cache:'no-store', credentials:'same-origin'});
      if (!response.ok) return;
      const status = await response.json();
      const levels = status.audio_levels || {};
      render('meterPhoneIn', 'meterPhoneInFill', 'meterPhoneInPeak', 'meterPhoneInValue', levels.phone_to_discord);
      render('meterDiscordOut', 'meterDiscordOutFill', 'meterDiscordOutPeak', 'meterDiscordOutValue', levels.discord_to_phone);
      for (const [key,state] of Object.entries(gains)) {
        const server = Number(status[key]);
        if (!Number.isFinite(server)) continue;
        state.confirmed = clamp(server, 0, 2);
        if (!state.editing && !state.saving) setGainVisual(key, state.confirmed, {force:true});
      }
    } catch (_) {
      // Meter/control telemetry must never interfere with call controls.
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
        # These keys are intentionally top-level because the existing Settings UI
        # already hydrates its sliders from /api/status. v3.3.19 omitted them from
        # the modern status path, causing every refresh to fall back to 1.0 and
        # visually reset a gain that the server had actually persisted correctly.
        data.update({
            "caller_to_discord_gain": float(getattr(self, "pbx_to_discord_master_gain", 1.0)),
            "discord_to_caller_gain": float(getattr(self, "discord_to_pbx_master_gain", 1.0)),
            "inbound_chime_gain": float(getattr(self, "inbound_chime_master_gain", 1.0)),
        })
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
