import tempfile
import unittest
from pathlib import Path

from prefix_blocks import PrefixBlockStore, extract_bulk_prefixes, normalize_prefix, _inject_ui


class PrefixBlockTests(unittest.TestCase):
    def test_normalize_and_bulk_parse(self):
        self.assertEqual(normalize_prefix('407-200'), '407200')
        self.assertEqual(normalize_prefix('1 (352) 201'), '352201')
        valid, invalid = extract_bulk_prefixes('407200\n352-201\n407200\n123100')
        self.assertEqual(valid, ['352201', '407200'])
        self.assertIn('123100', invalid)

    def test_store_bulk_add_remove_and_generation(self):
        with tempfile.TemporaryDirectory() as td:
            store = PrefixBlockStore(str(Path(td) / 'blocks.yaml'))
            result = store.add_bulk('407200\n407201\n352202')
            self.assertEqual(result['added'], 3)
            self.assertEqual(store.counts(), (3, 3))
            for _ in range(100):
                number = store.random_number('407200')
                self.assertRegex(number, r'^1407200\d{4}$')
            removed = store.remove_bulk('407201\n352202')
            self.assertEqual(removed['removed'], 2)
            self.assertEqual(store.enabled_prefixes(), ['407200'])

    def test_ui_injection_is_idempotent(self):
        page = '<html><body><div id="cidSub"><section></section></div><div id="randomSub"><section></section></div></body></html>'
        once = _inject_ui(page)
        twice = _inject_ui(once)
        self.assertEqual(once, twice)
        self.assertEqual(once.count('pbx-prefix-blocks-script'), 1)
        self.assertIn('Bulk Add Blocks', once)
        self.assertIn('mode=blocks', once)
        self.assertIn('Owned/verified NPA-NXX blocks only', once)


if __name__ == '__main__':
    unittest.main()
