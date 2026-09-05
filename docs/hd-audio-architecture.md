# DiscordPBX true HD audio — v3.4 architecture

## What changed

v3.4 does **not** call an upsampled 8 kHz AudioSocket signal "HD". The HD path is a separate, first-class Asterisk media transport built on `chan_websocket`.

```text
Discord voice
  Opus on the network
        ↓
DiscordPBX voice receive
  48 kHz / 16-bit / stereo PCM
        ↓
media_core.PcmMediaSession
  canonical 48 kHz internal processing
        ↓
Asterisk chan_websocket
  slin16 by default (16 kHz / 16-bit / mono PCM)
        ↓
Asterisk channel / SIP endpoint / carrier
```

The final SIP/PSTN leg can still limit end-to-end quality. An internal G.722/Opus-capable endpoint can remain wideband; a PSTN route negotiated as PCMU/PCMA is narrowband beyond Asterisk even though the DiscordPBX↔Asterisk leg is wideband.

## No HD monkeypatch

The old `wideband_audio.py` startup patch was removed. HD media now lives in normal source modules:

- `media_core.py` — transport-independent PCM engine
- `websocket_media.py` — authenticated Asterisk `chan_websocket` transport
- `media_config.py` — explicit transport/profile configuration
- `audiosocket.py` — legacy AudioSocket transport using the same PCM engine
- `pbx.py` — AMI originate logic that selects the configured media transport

The standard AudioSocket listener remains as a rollback/old-Asterisk compatibility transport.

## Asterisk requirements

`chan_websocket` is available in Asterisk 20.16+, 21.11+, 22.6+, and 23+. The default DiscordPBX control format is plain text for compatibility across that range. JSON control can be selected on versions that support it.

Before changing production routing, verify on FreePBX/Asterisk:

```text
asterisk -rx 'core show version'
asterisk -rx 'module show like chan_websocket'
asterisk -rx 'module show like res_websocket_client'
```

## DiscordPBX configuration

Generate a long random media password. Configure the bridge `.env`:

```dotenv
PBX_MEDIA_TRANSPORT=websocket
MEDIA_WS_BIND=0.0.0.0
MEDIA_WS_PORT=9093
MEDIA_WS_USERNAME=discordpbx
MEDIA_WS_PASSWORD=REPLACE_WITH_LONG_RANDOM_MEDIA_PASSWORD
MEDIA_WS_FORMAT=slin16
MEDIA_WS_CONTROL_FORMAT=plain-text
ASTERISK_MEDIA_CONNECTION=discordpbx_media
```

`auto` is the upgrade-safe default: if secure WebSocket credentials are configured, outbound calls use WebSocket media; otherwise the existing AudioSocket path remains active.

## Asterisk websocket_client.conf

Merge the stanza in `freepbx/websocket_client.conf.example` into Asterisk's `websocket_client.conf`, replacing `BUILDER_IP` and the password.

The connection must be `per_call_config`; each call receives its own media WebSocket.

## Inbound FreePBX route

Use the v3.4 `freepbx/extensions_custom.conf.example`. The HD destination uses:

```text
Dial(WebSocket/discordpbx_media/c(slin16)v(call_uuid=${BRIDGE_UUID}))
```

That `call_uuid` query parameter is mandatory. DiscordPBX validates it before accepting the media connection and uses it to correlate the WebSocket channel with the existing inbound/outbound call metadata and workspace routing.

The file also retains a separate `discord-bridge-legacy` AudioSocket destination for rollback.

## Outbound calls

The operator API remains unchanged. `AsteriskAMI.originate_media()` chooses the configured transport. In WebSocket mode, the answered Local channel runs `Dial(WebSocket/...)` rather than the `AudioSocket()` application.

A failed WebSocket configuration does not silently downgrade an explicitly selected `PBX_MEDIA_TRANSPORT=websocket` call. Explicit WebSocket mode fails clearly. `auto` is the only mode that intentionally chooses the legacy path when secure HD configuration is absent.

## Security

Port 9093 is PBX media, not a public web service. Keep it LAN/VPN/container-network only.

The WebSocket endpoint requires HTTP Basic authentication. Asterisk's `websocket_client.conf` username/password must match `MEDIA_WS_USERNAME` and `MEDIA_WS_PASSWORD`. Authentication comparisons are constant-time, and the server refuses connections without a valid UUID correlation parameter.

Do not route the media port through Nginx Proxy Manager or Cloudflare unless there is a specific network reason to do so. NPM is for the operator web UI; PBX media should normally stay directly reachable only between Asterisk and DiscordPBX.

## Flow control and framing

DiscordPBX sends 20 ms signed-linear frames. `chan_websocket` can re-time/re-frame linear PCM, but DiscordPBX still maintains a real-time 20 ms cadence to minimize queue latency.

`MEDIA_XOFF` pauses media transmission and `MEDIA_XON` resumes it. This prevents DiscordPBX from continuing to fill Asterisk's media queue when the channel driver signals backpressure.

## Supported HD profiles

The transport configuration accepts Asterisk signed-linear PCM profiles. The recommended production default is `slin16`.

- `slin` — 8 kHz; functional but narrowband
- `slin16` — 16 kHz; recommended HD/wideband speech
- `slin24`, `slin32`, `slin44`, `slin48` — higher-rate linear PCM profiles

For ordinary phone speech, `slin16` is usually the right tradeoff. `slin48` is supported by the internal PCM engine but does not make a PSTN carrier full-band.

## Acceptance test

1. Update DiscordPBX and configure the media password/profile.
2. Configure `websocket_client.conf` and reload Asterisk.
3. Verify `chan_websocket` and `res_websocket_client` are loaded.
4. Place an inbound call through `discord-bridge`.
5. Place an outbound call from DiscordPBX.
6. Confirm Discord audio is bidirectional and gain controls still work.
7. Confirm the server log reports a WebSocket media call at `slin16` / `16000Hz`.
8. Verify DTMF through the existing AMI `PlayDTMF` path.
9. Verify hold, conferencing, voicemail detection, hangup, and multiple workspaces.
10. Change the route to `discord-bridge-legacy` or set `PBX_MEDIA_TRANSPORT=audiosocket` to prove rollback remains functional.

## Why not direct Opus yet?

Asterisk treats codecs such as Opus in WebSocket passthrough mode, which moves framing/timing responsibility back to the application. DiscordPBX currently decodes Discord participants to PCM for mixing, conferencing, gains, metering, voicemail detection, and soundboard injection. Signed-linear 16/48 kHz therefore gives a real HD path without sacrificing those features or adding an unnecessary encode/decode stage inside the bridge.
