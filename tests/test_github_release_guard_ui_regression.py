from pathlib import Path
import unittest


class GitHubUpdaterUIRegressionTests(unittest.TestCase):
    def test_frequent_settings_refresh_is_backed_by_server_cache(self):
        runtime = Path("runtime_hotfix.py").read_text(encoding="utf-8")
        guard = Path("github_release_guard.py").read_text(encoding="utf-8")
        self.assertIn("setInterval", runtime)
        self.assertIn("CACHE_SECONDS = 60.0", guard)
        self.assertIn("_github_release_cache", guard)


if __name__ == "__main__":
    unittest.main()
