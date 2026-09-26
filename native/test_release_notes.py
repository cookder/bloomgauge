import copy
import json
import pathlib
import plistlib
import tempfile
import unittest
from release_notes import snapshot


class ReleaseNotesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name) / 'Contents' / 'Resources'
        self.root.mkdir(parents=True)
        self.info = self.root.parent / 'Info.plist'
        self.info.write_bytes(plistlib.dumps({'CFBundleShortVersionString': '1.36.19'}))
        self.notes = {
            'id': 'bloom-1.36.19-beta23',
            'version': '1.36.19',
            'title': 'What changed',
            'highlights': [
                {
                    'title': 'Clear controls',
                    'detail': 'Explicit On and Manual preserve the saved plan.',
                }
            ],
        }
        self.path = self.root / 'release-notes.json'
        self.path.write_text(json.dumps(self.notes))

    def test_only_exact_installed_version_exposes_bundled_notes(self):
        self.assertEqual(
            snapshot(self.root), {'installedVersion': '1.36.19', 'release': self.notes}
        )
        self.info.write_bytes(plistlib.dumps({'CFBundleShortVersionString': '1.33.8'}))
        self.assertEqual(snapshot(self.root), {'installedVersion': '1.33.8', 'release': None})

    def test_source_preview_missing_or_invalid_plist_never_claims_candidate_installed(self):
        self.info.unlink()
        self.assertEqual(snapshot(self.root), {'installedVersion': 'development', 'release': None})
        for value in ({'CFBundleShortVersionString': 'unknown'}, ['bad plist root']):
            self.info.write_bytes(plistlib.dumps(value))
            self.assertIsNone(snapshot(self.root)['release'])

    def test_malformed_oversize_or_markup_metadata_is_hidden(self):
        cases = ['bad JSON', '[' * 1200, ' ' * 32769]
        for text in cases:
            self.path.write_text(text)
            self.assertIsNone(snapshot(self.root)['release'])
        for change in (
            {'title': '<script>x</script>'},
            {'id': '../x'},
            {'highlights': []},
            {'highlights': [{'title': 'x', 'detail': 'x' * 601}]},
            {'extra': 'remote-url'},
        ):
            data = {**copy.deepcopy(self.notes), **change}
            self.path.write_text(json.dumps(data))
            self.assertIsNone(snapshot(self.root)['release'])

    def test_missing_notes_are_a_valid_no_notice_result(self):
        self.path.unlink()
        self.assertEqual(snapshot(self.root), {'installedVersion': '1.36.19', 'release': None})


if __name__ == '__main__':
    unittest.main()
