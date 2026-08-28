# DiscordPBX v3.3.12

Inbound-call reliability release.

## Inbound voice reliability

- Detect and discard stale Discord `VoiceRecvClient` objects instead of attempting to reuse a disconnected client.
- Retry Discord voice setup up to three times with bounded backoff before rejecting an AudioSocket call.
- Verify that Discord playback and receive workers are actually running before treating voice setup as healthy.
- Add a per-workspace watchdog while PBX calls are active. If Discord voice, playback, or receive dies mid-call, the bridge attempts to repair it automatically.
- Restart the watchdog when Discord playback/receive callbacks report an unexpected stop.
- Preserve intentional operator/idle disconnects so the watchdog does not fight a requested disconnect.
- Expose voice-recovery attempts, active watchdogs, recovery counts, and last errors in `/api/status` under `voice_reliability`.

## Inbound call lifecycle

- Add a TTL to inbound metadata registrations so a registration whose AudioSocket connection never arrives cannot remain pending forever.
- Clean up inbound pending metadata when a call is rejected before the normal session-registration path completes.
- Record a `bridge failed` event with the concrete route/voice failure reason and persist the failure in call history.
- Continue accepting multi-workspace calls when at least one Discord route is healthy; failed routes remain eligible for watchdog recovery.

## Routing

- Fix manual/ring-group routing with deleted or disabled targets. When the saved target list resolves to nothing, the configured fallback is now honored instead of silently producing an empty route.
- Explicit `off`, `dnd`, and `reject` modes continue to reject inbound routing and never fall back automatically.

## FreePBX ingress

- Bound the optional inbound metadata HTTP callback with short cURL connect/overall timeouts in the example dialplan. A stopped or restarting DiscordPBX web service can no longer hold the caller indefinitely before `AudioSocket()` starts.
- If metadata registration fails, AudioSocket can still connect and the bridge falls back to its normal default inbound routing behavior.

## Configuration

Optional tuning variables were added to `.env.example`:

- `PBX_VOICE_CONNECT_ATTEMPTS=3`
- `PBX_VOICE_CONNECT_TIMEOUT=10`
- `PBX_VOICE_READY_TIMEOUT=15`
- `PBX_VOICE_WATCHDOG_INTERVAL=2`
- `PBX_INBOUND_PENDING_TTL=30`

Regression tests cover stale Discord voice clients, transient voice-connect retries, empty-route failure cleanup, stale inbound registrations, manual-route fallback, and DND behavior.
