# DiscordPBX v3.3.17

## Inbound pickup reliability

- Inbound registration now makes a single routing decision instead of immediately performing a second full routing/presence pass just to build status metadata.
- The FreePBX metadata callback gives the existing Discord voice prewarm a bounded 1.25-second head start before returning, staying inside the bundled dialplan's 2-second CURL timeout.
- A prewarm timeout never rejects the phone call; the existing AudioSocket voice retry/self-heal path still runs normally.
- The bundled FreePBX dialplan example now registers metadata/prewarms Discord before `Answer()`, reducing answered dead-air when a Discord voice connection is cold.

## Quieter call teardown

- Hangup sounds are now burst-limited: the first three hangups in a rolling 60-second window play the bundled hangup cue; additional hangups during the burst are silent.
- The actual PBX calls still hang up normally. Only the Discord-local sound is suppressed.
- Defaults can be tuned with `PBX_HANGUP_SOUND_BURST_LIMIT` and `PBX_HANGUP_SOUND_WINDOW`.

## Diagnostics

`/api/status` now exposes `inbound_stability` counters for successful prewarm handshakes, handshake timeouts, and suppressed hangup cues.
