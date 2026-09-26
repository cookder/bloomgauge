"""Per-install id and edition flag, with temporary history and config only."""

from pathlib import Path
import re
import tempfile
import unittest

from history import History
from installation import KEY, installation_id, personal_edition


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_id_is_random_hex_and_survives_reopen(self):
        h = History(self.root / 'history.sqlite')
        first = installation_id(h)
        self.assertRegex(first, r'^[a-f0-9]{32}$')
        self.assertEqual(installation_id(h), first)
        h.close()
        h = History(self.root / 'history.sqlite')
        self.assertEqual(installation_id(h), first)
        self.assertNotEqual(installation_id(History(':memory:')), first)
        h.close()

    def test_invalid_saved_id_is_replaced(self):
        h = History(':memory:')
        for bad in ('short', 'A' * 32, 42, None):
            with self.subTest(bad=bad):
                h.cache(KEY, bad)
                value = installation_id(h)
                self.assertTrue(re.fullmatch(r'[a-f0-9]{32}', value))
                self.assertEqual(h.cache(KEY), value)

    def test_only_a_personal_config_enables_the_personal_edition(self):
        config = self.root / 'product-config.json'
        self.assertFalse(personal_edition(config))  # dev runs have no config
        for text, expected in (
            ('{"edition": "personal"}', True),
            ('{"edition": "free"}', False),
            ('[]', False),
            ('not json', False),
        ):
            with self.subTest(text=text):
                config.write_text(text)
                self.assertIs(personal_edition(config), expected)


if __name__ == '__main__':
    unittest.main()
