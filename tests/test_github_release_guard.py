from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import github_release_guard


RELEASE = {
    "tag_name": "v3.3.16",
    "name": "DiscordPBX v3.3.16",
    "published_at": "2026-09-03T00:00:00Z",
    "html_url": "https://github.com/example/discordpbx/releases/tag/v3.3.16",
    "assets": [
        {
            "name": "discord-freepbx-bridge-v3.3.16.zip",
            "size": 4,
            "url": "https://api.github.com/repos/example/discordpbx/releases/assets/1",
            "browser_download_url": "https://github.com/example/discordpbx/releases/download/v3.3.16/discord-freepbx-bridge-v3.3.16.zip",
        }
    ],
}


class FakeDB:
    def __init__(self):
        self.audit_rows = []

    def get_setting(self, key, default=None):
        if key == "github_repo":
            return "example/discordpbx"
        return default

    def audit(self, *args, **kwargs):
        self.audit_rows.append((args, kwargs))


class FakeSecrets:
    def __init__(self, token=""):
        self.token = token

    def get(self, key, default=""):
        if key == "github_release_token":
            return self.token
        return default


class FakeConfig:
    version = "3.3.15"
    github_repo = "example/discordpbx"


class FakeServer:
    def __init__(self, token="", updates_dir=None):
        self.config = FakeConfig()
        self.db = FakeDB()
        self.secret_store = FakeSecrets(token)
        self._updates_dir = Path(updates_dir or tempfile.mkdtemp())
        self._updates_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _version_tuple(value):
        import re
        nums = re.findall(r"\d+", str(value or ""))
        return tuple(int(x) for x in nums[:4]) if nums else (0,)

    def _inspect_update_zip(self, _path):
        return {"version": "3.3.16", "expanded_bytes": 123}


class FakeContent:
    def __init__(self, data=b""):
        self.data = data

    async def iter_chunked(self, _size):
        if self.data:
            yield self.data


class FakeResponse:
    def __init__(self, status, payload=None, data=b""):
        self.status = status
        self.payload = payload if payload is not None else {}
        self.content = FakeContent(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self.payload


class FakeSession:
    routes = {}
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, headers=None, allow_redirects=False):
        headers = dict(headers or {})
        self.__class__.calls.append((url, headers, allow_redirects))
        route = self.__class__.routes[url]
        if callable(route):
            return route(headers)
        if isinstance(route, list):
            return route.pop(0)
        return route


class GitHubReleaseGuardTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_webui_v3 = sys.modules.get("webui_v3")
        sys.modules["webui_v3"] = types.SimpleNamespace(WebControlServer=FakeServer)
        github_release_guard.apply()

    @classmethod
    def tearDownClass(cls):
        if cls.original_webui_v3 is None:
            sys.modules.pop("webui_v3", None)
        else:
            sys.modules["webui_v3"] = cls.original_webui_v3

    def setUp(self):
        FakeSession.routes = {}
        FakeSession.calls = []

    async def test_bad_stored_token_retries_public_release_anonymously_and_caches_result(self):
        url = "https://api.github.com/repos/example/discordpbx/releases/latest"

        def release_route(headers):
            if "Authorization" in headers:
                return FakeResponse(403, {"message": "Bad credentials"})
            return FakeResponse(200, RELEASE)

        FakeSession.routes[url] = release_route
        server = FakeServer(token="stale-token")
        with patch.object(github_release_guard.aiohttp, "ClientSession", FakeSession):
            first = await server._github_latest_release()
            second = await server._github_latest_release()

        self.assertEqual(first["tag"], "v3.3.16")
        self.assertEqual(second["tag"], "v3.3.16")
        self.assertEqual(len(FakeSession.calls), 2, "second release check should be served from the cache")
        self.assertIn("Authorization", FakeSession.calls[0][1])
        self.assertNotIn("Authorization", FakeSession.calls[1][1])

    async def test_public_asset_download_uses_browser_url_without_token_or_api_asset_request(self):
        release_url = "https://api.github.com/repos/example/discordpbx/releases/latest"
        browser_url = RELEASE["assets"][0]["browser_download_url"]
        api_asset_url = RELEASE["assets"][0]["url"]
        FakeSession.routes[release_url] = FakeResponse(200, RELEASE)
        FakeSession.routes[browser_url] = FakeResponse(200, data=b"zip-bytes")
        FakeSession.routes[api_asset_url] = FakeResponse(500, {"message": "API asset route should not be used"})

        with tempfile.TemporaryDirectory() as td:
            server = FakeServer(token="", updates_dir=td)
            actor = {"user_id": "admin", "name": "Admin", "auth_type": "session"}
            with patch.object(github_release_guard.aiohttp, "ClientSession", FakeSession):
                meta = await server._stage_github_release(actor)

            self.assertEqual(meta["version"], "3.3.16")
            self.assertEqual(meta["bytes"], len(b"zip-bytes"))
            self.assertTrue((Path(td) / "pending.zip").exists())

        called_urls = [row[0] for row in FakeSession.calls]
        self.assertIn(browser_url, called_urls)
        self.assertNotIn(api_asset_url, called_urls)
        browser_headers = next(headers for url, headers, _ in FakeSession.calls if url == browser_url)
        self.assertNotIn("Authorization", browser_headers)


if __name__ == "__main__":
    unittest.main()
