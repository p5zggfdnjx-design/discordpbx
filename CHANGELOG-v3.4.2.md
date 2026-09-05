# DiscordPBX v3.4.2

## Fixed

- Native Asterisk `chan_websocket` calls no longer fail when Asterisk sends one or more binary PCM media frames before the `MEDIA_START` control event is observed by DiscordPBX.
- Startup media is held in a tightly bounded 500 ms tail while DiscordPBX waits for authoritative `MEDIA_START` negotiation. Older frames are dropped instead of replaying a multi-second backlog, avoiding startup catch-up latency.
- HD/wideband proof remains strict: a call is not accepted or classified as wideband until `MEDIA_START` has supplied a supported signed-linear format such as `slin16`.
- `MEDIA_START` now has one overall five-second startup deadline rather than an effectively renewable timeout per incoming message.
- Pre-start buffering is bounded by both frame count and bytes and is observable in media connection logs.

## Validation

- Added an integration regression reproducing the production Asterisk 22.6 ordering: three 20 ms `slin16` binary frames arrive before `MEDIA_START`, the session remains alive, negotiates 16 kHz, and the buffered audio is delivered after call acceptance.
- Added an overflow regression verifying the startup queue remains bounded and keeps only recent audio.
- Existing native 16 kHz bidirectional WebSocket media integration coverage remains in place.

## Compatibility

- Existing AudioSocket fallback behavior is unchanged.
- Recommended HD speech profile remains Asterisk `chan_websocket` + `slin16` (16 kHz) with Discord processing at 48 kHz internally.
