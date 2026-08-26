# Discord ↔ FreePBX Bridge v3.3.0

v3.3.0 makes this repository the default update source, turns the manual GitHub workflow into a one-click release publisher, and hardens Discord OAuth URL/callback validation. It keeps the v3 multi-workspace/RBAC architecture and the v2 telephony bridge/persistent formats.

> **v3.3.0 canonical updates:** new installs default to `p5zggfdnjx-design/discordpbx`, so **Settings → Updates → Update latest from GitHub** works without entering a repository or token while this repository remains public. The GitHub **Publish DiscordPBX release** workflow now publishes or refreshes the release asset when run manually, not only when a tag was pushed.

> **v3.3.0 Discord sign-in repair:** OAuth authorization and token exchange now use one validated callback builder, forwarded proxy headers are normalized, unsafe return URLs are rejected, Discord cancellation/errors are reported clearly, and the OAuth readiness API lists the exact missing configuration. Public URLs must be an HTTPS origin with no path, query, credentials, or fragment.


> **v3.2.9 GitHub pull updater:** system administrators can configure an `owner/repository` under **Settings → Updates**, optionally store a token for a private repository, check the latest GitHub Release, and press **Update latest from GitHub**. The bridge downloads the release ZIP through GitHub's Releases API, validates it as a DiscordPBX package, verifies its SHA-256 internally, stages it into the existing managed updater, and then uses the same backup/health-check/automatic-rollback path as manual ZIP updates. Manual ZIP upload remains available as a fallback.

> **v3.2.9 local user accounts:** **Settings → Local accounts** can create username/password operators for the shared `/login` page without requiring Discord. Each account can be enabled/disabled, assigned to one or more PBX workspaces with View, Operator, or Workspace Admin capability presets, or explicitly promoted to system administrator. Password changes/disable actions invalidate active sessions; passwords use the same PBKDF2-SHA256 storage as the recovery administrator.

> **v3.2.9 release automation:** `.github/workflows/release.yml` packages tagged versions into the exact ZIP format the managed updater expects and publishes them as GitHub Release assets, so a repository can become the authoritative source for future one-click updates.


> **v3.2.9 sign-in fix:** the local break-glass sign-in no longer relies on browser-created global variables for the username/password fields. The controls are explicitly bound, the request carries same-origin credentials, the button shows a real pending state, and errors remain visible instead of silently doing nothing. The Discord button now has an explicit route/return target and visible navigation state.

> **v3.2.9 updater visibility:** system administrators now get an **↑ Updates** shortcut beside the account controls (and an **Open Updates** button in the mobile More sheet). It jumps directly to the managed updater card, which is now the first administrator card under Settings rather than buried near the bottom. **Update now** can install either a newly selected ZIP or an already-staged release.

> **v3.2.9 updater permissions:** the one-time managed-updater installer normalizes ownership/mode of `data/updates` to the project owner, preventing the host-side `status.json`/rsync permission problem encountered during the NAS migration.

> **v3.2.9 Skype × Matrix skin:** the operator console, login, and first-run setup now use a black/Matrix-green interface with Skype-cyan controls, status glow, grid/data-stream treatment, and a SkypePBX identity while retaining the compact v2-style operator layout.

> **v3.2.9 conference fix:** **Conference callers** is now durable workspace state instead of a transient browser/runtime flag. The server persists the switch, rehydrates it at startup, self-heals the hot audio-routing cache from persisted state, and exposes live conference diagnostics (`eligible_calls`, routed frames and last routed audio). Caller-to-caller audio uses a clean pre-monitor-gain feed so lowering Caller → Discord monitoring volume does not make conference participants inaudible. Conference input uses its own mixer source and gain path and does not loop through Discord.

> **v3.2.9 managed updates:** system administrators now have **Settings → Updates**. Choose a DiscordPBX release ZIP and press **Update now** (or stage it first if you prefer). After the one-time `install-managed-updater.sh` host setup, the updater preserves `.env` and `data/`, creates code and persistent-state rollback snapshots, validates/builds the release, restarts the container, waits for Docker health, and automatically restores both the previous application code and pre-update runtime state if the new version does not become healthy.

> **v3.2.9 keypad UI:** an open per-call keypad stays open across status/event refreshes and pressing a digit does not force a full call-card refresh. The panel shows `Sent <digit>` plus the Asterisk channel returned by the DTMF API, or the actual error if delivery fails.

> **v3.2.4 DTMF safety fix:** the per-call keypad no longer injects DTMF frames into the call's AudioSocket stream. The bridge resolves the live Asterisk channel by call UUID/Linkedid and uses AMI `PlayDTMF`, preferring the PJSIP/SIP/DAHDI leg and falling back to the Local leg. A DTMF lookup/action failure now returns an error without closing the AudioSocket call.

> **v3.2.4 login redirect fix:** `/` is deliberately allowed through the authentication middleware so the root handler can consistently route first-run systems to `/setup`, authenticated sessions to the console, and unauthenticated browser requests to `/login` even when a copied legacy `.env` still has Basic mode configured. Protected APIs continue returning HTTP 401 rather than HTML redirects.

> **v3.2.3 Discord setup reliability fix:** `/pbx setup` now acknowledges Discord immediately before checking first-time guild authorization, so a slow member lookup cannot produce “The application did not respond.” Member REST lookups are bounded and logged, setup component interactions no longer repeat the network authorization lookup, a standalone `/pbx-setup` fallback opens the same wizard, and unhandled application-command errors are logged and returned as an ephemeral PBX error instead of silently timing out.

> **v3.2.2 contact voicemail bypass:** each contact can opt out of answering-machine/voicemail detection. The override is enforced server-side by matching the outbound number in the owning workspace, so Contact, Quick Dial, History redial, scheduled calls, and manual dials to that saved number all bypass detection consistently. CSV import/export includes `bypass_voicemail_detection`.

> **v3.2.1 Discord-native guild onboarding:** when the bot is added to an unconfigured server it posts a one-time setup prompt in the first writable text channel. A server owner or member with Manage Server can run `/pbx setup` and choose the PBX voice channel, notification text channel, normal PBX user role, and workspace-admin role entirely inside Discord. `/pbx config` shows the stored mapping later. For first-time onboarding, at least one configured PBX system-admin Discord account must also be a member of that guild; this prevents an unrelated server from discovering the bot invite and attaching itself to the phone trunk.

> **v3.2.1 workspace Hang Up All:** the operator-console **Hang up all** action now requires the workspace `bridge` capability instead of PBX system-admin. It is scoped to calls belonging to the currently selected workspace and cancels that workspace's pending auto-redials.

> **v3.2.1 login entry:** `/` redirects unauthenticated users to `/login` (or first-run systems to `/setup`) instead of exposing the operator shell before authentication.

> **v3.1.0 console refresh:** the dialer is back in the sticky header, call statistics are compact, bridge controls and the dialpad are back on the Calls screen, and mobile now has a **More** sheet that exposes History, Audit, Workspaces and Settings instead of silently hiding those pages.

> **v3.1.1 operator preference:** Random Caller ID is remembered server-side per signed-in operator and per Discord workspace. It survives refresh, logout/login, browser changes and container restarts. If a workspace temporarily has no enabled Caller ID entries, the remembered preference stays on but is not sent with calls until a pool is available.


> **v3.2.0 on-demand Discord voice:** the bot remains installed in authorized guilds so OAuth, role checks and presence routing continue to work, but it no longer idles in voice. Each guild joins its configured voice channel only for a call or explicit voice test, then disconnects independently after the idle grace period (20 seconds by default). One guild staying busy no longer keeps unrelated guild voice clients connected.

> **v3.2.0 OAuth setup:** the bridge requests the Server Members intent, adds an OAuth readiness card with the exact redirect URI, and lets a local/system administrator pre-authorize Discord user IDs as PBX system administrators from Settings.

> **v3.2.0 tenant views:** Contacts and History can switch between **Current guild** and **All my guilds**. The combined view is a server-side union of only the workspaces the signed-in Discord account may access; records remain owned by their original guild and calls/redials are routed through that owning workspace.

> **Pool management:** Caller ID and Random Destination pools both support bulk add and bulk remove from the same textarea. Random Destination also retains Remove All.

> **Inherited v3.0.1 hotfix:** includes the startup migration fix for the v3.0 `web.history` / persistent `web.call_history` store issue.

## What is new

### Multi-Discord workspaces

One bot application can serve multiple Discord guilds. Each configured workspace has its own:

- friendly alias
- guild, voice channel and notification text channel
- enabled / Accept inbound / Allow outbound / AUTO routing switches
- inbound priority and simultaneous-call limit
- role-to-capability mapping
- contacts, Quick Dial, schedules and call-history scope
- live presence/eligible-operator state

A call can be attached to one or more Discord workspaces. Active-call controls can add/remove workspaces, transfer a call between workspaces, hold/park/retrieve, send DTMF, mute Caller or Discord, bridge phone calls, claim an inbound call and configure Auto Redial.

Discord supports one voice connection per guild for a bot. v3 therefore uses one configured PBX voice channel per workspace and may have those guild voice connections active simultaneously.

### Discord OAuth + server-side RBAC

The panel supports **Sign in with Discord**. Authorization is enforced on the server for every protected API request; hiding a button in the browser is not considered authorization.

For v3.2, enable **Server Members Intent** in Discord Developer Portal **before starting the new container**. The bridge requests that privileged intent so role changes and guild membership can be evaluated reliably. In **Settings → Discord OAuth**, the panel shows the exact callback URI to add under **OAuth2 → Redirects**. The callback is always `PUBLIC_BASE_URL/auth/discord/callback`. OAuth itself requests only the `identify` scope; guild membership and roles are resolved server-side through the installed bot.

Capabilities are assigned to Discord **role IDs**, so Discord role renames do not break access:

- `panel_access`
- `dial`
- `receive_inbound`
- `contacts`
- `schedule`
- `bridge`
- `workspace_admin`
- `routing`
- `history`
- `audit`
- `settings`

The Workspaces page reads the actual guild roles/channels from Discord and provides a role/capability matrix. A guild owner and configured system administrators retain administrative access.

A local break-glass administrator is also supported. Its PBKDF2 password hash is stored in the application DB; the password must be at least 12 characters. Settings can replace this login and switch between Discord, hybrid, legacy Basic or no-application-auth modes.

**Discord channel permissions still matter.** PBX role authorization controls the bridge and panel, but Discord itself determines who may physically join/listen to a voice channel. Configure Discord channel permission overwrites accordingly.

### Single-DID inbound routing

FreePBX continues to send the DID to the bridge. The bridge then chooses the Discord destination without requiring a FreePBX reload.

The header exposes:

- **AUTO** – choose an enabled workspace with a stable eligible operator in its configured voice channel
- a specific workspace – manual override
- ring-group targets
- DND/off/reject modes through the routing API

AUTO ignores bots, AFK-channel users and self-deafened users. Only members whose mapped role has `receive_inbound`/`workspace_admin` (plus guild/system owners) count as eligible. Workspace priority breaks ties. A short presence-grace period prevents a one-second join/leave from bouncing routing.

Manual routes can carry an expiration timestamp and automatically return to AUTO. If no eligible AUTO destination exists, the configured fallback is used.

### Operator attribution, history and audit

Call history and administrator audit are separate:

- **Call History** stores call direction, number/contact, caller ID, source, workspace(s), operator, who claimed/answered it, result, duration, notes/disposition, retry lineage and a per-call event timeline.
- **Audit Log** records authenticated mutations and bridge lifecycle events with actor identity, workspace, call ID and sanitized details. Audit entries form a SHA-256 hash chain and can be integrity-checked from the UI/API.

This means outbound calls, hangups, routing changes, role changes, contacts, schedules, bridge actions, token/webhook changes and other administrator actions are attributable instead of anonymous.

### Operator console

The v3 UI is responsive down to 320 px and includes:

- 7-column Quick Dial on large desktop, 5-column medium, 3-column phone and 2-column narrow-phone layouts
- searchable/favorite Quick Dial and workspace contacts
- simple **Mute Caller / Mute Discord** wording
- outgoing ringing queue with cancellation
- active-call state, timers, DTMF, hold, park, claim, workspace routing, transfer, conference and Auto Redial controls
- recurring schedules
- five-slot soundboard
- Caller ID and Random destination pools, including large bulk imports
- voicemail/answering-machine detection and automatic hangup
- ringback mute, inbound retro chime and audio gain controls
- health/self-test panel, DNC/calling-hours policy, API tokens and signed webhooks
- SSE realtime event updates with a low-rate status fallback

No browser confirmation dialogs are used for ordinary operator controls. Destructive controls are clearly styled and audit logged.

### Configuration, secrets and backups

Most application settings are now persistent UI-managed configuration instead of requiring `.env` edits. `.env` remains the Docker/bootstrap and v2-migration input.

Sensitive values are stored encrypted in `/app/data/secrets.enc.json`. A Fernet master key is generated as `/app/data/master.key` with mode 0600 unless supplied externally. Saved secret values are never returned to the browser.

Backups use transactionally consistent SQLite snapshots. A restore is staged from the UI and only applied during process startup, before SQLite is opened. The current backup directory and `master.key` are preserved, and a safety backup is created before a restore is queued.

v3 also keeps configuration revisions and takes a one-time startup safety snapshot before the first v3 run.

## First-run setup

A fresh deployment can start with almost all credentials blank. Compose still needs an `.env` file, so create the bootstrap file first:

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs --tail=100
```

The logs print a six-digit one-time setup code. Open:

```text
http://BRIDGE_IP:8088/setup
```

The wizard can store:

- local recovery administrator
- Discord bot token
- Discord application/client ID and OAuth client secret
- public HTTPS base URL
- FreePBX AMI host/user/secret/port
- AudioSocket advertise address
- PBX inbound callback token
- initial Discord guild/voice channel IDs if desired
- maximum simultaneous calls

After replacing the Discord bot token/OAuth secret, restart the container so the Discord gateway reconnects with the new credentials.

## Discord OAuth setup (upgrade or existing install)

Before starting v3.2, open the Discord Developer Portal for the same application used by the PBX bot and enable **Bot → Privileged Gateway Intents → Server Members Intent**. v3.2 deliberately requests this intent because guild membership and current role IDs are authorization inputs.

After v3.2 is running:

1. Sign in with the local break-glass administrator and open **More → Settings → Discord OAuth**.
2. Set **Public URL** to the bridge's externally reachable HTTPS origin, with no trailing path (for example `https://pbx.example.com`).
3. Copy the **Exact Discord redirect URI** displayed by the OAuth card. It will be `PUBLIC_BASE_URL/auth/discord/callback`.
4. In Discord Developer Portal → **OAuth2 → Redirects**, add that exact URI and save.
5. Copy the application's **Application / Client ID** into **System configuration → Discord Client ID**.
6. Put your own Discord numeric user ID into **System-admin Discord User IDs**. This can contain more than one comma-separated ID.
7. Under **Replace secrets**, save the Discord **OAuth client secret**. The stored secret is encrypted and is not returned to the browser.
8. Set **Web authentication** to **Discord OAuth + local admin** and save. Keep the local administrator as the recovery path.
9. Restart `discord-pbx` after changing the bot token or OAuth client secret, then return to the OAuth card. It should report the Client ID, secret, bot token, gateway and Members Intent as ready.
10. Open `/login` and choose **Continue with Discord**. The configured system-admin Discord ID receives system-administrator access after the successful login. Other users receive only the guild/workspace capabilities mapped to their Discord role IDs under **Workspaces**.

The OAuth flow itself requests only `identify`; the bridge does not trust a user-supplied guild list. It resolves guild membership and role capabilities through the installed bot, server-side, on the configured workspaces.

If Discord sign-in still fails, compare the callback shown under **Settings → Discord OAuth** character-for-character with the redirect stored in Discord Developer Portal. The Public URL must look like `https://pbx.example.com`—do not include `/login`, `/auth/discord/callback`, a trailing path, or a query string. If the PBX is behind a reverse proxy, forward `Host` and `X-Forwarded-Proto: https` and keep `TRUST_PROXY_HEADERS=true`. A user who authenticates successfully but receives “does not have a configured PBX panel role” must either be added under **System-admin Discord User IDs** or receive a mapped PBX role in at least one workspace.

## Discord Developer Portal

For Discord OAuth at `https://discordpbx.example.com`, add the exact redirect URI:

```text
https://discordpbx.example.com/auth/discord/callback
```

Enable the **Server Members Intent** for the bot. v3 uses the bot's guild membership to resolve a signed-in Discord user's current role IDs; this is what lets role changes revoke/grant access without maintaining separate PBX user passwords.

The panel's **Invite Bot** action generates an installation URL from the configured Discord Client ID. Discord still requires an authorized guild administrator to approve the bot installation.

## FreePBX inbound metadata callback — required for dynamic routing

Presence-aware/multi-workspace inbound routing requires FreePBX to register the inbound call UUID and caller ID **before** AudioSocket connects.

An example is included at `freepbx/extensions_custom.conf.example`:

```asterisk
[discord-bridge]
exten => s,1,NoOp(Discord AudioSocket Bridge - caller ${CALLERID(all)})
 same => n,Answer()
 same => n,Set(BRIDGE_UUID=${UUID()})
 same => n,Set(DISCORD_CALL_UUID=${BRIDGE_UUID})
 same => n,Set(PBX_META=${CURL(http://BRIDGE_IP:8088/api/pbx/inbound/register?token=PBX_INGRESS_TOKEN&uuid=${BRIDGE_UUID}&number=${CALLERID(num)})})
 same => n,AudioSocket(${BRIDGE_UUID},BRIDGE_IP:9092)
 same => n,Hangup()
```

Use a strong `PBX_INGRESS_TOKEN` and keep port 8088/9092 private to the LAN except for the HTTPS reverse-proxied web panel. Do **not** expose AudioSocket 9092 to the Internet.

After editing custom dialplan:

```bash
fwconsole reload
asterisk -rx 'dialplan show discord-bridge'
```

Without the metadata callback, AudioSocket itself still works, but v3 cannot reliably decide the intended Discord workspace before the inbound call connects.

## Upgrade from v2.x

The intended upgrade keeps the old project untouched until v3 has been extracted and its persistent data copied.

On the builder, first capture the project currently backing `discord-pbx`:

```bash
OLD=$(docker inspect discord-pbx --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}')
echo "$OLD"
```

After `discord-freepbx-bridge-v3.3.0.zip` has been uploaded to `/home/builder/Downloads`, the included upgrade helper can copy the running installation's `.env` and persistent `data/`, validate the new Compose file **before** stopping the current container, then build/start v3:

```bash
cd /home/builder/Downloads
unzip -o discord-freepbx-bridge-v3.3.0.zip
cd discord-freepbx-bridge-v3.3.0
./upgrade-from-current.sh
```

The manual equivalent is:

```bash
cd /home/builder/Downloads
unzip -o discord-freepbx-bridge-v3.3.0.zip

cp "$OLD/.env" /home/builder/Downloads/discord-freepbx-bridge-v3.3.0/.env
mkdir -p /home/builder/Downloads/discord-freepbx-bridge-v3.3.0/data
cp -a "$OLD/data/." /home/builder/Downloads/discord-freepbx-bridge-v3.3.0/data/

cd /home/builder/Downloads/discord-freepbx-bridge-v3.3.0
docker compose config >/dev/null
docker rm -f discord-pbx 2>/dev/null || true
docker compose up -d --build
docker compose ps
docker compose logs --tail=150
```

v3 migrates legacy single-guild `.env` values into an initial **Main** workspace. Legacy contacts, schedules and previously unscoped call-history rows are attached to that workspace so they do not leak into future Discord workspaces. Soundboard/operator settings and number pools are preserved, and the new application DB is created alongside the existing data.

If your copied v2 `.env` uses `WEB_AUTH_MODE=basic`, Basic auth remains available immediately. Configure the Discord OAuth Client ID/secret and a local break-glass administrator in **Settings**, then switch Web Authentication to **Discord OAuth + local admin** (or Hybrid) from the panel when ready.


### One-time managed updater setup

After v3.3.0 is running in its permanent directory, install the host watcher once. A normal standalone NAS install can use:

```bash
cd /opt/discord-pbx/current  # or your actual permanent project directory
sudo ./install-managed-updater.sh
```

If you use the included builder → NAS migration kit, its NAS preparation step installs the watcher ahead of time and migration marks it ready automatically.

Then future release ZIPs can be installed from **Settings → Updates** without SSH. The web container only stages a validated ZIP and an update request in `data/updates/`; the root-owned systemd updater performs the host/Docker changes.

### One-click GitHub release and PBX update

1. Merge the version commit into `main`.
2. On GitHub, open **Actions → Publish DiscordPBX release → Run workflow**. Leave the version blank to read it from `config.py`.
3. The workflow creates or refreshes `discord-freepbx-bridge-v<VERSION>.zip` and its SHA-256 file in the matching GitHub Release.
4. In the PBX console, open **Settings → Updates** and press **Update latest from GitHub**.

The canonical public repository is already `p5zggfdnjx-design/discordpbx`, so no GitHub token is needed. Set `GITHUB_REPO` only when deploying a fork; private repositories also require a read-capable release token in the PBX secret store.

## Reverse proxy

Proxy the HTTPS hostname to:

```text
http://BRIDGE_IP:8088
```

Set `PUBLIC_BASE_URL` (or the same value in Settings) to the external HTTPS origin, for example:

```text
https://discordpbx.example.com
```

The session cookie becomes `Secure` when the public URL is HTTPS. Keep `TRUST_PROXY_HEADERS=true` when the reverse proxy supplies normal `X-Forwarded-*` headers.

## Data layout

Typical persistent files under `/app/data`:

```text
pbx_app.sqlite3            v3 users/workspaces/RBAC/settings/audit
call_history.sqlite3       call history + per-call events
contacts.json              contacts / Quick Dial metadata
scheduled_calls.json       recurring/one-time schedules
caller_id_pool.yaml        permitted outbound caller IDs
random_call_pool.yaml      random destination pool
operator_settings.json     ringback/AMD/audio settings
soundboard.json            soundboard metadata
soundboard/                soundboard audio
secrets.enc.json           encrypted application secrets
master.key                 local encryption key (never in ordinary backups)
backups/                   versioned safety/manual backups
```

## Reliability model

v3 deliberately does **not** pretend to provide high availability when FreePBX and the Discord bridge depend on the same physical/server infrastructure. If that underlying host/site is down, application-level trunk failover on the same box does not solve the outage.

Instead v3 focuses on the useful failure controls for a single-site deployment:

- container restart policy
- clear health/self-tests
- local recovery login
- configuration revisions
- SQLite-consistent backups
- startup safety snapshot
- staged restart restore
- sanitized diagnostics
- explicit inbound fallback behavior when the bridge itself is running

True site/host failover would require FreePBX/bridge or carrier routing on independent infrastructure.

## Security notes

- Use only caller IDs your carrier authorizes you to present.
- Use per-workspace dial permissions, rate limits, DNC/calling-hours policy and call limits when giving other guilds access to a trunk. The Settings UI exposes the per-user dial-rate limit and history/timeline retention controls.
- `WEB_AUTH_MODE=none` should only be used behind another trusted authentication layer.
- Keep a local recovery administrator even when Discord OAuth is the normal login method.
- API tokens are workspace scoped and displayed only once when created.
- Webhook signing secrets are not returned to the browser after storage.
- The administrator audit chain detects ordinary record tampering; it is not a substitute for an external append-only log against a hostile host administrator.
- Automatic call recording is **not enabled or implemented by v3.2.9**. If recording is added later, consent/retention controls should be explicit rather than silently recording every call.

## Validation boundary

The release package is statically checked and includes offline migration/RBAC/backup/web-API/responsive-layout tests. These cannot prove live behavior against your exact services. After the first deployment, explicitly validate:

1. Discord OAuth callback/login
2. role access in each real guild
3. simultaneous voice connections in two guilds
4. one inbound AUTO-routed call
5. one outbound call and DTMF
6. voicemail detection
7. hold/park/transfer/conference behavior
8. AMI blind transfer on your Asterisk/FreePBX build

The Health and Audit pages are designed to make those validation failures observable without digging through every Docker log.
