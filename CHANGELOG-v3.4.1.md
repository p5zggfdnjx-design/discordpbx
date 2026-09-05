# DiscordPBX v3.4.1

## Discord voice reliability

- Replaces the competing runtime voice-repair monkeypatch stack with a canonical `ReliableBridgeManager` and explicit `bridge_core`/voice-lifecycle split.
- Makes DiscordPBX the single owner of Discord voice reconnects. Bridge voice connections use `reconnect=False`, preventing discord.py's internal reconnect loop from racing DiscordPBX's watchdog.
- Requires a sustained unhealthy interval before one bounded clean repair instead of tearing down a connection on a single watchdog poll.
- Moves inbound prewarm, registration expiry, idle departure, worker-settle handling, and voice diagnostics into normal source architecture.
- Fixes the idle-leave race: inactive/ended media sessions no longer count as voice work, and the leave timer starts before call-history/event/Discord notification awaits can delay it.
- Duplicate idle scheduling no longer restarts the leave clock.
- A watchdog re-checks active/pending work immediately before a destructive repair, so a call that just ended cannot cause the bot to rejoin.
- Adds event-loop lag telemetry to `/api/status` under `voice_reliability`.

## Discord receive dependency

- Replaces the broad, still-unmerged DAVE receive PR snapshot previously pinned from `imayhaveborkedit/discord-ext-voice-recv` with the smaller reviewed DAVE receive implementation from `rdphillips7/discord-ext-voice-recv`, pinned to an exact commit.
- This also removes the known noisy treatment of normal RTCP Sender Reports seen in the affected deployment.

## Media

- The v3.4 native Asterisk WebSocket/slin16 HD architecture is unchanged.
- This release intentionally stabilizes the Discord side before switching a production FreePBX route from legacy AudioSocket to HD WebSocket media.
