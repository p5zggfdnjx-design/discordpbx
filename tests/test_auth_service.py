import unittest
import sys
import types

aiohttp = types.ModuleType("aiohttp")
aiohttp.web = types.ModuleType("aiohttp.web")
sys.modules.setdefault("aiohttp", aiohttp)
sys.modules.setdefault("aiohttp.web", aiohttp.web)

from auth_service import AuthService


class PublicBaseUrlTests(unittest.TestCase):
    def test_normalizes_https_origin(self):
        self.assertEqual(
            AuthService.normalize_public_base_url(" HTTPS://pbx.example.com/ ", require_https=True),
            "https://pbx.example.com",
        )

    def test_rejects_path_in_public_url(self):
        with self.assertRaisesRegex(ValueError, "must not include a path"):
            AuthService.normalize_public_base_url("https://pbx.example.com/login", require_https=True)

    def test_requires_https_for_remote_host(self):
        with self.assertRaisesRegex(ValueError, "requires an HTTPS"):
            AuthService.normalize_public_base_url("http://pbx.example.com", require_https=True)

    def test_allows_http_localhost_for_development(self):
        self.assertEqual(
            AuthService.normalize_public_base_url("http://localhost:8088", require_https=True),
            "http://localhost:8088",
        )

    def test_rejects_protocol_relative_return_target(self):
        self.assertEqual(AuthService.safe_return_to("//example.com"), "/")
        self.assertEqual(AuthService.safe_return_to("/settings"), "/settings")


if __name__ == "__main__":
    unittest.main()
