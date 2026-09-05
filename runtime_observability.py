from __future__ import annotations

import logging
from typing import Any

from media_config import MediaTransportConfig


log = logging.getLogger("discord-pbx.observability")
_PATCHED = False


OBS_STYLE = r"""
<style id="runtimeObservabilityStyle">
.runtimeHealthStrip{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0 0}
.runtimeHealthPill{display:inline-flex;align-items:center;gap:7px;min-height:29px;padding:5px 9px;border:1px solid var(--border);border-radius:999px;background:rgba(4,10,12,.78);font:9px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);letter-spacing:.025em;box-shadow:inset 0 1px rgba(255,255,255,.025)}
.runtimeHealthPill::before{content:"";width:7px;height:7px;border-radius:50%;background:#76858a;box-shadow:0 0 8px rgba(118,133,138,.25)}
.runtimeHealthPill.good{color:#bfead9;border-color:rgba(93,210,161,.32)}
.runtimeHealthPill.good::before{background:#5dd2a1;box-shadow:0 0 8px rgba(93,210,161,.55)}
.runtimeHealthPill.warn{color:#f0d69a;border-color:rgba(240,190,95,.42)}
.runtimeHealthPill.warn::before{background:#f0be5f;box-shadow:0 0 9px rgba(240,190,95,.55)}
.runtimeHealthPill.bad{color:#ffadb3;border-color:rgba(255,107,120,.55)}
.runtimeHealthPill.bad::before{background:#ff6b78;box-shadow:0 0 10px rgba(255,107,120,.65)}
@media(max-width:760px){.runtimeHealthStrip{gap:5px}.runtimeHealthPill{font-size:8px;min-height:27px;padding:4px 8px}}
</style>
"""


OBS_HTML = r"""
<div class="runtimeHealthStrip" id="runtimeHealthStrip" aria-label="Runtime audio and event-loop health">
  <span class="runtimeHealthPill" id="audioPathPill" title="Actual media path is confirmed during a live call">AUDIO · waiting</span>
  <span class="runtimeHealthPill" id="eventLoopLagPill" title="Python event-loop scheduling delay">LAG · waiting</span>
</div>
"""


OBS_SCRIPT = r"""
<script id="runtimeObservabilityScript">
(()=>{'use strict';
const cookie=(name)=>document.cookie.split('; ').find(x=>x.startsWith(name+'='))?.split('=').slice(1).join('=')||'';
const headers=()=>{const h={};const ws=document.getElementById('workspaceSelect');if(ws?.value)h['X-PBX-Workspace']=ws.value;return h};
let previousWarnings=null, hotUntil=0;
const khz=(hz)=>{const n=Number(hz||0);if(!n)return '?';return (n/1000).toFixed(n%1000?1:0)};
function audioText(fmt){
  if(fmt?.confirmed){
    const q=String(fmt.quality||'voice').toUpperCase();
    const rates=Array.isArray(fmt.pbx_tx_rates_hz)?fmt.pbx_tx_rates_hz:[];
    const r=rates.length===1?`${khz(rates[0])} kHz`:rates.length?`${khz(Math.min(...rates))}–${khz(Math.max(...rates))} kHz`:'? kHz';
    const t=String(fmt.transport||'media').toUpperCase();
    return `${q==='HD'?'HD/WIDEBAND':q} · ${t} ${r} · Discord 48 kHz`;
  }
  const c=fmt?.configured||{};
  if(c.wideband_preferred)return `HD READY · ${String(c.format||'slin16').toUpperCase()} ${khz(c.rate_hz)} kHz · awaiting live proof`;
  return 'VOICE READY · AudioSocket fallback · awaiting live proof';
}
function render(status){
  const fmt=status.audio_format||{}, a=document.getElementById('audioPathPill');
  if(a){
    a.textContent=audioText(fmt);
    a.className='runtimeHealthPill '+(fmt.confirmed?(fmt.quality==='hd'?'good':fmt.quality==='mixed'?'warn':''):(fmt.configured?.wideband_preferred?'good':''));
    const calls=Array.isArray(fmt.calls)?fmt.calls:[];
    a.title=fmt.confirmed
      ? `Confirmed live media: ${calls.map(x=>`${x.transport}/${x.format} RX ${x.rx_hz} Hz TX ${x.tx_hz} Hz`).join(' · ')}. Final SIP/carrier codec can still cap end-to-end quality.`
      : `Configured preference only; actual media is confirmed when a call is active. ${fmt.configured?.wideband_preferred?'WebSocket wideband is configured.':'Legacy AudioSocket remains the active-safe fallback.'}`;
  }
  const lag=status.lag_detector||{}, p=document.getElementById('eventLoopLagPill');
  if(p){
    const warnings=Number(lag.stall_count||0), current=Math.max(0,Number(lag.current_seconds||0)), max=Math.max(0,Number(lag.max_seconds||0));
    if(previousWarnings!==null&&warnings>previousWarnings)hotUntil=performance.now()+30000;
    previousWarnings=warnings;
    const currentMs=Math.round(current*1000), maxMs=Math.round(max*1000);
    const active=Boolean(lag.monitor_active);
    const serverState=String(lag.state||'idle');
    const heldBad=performance.now()<hotUntil;
    const state=heldBad?'critical':serverState;
    p.className='runtimeHealthPill '+(state==='critical'?'bad':state==='warning'?'warn':active?'good':'');
    p.textContent=active?`LOOP ${currentMs} ms · max ${maxMs} ms · stalls ${warnings}`:'LAG · monitor arms with Discord voice';
    p.title=`Event-loop detector: current ${current.toFixed(3)}s, max since process start ${max.toFixed(3)}s, >=1s stalls ${warnings}. Warning begins at 250ms; critical begins at 1s. A newly detected stall stays highlighted here for 30 seconds.`;
  }
}
async function tick(){try{const r=await fetch('/api/status',{headers:headers(),cache:'no-store',credentials:'same-origin'});if(!r.ok)return;render(await r.json())}catch(_){}}
tick();setInterval(tick,1000);
})();
</script>
"""


def _session_row(session: object) -> dict[str, Any]:
    rx = int(getattr(session, "media_rx_rate", 0) or 0)
    tx = int(getattr(session, "media_tx_rate", 0) or 0)
    transport = str(getattr(session, "media_transport", "unknown") or "unknown")
    media_format = str(getattr(session, "media_format", "unknown") or "unknown")
    return {
        "transport": transport,
        "format": media_format,
        "rx_hz": rx,
        "tx_hz": tx,
        "wideband": bool(rx >= 16000 and tx >= 16000),
    }


def audio_format_summary(manager: object) -> dict[str, Any]:
    try:
        sessions = [s for s in manager.get_sessions() if bool(getattr(s, "active", False))]
    except Exception:
        sessions = []
    calls = [_session_row(s) for s in sessions]
    cfg = MediaTransportConfig.from_env()
    configured_wideband = bool(
        cfg.transport != "audiosocket"
        and cfg.websocket_configured
        and cfg.websocket_rate >= 16000
    )
    configured = {
        "transport": cfg.transport,
        "websocket_configured": bool(cfg.websocket_configured),
        "wideband_preferred": configured_wideband,
        "format": cfg.websocket_format,
        "rate_hz": int(cfg.websocket_rate),
    }
    if not calls:
        return {
            "confirmed": False,
            "quality": "idle",
            "discord_hz": 48000,
            "transport": "idle",
            "pbx_rx_rates_hz": [],
            "pbx_tx_rates_hz": [],
            "calls": [],
            "configured": configured,
        }

    wide = [bool(row["wideband"]) for row in calls]
    quality = "hd" if all(wide) else ("mixed" if any(wide) else "voice")
    transports = sorted({str(row["transport"]) for row in calls})
    formats = sorted({str(row["format"]) for row in calls})
    return {
        "confirmed": True,
        "quality": quality,
        "discord_hz": 48000,
        "transport": transports[0] if len(transports) == 1 else "mixed",
        "format": formats[0] if len(formats) == 1 else "mixed",
        "pbx_rx_rates_hz": sorted({int(row["rx_hz"]) for row in calls if int(row["rx_hz"]) > 0}),
        "pbx_tx_rates_hz": sorted({int(row["tx_hz"]) for row in calls if int(row["tx_hz"]) > 0}),
        "calls": calls,
        "configured": configured,
    }


def lag_summary(manager: object, status: dict[str, Any]) -> dict[str, Any]:
    reliability = status.get("voice_reliability") if isinstance(status, dict) else {}
    reliability = reliability if isinstance(reliability, dict) else {}
    current = max(0.0, float(reliability.get("event_loop_lag_seconds", 0.0) or 0.0))
    maximum = max(0.0, float(reliability.get("event_loop_lag_max_seconds", 0.0) or 0.0))
    count = max(0, int(reliability.get("event_loop_lag_warnings", 0) or 0))
    task = getattr(manager, "_event_loop_monitor", None)
    active = bool(task is not None and not task.done())
    state = "critical" if current >= 1.0 else ("warning" if current >= 0.25 else ("healthy" if active else "idle"))
    return {
        "monitor_active": active,
        "state": state,
        "current_seconds": round(current, 3),
        "max_seconds": round(maximum, 3),
        "stall_count": count,
        "warning_threshold_seconds": 0.25,
        "critical_threshold_seconds": 1.0,
    }


def inject_observability_ui(page: str) -> str:
    if not page or 'id="runtimeHealthStrip"' in page:
        return page
    if "</head>" in page:
        page = page.replace("</head>", OBS_STYLE + "</head>", 1)
    marker = '<div class="commandbar">'
    if marker in page:
        page = page.replace(marker, OBS_HTML + marker, 1)
    elif "</header>" in page:
        page = page.replace("</header>", "</header>" + OBS_HTML, 1)
    else:
        page = OBS_HTML + page
    if "</body>" in page:
        page = page.replace("</body>", OBS_SCRIPT + "</body>", 1)
    else:
        page += OBS_SCRIPT
    return page


def apply() -> None:
    global _PATCHED
    if _PATCHED:
        return

    import bridge
    import webui

    manager_cls = bridge.BridgeManager
    original_status = manager_cls.status_dict
    original_index = webui.WebControlServer.index

    def status_dict(self) -> dict[str, Any]:
        data = original_status(self)
        data["audio_format"] = audio_format_summary(self)
        data["lag_detector"] = lag_summary(self, data)
        return data

    async def index(self, request):
        response = await original_index(self, request)
        try:
            if getattr(response, "status", 200) == 200 and "text/html" in str(getattr(response, "content_type", "")):
                response.text = inject_observability_ui(response.text)
        except Exception:
            log.exception("Could not inject runtime observability strip")
        return response

    manager_cls.status_dict = status_dict
    webui.WebControlServer.index = index
    manager_cls._runtime_observability_applied = True
    webui.WebControlServer._runtime_observability_applied = True
    _PATCHED = True
