from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from collector import parse_args
from migrate_data import migrate


class StorageMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'old'
        self.source.mkdir()
        self.destination = self.root / 'new'
        self.writer = sqlite3.connect(self.source / 'history.sqlite3')
        self.addCleanup(self.writer.close)
        self.writer.execute('PRAGMA journal_mode=WAL')
        self.writer.execute('PRAGMA wal_autocheckpoint=0')
        self.writer.execute(
            'CREATE TABLE earnings(id INTEGER PRIMARY KEY, amount REAL, model TEXT, saved BLOB)'
        )
        self.writer.executemany(
            'INSERT INTO earnings VALUES(?,?,?,?)',
            [(1, 0.15, 'gemma', b'one'), (2, -0.01, 'oss', b'two')],
        )
        self.writer.execute('CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID')
        self.writer.execute('INSERT INTO settings VALUES(?,?)', ('plan', '{"mode":"observe"}'))
        self.writer.commit()

    def test_includes_uncheckpointed_wal_and_preserves_all_rows(self):
        self.assertGreater((self.source / 'history.sqlite3-wal').stat().st_size, 0)
        result = migrate(self.source, self.destination)
        self.assertTrue(result['verified'])
        self.assertEqual(result['tables'], {'earnings': 2, 'settings': 1})
        self.assertTrue((self.source / 'history.sqlite3').is_file())
        with sqlite3.connect(self.destination / 'history.sqlite3') as copied:
            self.assertEqual(
                copied.execute('SELECT * FROM earnings').fetchall(),
                self.writer.execute('SELECT * FROM earnings').fetchall(),
            )
            self.assertEqual(copied.execute('PRAGMA quick_check').fetchone(), ('ok',))
        copied.close()

    def test_private_phone_config_is_copied_exactly_with_owner_permissions(self):
        config = b'{"host":"test.invalid","owner":"test"}\n'
        (self.source / 'remote-access.json').write_bytes(config)
        result = migrate(self.source, self.destination)
        self.assertTrue(result['phoneConfigPreserved'])
        self.assertEqual((self.destination / 'remote-access.json').read_bytes(), config)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)
        for name in ('history.sqlite3', 'remote-access.json'):
            self.assertEqual((self.destination / name).stat().st_mode & 0o777, 0o600)

    def test_missing_phone_config_stays_disabled(self):
        result = migrate(self.source, self.destination)
        self.assertFalse(result['phoneConfigPreserved'])
        self.assertFalse((self.destination / 'remote-access.json').exists())

    def test_repeated_migration_does_not_replace_new_history(self):
        migrate(self.source, self.destination)
        with sqlite3.connect(self.destination / 'history.sqlite3') as copied:
            copied.execute('INSERT INTO earnings VALUES(3,1,"later",NULL)')
        copied.close()
        with self.assertRaises(FileExistsError):
            migrate(self.source, self.destination)
        with sqlite3.connect(self.destination / 'history.sqlite3') as copied:
            self.assertEqual(copied.execute('SELECT COUNT(*) FROM earnings').fetchone()[0], 3)
        copied.close()

    def test_corrupt_phone_config_leaves_no_partial_destination(self):
        (self.source / 'remote-access.json').write_text('{')
        with self.assertRaises(ValueError):
            migrate(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob('.bloom-migration-*')), [])

    def test_extra_files_are_not_silently_lost(self):
        (self.source / 'unknown-settings.json').write_text('{}')
        with self.assertRaises(ValueError):
            migrate(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_missing_source_never_creates_an_empty_history(self):
        with self.assertRaises(FileNotFoundError):
            migrate(self.root / 'missing', self.destination)
        self.assertFalse(self.destination.exists())

    def test_existing_symlink_is_not_followed_or_replaced(self):
        self.destination.symlink_to(self.root / 'missing-target')
        with self.assertRaises(FileExistsError):
            migrate(self.source, self.destination)
        self.assertTrue(self.destination.is_symlink())

    def test_defaults_use_support_without_probing_documents(self):
        with (
            patch('pathlib.Path.home', return_value=self.root),
            patch('pathlib.Path.exists', side_effect=AssertionError('No storage probes')),
        ):
            self.assertEqual(
                parse_args([]).data,
                self.root / 'Library/Application Support/Bloom Dashboard/history.sqlite3',
            )

    def test_explicit_development_path_still_works(self):
        self.assertEqual(
            parse_args(['--data', str(self.root / 'test.sqlite3')]).data, self.root / 'test.sqlite3'
        )


if __name__ == '__main__':
    unittest.main()
