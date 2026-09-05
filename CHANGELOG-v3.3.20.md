# DiscordPBX v3.3.20

## Audio console repair

- Rebuilt the always-visible bidirectional meters into compact mixer strips with smoother bars, scale marks, peak-hold indicators, RMS readouts, and inline gain controls.
- Added live gain readouts in both multiplier and dB form (for example `1.35× · +2.6 dB`).
- The top mixer and Settings sliders now use the same persistent `/api/operator/audio` path and include the existing CSRF/workspace headers.
- Gain edits are protected while the operator is dragging, focused, or waiting for a save. A failed save rolls back to the last server-confirmed value instead of silently drifting.
- Fixed the Settings gain reset bug: the modern `/api/status` path now exposes the persisted/current `caller_to_discord_gain`, `discord_to_caller_gain`, and `inbound_chime_gain` values that the existing Settings renderer already expects.
- Meter/control polling remains isolated from call controls; telemetry failure cannot interrupt a call.

## HD audio roadmap

v3.3.20 deliberately does **not** pretend that forcing a higher AudioSocket packet rate makes an ordinary Asterisk `AudioSocket()` dialplan leg truly wideband. The existing v3.3.19 adaptive-rate groundwork remains in place while the next media-engine phase introduces a transport abstraction and a genuine Asterisk wideband media path (targeting `chan_websocket`/`slin16` first, with the existing AudioSocket path retained as compatibility fallback).
