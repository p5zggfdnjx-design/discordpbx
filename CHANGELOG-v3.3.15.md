# DiscordPBX v3.3.15

## Bundled call sound pack

- Bundles the project owner's seven call-event sounds directly in DiscordPBX releases.
- Replaces the synthesized join cue with the supplied `start-call` sound.
- Uses the supplied `phone-ring` sound for incoming call notification in Discord.
- Uses the supplied `call-ring` sound as outbound Discord-local ringback.
- Plays dedicated cues for hold, declined/cancelled calls, normal hangup, and call setup failure.
- Keeps every cue on the Discord-local mixer so PBX/PSTN callers do not hear operator UI sounds.
- Delays idle voice disconnect long enough for terminal cues such as hangup/declined to finish playing.
- Adds sound-pack status diagnostics and regression tests.

The bundled runtime assets are compact 48 kHz Opus transcodes of the supplied MP3 files. Original upload SHA-256 values are recorded in `assets/sounds/README.md` for provenance.
