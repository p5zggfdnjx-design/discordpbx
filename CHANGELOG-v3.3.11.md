# DiscordPBX v3.3.11

Contact-sharing, user-presence, and storage-reliability release.

## Contacts

- Any authenticated operator with the workspace `contacts` capability can create a new Global contact.
- CSV import can create new Global contacts for Contacts-capable operators.
- Existing Global contacts remain protected: non-system administrators cannot edit/delete them or use a CSV number collision to overwrite/promote them.
- The Contacts UI keeps the Global scope selector enabled for permitted operators and hides Global Edit/Delete controls when the server would reject them.

## Users and presence

- Add a persistent unified identity directory for Discord users, local users, and the local break-glass administrator.
- Track first/last-seen and login metadata without storing raw client IPs or user-agent strings.
- Add an Online users card to the Workspaces page using active authenticated PBX sessions.
- Expose known-user and online-user counts through the existing authenticated status surface.

## Database consolidation and migration safety

- Consolidate live call-history tables into the existing `pbx_app.sqlite3` SQLite/WAL application database.
- Preserve `call_history.sqlite3` as a compatibility/rollback mirror and continue mirroring history mutations so existing updater continuity checks remain effective.
- Migrate call history transactionally with a migration ledger so a failed or interrupted migration can be retried safely without duplicating calls.
- Create SQLite-safe backups of the legacy call-history database before cutover.
- Snapshot and fingerprint the existing contacts, schedules, operator settings, number pools, block pools, and soundboard metadata into a legacy-data catalog before migration work.
- Keep legacy flat stores intact in this release rather than destructively rewriting them; this provides an explicit rollback boundary for later normalization.
- Add component schema/version metadata so future storage migrations can be additive and versioned.

## Releases

- The release workflow keeps the current release as the only visible GitHub Release entry after successful publication, while historical Git tags remain intact.
