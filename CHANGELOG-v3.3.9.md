# DiscordPBX v3.3.9

- Add `bootstrap-managed-install.sh` to adopt an existing versioned/unzipped deployment into a stable `/opt/discord-pbx` production installation.
- Preserve `.env`, `data/`, and host-local `docker-compose.override.yml` across managed updates and rollback operations.
- Migrate existing runtime state into the stable install and create a dated contact recovery copy before cutover.
- Refuse migration if the destination would contain fewer contacts than the source deployment.
- Detect and terminate only recognized stale DiscordPBX host Python listeners that block the configured web or AudioSocket ports; unrelated listeners abort the cutover instead of being killed.
- Generate a host-local Compose override with real upstream DNS resolvers so containers do not inherit an unusable `127.0.0.53`/loopback resolver.
- Verify both Docker health and `discord.com` DNS resolution before declaring the managed installation successful; restore the old Compose deployment when possible if cutover fails.
- Install the managed updater against the stable production directory so future Settings → Updates actions no longer depend on a Downloads/release folder.
