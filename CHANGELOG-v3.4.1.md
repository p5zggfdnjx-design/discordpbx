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
- Adds a visible runtime lag detector to the operator console. It shows current loop delay, maximum delay since process start, and the count of >=1 second stalls. A new stall remains highlighted for 30 seconds so a brief freeze is not missed.

## Discord receive dependency

- Replaces the broad, still-unmerged DAVE receive PR snapshot previously pinned from `imayhaveborkedit/discord-ext-voice-recv` with the smaller reviewed DAVE receive implementation from `rdphillips7/discord-ext-voice-recv`, pinned to an exact commit.
- This removes the affected dependency snapshot that produced the repetitive RTCP Sender Report log noise seen in production.

## Media / HD verification

- The v3.4 native Asterisk WebSocket signed-linear architecture remains the HD media path; Discord processing stays at 48 kHz internally.
- `slin16` (16 kHz signed-linear mono) remains the recommended Asterisk-facing wideband speech profile. `slin24`, `slin32`, `slin44`, and `slin48` remain supported where appropriate.
- The operator console now reports the **actual live media transport and actual RX/TX sample rate**. It displays `HD/WIDEBAND` only when a live call is genuinely >=16 kHz in both directions; an 8 kHz AudioSocket call remains labeled voice/narrowband rather than fake HD.
- While idle, the console may show that WebSocket HD is configured and ready, but labels that state as awaiting live proof until a call negotiates the media path.
- Final end-to-end call quality can still be capped by the SIP endpoint/carrier codec after Asterisk.

## Release validation

- Full Python regression discovery, native voice lifecycle tests, WebSocket media integration tests, HD media tests, shell syntax checks, and injected-JavaScript syntax checks must pass before release.
