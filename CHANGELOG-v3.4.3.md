# DiscordPBX v3.4.3

## Discord voice cut-out resilience

- Harden the pinned `discord-ext-voice-recv` packet router so one malformed, DAVE, Opus, or sink-processing exception cannot terminate the whole receive worker.
- Move DiscordPBX PCM sink processing outside the third-party `PacketRouter` lock. Packet extraction/decode remains protected for the dependency's non-independently-locked jitter buffer, but 48 kHz bridge mixing/resampling no longer blocks RTP ingestion behind that router lock.
- Add a coalesced fast repair path when Discord explicitly reports that receive or playback stopped. Active calls no longer need to wait for the slower polling watchdog before the canonical lifecycle attempts recovery.
- Keep the existing polling watchdog as fallback for silent failures, with safer faster defaults: 1.0 second polling and 1.5 second sustained-unhealthy grace.
- Add an external event-loop stack watchdog. When the asyncio heartbeat is stale for at least one second it rate-limits and logs the live Python event-loop stack as `ASYNC LOOP BLOCKED ...`, making the next real scheduling stall actionable instead of recording duration only.
- Expose fast-repair, stack-watchdog, and voice-receive-router counters through the existing `voice_reliability` status payload.

## Audio path

The real Asterisk WebSocket `slin16` 16 kHz path introduced before this release is unchanged. This release is about continuity and latency; it does not downgrade the HD/wideband media path or relabel narrowband audio as HD.

## Validation

Regression coverage verifies that decoder/sink exceptions are isolated, DiscordPBX sink processing occurs after the router lock is released, duplicate worker-stop callbacks coalesce to one repair, idle/suppressed workspaces are not rejoined, and the stack formatter can capture the Python owner thread.
