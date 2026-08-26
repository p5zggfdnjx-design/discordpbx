import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

# Importing EventBus installs the same request compatibility hook used by webui.py.
import eventbus  # noqa: F401


class CachedRequestBodyTests(unittest.IsolatedAsyncioTestCase):
    async def test_audit_read_does_not_hide_json_from_handler(self):
        seen = {}

        @web.middleware
        async def audit_like_middleware(request, handler):
            seen["raw"] = await request.read()
            seen["after_read"] = request.can_read_body
            return await handler(request)

        async def conference_like_handler(request):
            body = await request.json() if request.can_read_body else {}
            return web.json_response({
                "has_enabled": "enabled" in body,
                "enabled": body.get("enabled"),
            })

        app = web.Application(middlewares=[audit_like_middleware])
        app.router.add_post("/api/calls/conference-mode", conference_like_handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)

        response = await client.post(
            "/api/calls/conference-mode",
            json={"enabled": True},
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertTrue(seen["raw"])
        self.assertTrue(seen["after_read"], "cached request body must remain readable")
        self.assertTrue(payload["has_enabled"])
        self.assertIs(payload["enabled"], True)


if __name__ == "__main__":
    unittest.main()
