# DiscordPBX v3.3.18

## Audio intelligibility

- Raised the default Discord -> phone/caller master gain from `1.0x` to `1.35x` so Discord participants are easier to hear on PBX/PSTN calls.
- Existing installations migrate only when they are still on the historical untouched `1.0x` value; explicitly customized audio gains are preserved.
- The existing PCM peak limiter remains the clipping safety guard.

## Live audio meters

- Added always-visible live `PHONE -> DISCORD` and `DISCORD -> PHONE` meters at the top of the operator console.
- Meters show current RMS level in dBFS and react to the actual gain-limited PCM path used for each direction.
- Meter telemetry decays to inactive within roughly half a second when audio stops, rather than showing stale audio.
- Meter polling and rendering are isolated from call controls so a telemetry/UI failure cannot interrupt telephony.
- Meter layout remains visible and compact on mobile.

## Validation

- Added regression coverage for the audio-level migration, explicit custom gain preservation, PCM level measurement, limiter behavior, stale-meter decay, and idempotent UI injection.
