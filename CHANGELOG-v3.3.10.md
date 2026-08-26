# DiscordPBX v3.3.10

Reliability and tenant-safety pass focused on preserving PBX state through workspace recovery, upgrades, and administrative changes.

## Fixed

- Preserve contacts that still remember a missing workspace ID instead of silently reassigning them to whichever workspace happens to exist.
- Continue adopting truly unassigned pre-v3 contacts, but refuse to guess between multiple workspaces when there is no valid default/preferred workspace.
- Prevent non-system administrators from creating or mutating global contacts through CSV import or number-collision merges.
- Preserve local-user workspace access across configuration revision restores.
- Save recoverable configuration revisions before workspace deletion and workspace role changes.
- Immediately invalidate Discord RBAC capability caches after workspace, role, or revision changes instead of waiting for the cache TTL.

## Hardening

- Persistent-state guards now verify hashed row identities in addition to table/contact counts, detecting same-count replacement of workspaces, RBAC mappings, local-user bindings, API tokens, webhooks, DNC entries, and contacts.
- Stable managed adoption now captures the old deployment's full state baseline and verifies it after migration before cutover.
- System diagnostics now expose contact-ownership health and highlight Discord guilds connected to the bot but missing from PBX workspace configuration.

## Polish

- The web console version label is populated from the running application version rather than a stale hard-coded UI string.
- Added regression coverage for orphaned contact ownership, CSV tenant safety, local-user revision recovery, capability-cache invalidation, managed-adoption continuity, and same-count identity loss.
