# DiscordPBX v3.3.13

First-call inbound pickup reliability release.

## Discord voice startup

- Add a bounded worker-settle window after Discord voice connects so a valid cold connection is not torn down before playback/receive workers finish starting.
- Keep the existing retry and watchdog behavior for genuine connection failures.
- Add optional inbound voice prewarming as soon as FreePBX registers inbound metadata, giving Discord voice negotiation a head start before AudioSocket begins bridging.
- Treat prewarm as opportunistic: a failed prewarm never rejects an otherwise valid inbound call, and the normal AudioSocket-triggered voice setup still retries normally.

## Diagnostics

- Expose worker-settle timeout, prewarm enabled state, active prewarms, successes, and failures under `voice_reliability` status.

## Configuration

New optional tuning values are documented in `.env.example`:

- `PBX_VOICE_WORKER_SETTLE_TIMEOUT=2.5`
- `PBX_VOICE_WORKER_SETTLE_POLL=0.05`
- `PBX_INBOUND_VOICE_PREWARM=true`

## Tests

Regression coverage verifies:

- delayed Discord playback/receive workers are allowed to become ready without an unnecessary reconnect;
- inbound registration starts voice prewarming;
- failed prewarming does not reject or consume the pending inbound call.
