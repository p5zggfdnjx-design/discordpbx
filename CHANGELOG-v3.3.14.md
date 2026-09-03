# DiscordPBX v3.3.14

Discord voice join-chime release.

## Discord voice join cue

- Play a short local-only Skype-style join chime whenever DiscordPBX establishes a new Discord voice connection.
- Do not replay the cue when the same healthy voice client is reused for another call.
- Replay the cue after a genuine disconnect/reconnect so operators can hear that the bridge has rejoined voice.
- Keep the cue entirely on the Discord side; PBX callers do not hear it.
- Expose join-chime status and play counts in `/api/status`.

## Configuration

- `DISCORD_JOIN_CHIME_ENABLED=true`
- `DISCORD_JOIN_CHIME_GAIN=0.8`
- `DISCORD_JOIN_CHIME_FILE=`

When `DISCORD_JOIN_CHIME_FILE` is blank, DiscordPBX synthesizes an original Skype-style bubbly cue at runtime. To use a user-supplied exact sound, point the setting at a 48 kHz, 16-bit, stereo PCM WAV inside the container.

Regression tests cover PCM framing, one cue per voice-client connection, replay after reconnect, and Discord-local-only mixing.
