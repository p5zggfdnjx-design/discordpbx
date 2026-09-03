# DiscordPBX v3.3.16

## GitHub updater reliability

- Fixes the Settings page exhausting GitHub's unauthenticated API quota by repeatedly checking the latest release while the page is open.
- Adds a 60-second server-side cache for GitHub release metadata.
- Public release ZIPs now download through the normal `browser_download_url`, avoiding an unnecessary GitHub API request for the binary asset.
- If a saved GitHub token is stale, revoked, or otherwise rejected, DiscordPBX retries release discovery anonymously so public repositories continue to update normally.
- GitHub API failures now surface the returned GitHub message when available, including a clear rate-limit explanation.
- Keeps authenticated API asset download as a fallback for private repositories.

## Tests

Adds regression coverage for:

- stale-token → anonymous public-release fallback;
- release metadata caching;
- public release asset download without an API asset request or Authorization header.
