# DiscordPBX v3.3.8

- Recover legacy or orphaned contact workspace ownership so Contacts and Speed Dial do not silently disappear after workspace/update migrations.
- Re-check contact ownership when the Contacts API is read, while never moving contacts that still belong to another valid workspace.
- Extend updater continuity checks to reject a release that loses `contacts.json` or reduces its contact row count.
- Add regression tests for contact recovery and contact continuity protection.
