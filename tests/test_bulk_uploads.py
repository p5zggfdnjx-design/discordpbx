import unittest

import webui_legacy
from bulk_uploads import (
    MAX_BULK_PHONE_ENTRIES,
    MAX_BULK_UPLOAD_BYTES,
    apply,
    inject_bulk_file_uploads,
    validate_bulk_raw,
)


class BulkUploadTests(unittest.TestCase):
    def test_limits_match_release_policy(self):
        self.assertEqual(MAX_BULK_UPLOAD_BYTES, 25_000_000)
        self.assertEqual(MAX_BULK_PHONE_ENTRIES, 30_000_000)

    def test_validator_counts_utf8_bytes(self):
        validate_bulk_raw("1" * 100)
        with self.assertRaisesRegex(ValueError, "max 25 MB"):
            validate_bulk_raw("é" * 12_500_001)

    def test_console_file_upload_injection_is_idempotent(self):
        html = '<html><body><textarea id="cidBulk"></textarea></body></html>'
        first = inject_bulk_file_uploads(html)
        second = inject_bulk_file_uploads(first)
        self.assertIn('id="pbx-bulk-file-upload-script"', first)
        self.assertIn("MAX_BYTES=25000000", first)
        self.assertEqual(first, second)

    def test_apply_updates_shared_legacy_limits(self):
        apply()
        self.assertEqual(webui_legacy.MAX_BULK_PASTE_CHARS, 25_000_000)
        self.assertEqual(webui_legacy.MAX_BULK_PHONE_ENTRIES, 30_000_000)
        webui_legacy.WebControlServer._validate_bulk_raw("1" * 100)


if __name__ == "__main__":
    unittest.main()
