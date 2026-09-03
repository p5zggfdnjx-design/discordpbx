from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any

import aiohttp


CACHE_SECONDS = 60.0


def _error_detail(payload: Any, status: int) -> str:
    if isinstance(payload, dict):
        message = str(payload.get("message") or "").strip()
        if message:
            return f"GitHub HTTP {status}: {message}"
    return f"GitHub HTTP {status}"


def apply() -> None:
    """Harden GitHub-release checks/downloads against rate limits and stale tokens.

    The Settings page refreshes local updater status frequently. GitHub release
    metadata must not be fetched at the same cadence: unauthenticated GitHub API
    clients only receive a small hourly request budget. This patch adds a short
    server-side cache, retries public repositories anonymously when a stored token
    is stale/invalid, and downloads public release assets through their normal
    browser download URL instead of spending another API request on the asset.
    """
    import webui_v3

    cls = webui_v3.WebControlServer
    if getattr(cls, "_github_release_guard_applied", False):
        return

    original_init = cls.__init__

    def __init__(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._github_release_cache: dict[str, Any] = {}

    async def _github_release_request(self, session: aiohttp.ClientSession, url: str) -> tuple[int, Any, bool]:
        token = self.secret_store.get("github_release_token", "")
        attempts = [True, False] if token else [False]
        last_status = 0
        last_payload: Any = {}
        for use_token in attempts:
            headers = {
                "Accept": "application/vnd.github+json",
                "User-Agent": f"DiscordPBX/{self.config.version}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            if use_token:
                headers["Authorization"] = f"Bearer {token}"
            async with session.get(url, headers=headers) as resp:
                last_status = int(resp.status)
                try:
                    last_payload = await resp.json(content_type=None)
                except Exception:
                    last_payload = {}
                if resp.status < 400:
                    return last_status, last_payload, use_token
                # A stale/revoked token should not break updates from a public repo.
                if use_token and resp.status in {401, 403, 404}:
                    continue
                return last_status, last_payload, use_token
        return last_status, last_payload, False

    async def _github_latest_release(self, *, force: bool = False) -> dict[str, Any]:
        repo = str(self.db.get_setting("github_repo", "") or self.config.github_repo or "").strip()
        if not repo:
            raise ValueError("configure a GitHub release repository first (owner/repository)")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("invalid GitHub repository setting")

        now = time.monotonic()
        cache = getattr(self, "_github_release_cache", {}) or {}
        if not force and cache.get("repo") == repo and now - float(cache.get("at", 0.0)) < CACHE_SECONDS:
            return dict(cache["value"])

        url = f"https://api.github.com/repos/{repo}/releases/latest"
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            status, payload, authenticated = await _github_release_request(self, session, url)

        if status == 404:
            raise ValueError("GitHub repository/release was not found or is not accessible")
        if status == 403:
            detail = _error_detail(payload, status)
            if isinstance(payload, dict) and "rate limit" in str(payload.get("message", "")).lower():
                detail = "GitHub API rate limit reached; wait briefly and retry. DiscordPBX now caches release checks to prevent this."
            raise ValueError(detail)
        if status >= 400:
            raise ValueError(_error_detail(payload, status))

        assets = payload.get("assets") or []
        zips = [a for a in assets if str(a.get("name", "")).lower().endswith(".zip")]
        preferred = [a for a in zips if "discord-freepbx-bridge" in str(a.get("name", "")).lower()]
        asset = (preferred or zips or [None])[0]
        if not asset:
            raise ValueError("latest GitHub release has no ZIP asset")
        tag = str(payload.get("tag_name") or payload.get("name") or "")
        value = {
            "repo": repo,
            "tag": tag,
            "name": str(payload.get("name") or tag),
            "published_at": payload.get("published_at"),
            "html_url": payload.get("html_url"),
            "asset": {
                "name": str(asset.get("name") or "release.zip"),
                "size": int(asset.get("size") or 0),
                "api_url": str(asset.get("url") or ""),
                "browser_url": str(asset.get("browser_download_url") or ""),
            },
            "newer": self._version_tuple(tag) > self._version_tuple(self.config.version),
            "authenticated": bool(authenticated),
        }
        self._github_release_cache = {"repo": repo, "at": now, "value": dict(value)}
        return value

    async def _download_release_asset(self, session: aiohttp.ClientSession, asset: dict[str, Any], tmp) -> int:
        token = self.secret_store.get("github_release_token", "")
        browser_url = str(asset.get("browser_url") or "")
        api_url = str(asset.get("api_url") or "")
        attempts: list[tuple[str, dict[str, str]]] = []

        # Public release assets should use the normal download URL: no token and
        # no GitHub API quota is required for the binary transfer.
        if browser_url:
            attempts.append((browser_url, {"User-Agent": f"DiscordPBX/{self.config.version}"}))
        if api_url and token:
            attempts.append((api_url, {
                "Accept": "application/octet-stream",
                "Authorization": f"Bearer {token}",
                "User-Agent": f"DiscordPBX/{self.config.version}",
                "X-GitHub-Api-Version": "2022-11-28",
            }))
        if api_url and not token:
            attempts.append((api_url, {
                "Accept": "application/octet-stream",
                "User-Agent": f"DiscordPBX/{self.config.version}",
                "X-GitHub-Api-Version": "2022-11-28",
            }))

        if not attempts:
            raise ValueError("GitHub release asset URL is missing")

        last_error = "GitHub asset download failed"
        for url, headers in attempts:
            received = 0
            try:
                async with session.get(url, headers=headers, allow_redirects=True) as resp:
                    if resp.status >= 400:
                        try:
                            payload = await resp.json(content_type=None)
                        except Exception:
                            payload = {}
                        last_error = _error_detail(payload, int(resp.status))
                        continue
                    with tmp.open("wb") as fh:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            received += len(chunk)
                            if received > 50 * 1024 * 1024:
                                raise ValueError("GitHub update ZIP is larger than 50 MB")
                            fh.write(chunk)
                if received > 0:
                    return received
                last_error = "GitHub returned an empty release asset"
            except aiohttp.ClientError as exc:
                last_error = f"GitHub asset download failed: {exc}"
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        raise ValueError(last_error)

    async def _stage_github_release(self, actor: dict[str, Any]) -> dict[str, Any]:
        info = await _github_latest_release(self, force=True)
        asset = info["asset"]
        tmp = self._updates_dir / "pending.github"
        final = self._updates_dir / "pending.zip"
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        timeout = aiohttp.ClientTimeout(total=120)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                received = await _download_release_asset(self, session, asset, tmp)
            inspected = await asyncio.to_thread(self._inspect_update_zip, tmp)
            digest = await asyncio.to_thread(lambda: hashlib.sha256(tmp.read_bytes()).hexdigest())
            tmp.replace(final)
            meta = {
                "filename": asset["name"],
                "version": inspected["version"],
                "sha256": digest,
                "bytes": received,
                "expanded_bytes": inspected["expanded_bytes"],
                "uploaded_at": time.time(),
                "uploaded_by": actor.get("name", "system admin"),
                "source": "github",
                "github_repo": info["repo"],
                "github_tag": info["tag"],
            }
            (self._updates_dir / "pending_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            self.db.audit(
                "system.update.github_staged",
                actor_user_id=actor["user_id"],
                actor_name=actor["name"],
                auth_type=actor.get("auth_type", "session"),
                entity_type="system",
                entity_id=inspected["version"],
                detail={"repo": info["repo"], "tag": info["tag"], "sha256": digest, "bytes": received},
            )
            return meta
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    cls.__init__ = __init__
    cls._github_latest_release = _github_latest_release
    cls._stage_github_release = _stage_github_release
    cls._github_release_guard_applied = True
