# DiscordPBX v3.4.0 — True HD media

## Native wideband Asterisk transport

- Adds a first-class Asterisk `chan_websocket` media server on port 9093.
- Default HD profile is `slin16`: real 16 kHz signed-linear PCM between Asterisk and DiscordPBX.
- Supports higher signed-linear profiles up through `slin48` while preserving a canonical 48 kHz Discord-side PCM engine.
- Authenticates Asterisk WebSocket media with Basic auth and mandatory per-call UUID correlation.
- Handles `MEDIA_START`, DTMF events, `MEDIA_XOFF`, and `MEDIA_XON` control flow.
- Uses one per-call WebSocket connection, matching Asterisk's `per_call_config` media model.

## Proper audio architecture

- Introduces `media_core.py` as the transport-independent bidirectional PCM engine.
- Legacy AudioSocket now uses that same core rather than having a separate patched wideband implementation.
- Removes `wideband_audio.py` and its startup monkeypatch entirely.
- The standard Asterisk `AudioSocket()` application remains an explicit narrowband rollback path; DiscordPBX no longer treats forced/upsampled AudioSocket output as proof of HD.

## Outbound and inbound calls

- AMI outbound calls automatically use `Dial(WebSocket/.../c(slin16))` when secure WebSocket media is configured.
- `PBX_MEDIA_TRANSPORT=websocket` requires the HD transport rather than silently downgrading.
- `PBX_MEDIA_TRANSPORT=auto` preserves existing deployments by using AudioSocket only when HD credentials are absent.
- The FreePBX inbound example now uses `chan_websocket` and includes a separate legacy rollback destination.
- Adds a `websocket_client.conf` example for FreePBX/Asterisk.

## Compatibility

Asterisk `chan_websocket` requires Asterisk 20.16+, 21.11+, 22.6+, or 23+. Plain-text control mode is the compatibility default; JSON control may be selected on newer supported releases.

The final SIP/PSTN leg can still reduce an otherwise wideband bridge to narrowband. v3.4 removes DiscordPBX's own 8 kHz transport bottleneck; it does not claim control over a carrier's negotiated codec.
