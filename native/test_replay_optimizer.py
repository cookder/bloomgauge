"""Release replay must fail closed and never reach live state or commands."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


class ReplayTests(unittest.TestCase):
    def invoke(self, fixture=None, compare=None):
        with tempfile.TemporaryDirectory(prefix='bloom-replay-contract-') as temp:
            root = Path(temp)
            fixtures = root / 'fixtures'
            fixtures.mkdir()
            if fixture is not None:
                (fixtures / 'case.json').write_text(json.dumps(fixture))
            output = root / 'report.json'
            command = [
                sys.executable,
                '-B',
                str(ROOT / 'replay_optimizer.py'),
                '--fixtures',
                str(fixtures),
                '--output',
                str(output),
            ]
            if compare:
                baseline = root / 'baseline.json'
                baseline.write_text(json.dumps(compare))
                command += ['--compare', str(baseline)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            return result, json.loads(output.read_text()) if output.exists() else None

    def fixture(self):
        return json.loads((ROOT / 'replay/fixtures/verified-paid.json').read_text())

    def test_empty_suite_never_passes(self):
        result, report = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(report)
        self.assertIn('empty replay cannot pass', result.stderr)

    def test_wrong_expectation_reports_actual_outcome_and_fails(self):
        case = self.fixture()
        key = next(iter(case['expected']))
        case['expected'][key] = 'deliberately wrong'
        result, report = self.invoke(case)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(report['passed'])
        self.assertNotEqual(report['cases'][0]['observed'][key], case['expected'][key])

    def test_changed_outcomes_visible_in_comparison(self):
        case = self.fixture()
        previous = {
            'schema': 'bloom-optimizer-replay-v1',
            'cases': [{'id': case['id'], 'passed': False, 'observed': {'old': 'outcome'}}],
        }
        result, report = self.invoke(case, previous)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(report['passed'])
        self.assertEqual(report['changedOutcomes'][0]['id'], case['id'])
        self.assertEqual(report['changedOutcomes'][0]['previous'], {'old': 'outcome'})

    def test_live_access_is_rejected_before_io(self):
        # All attempts run behind the hook in child processes, never in the test runner.
        actions = [
            "subprocess.run(['/usr/bin/true'])",
            "socket.socket().connect(('127.0.0.1',9))",
            "(Path.home()/'.darkbloom'/'does-not-exist').read_text()",
            "sqlite3.connect(str(Path.home()/'Library/Application Support/Bloom Dashboard/history.sqlite3'))",
            "sqlite3.connect((Path.home()/'Library/Application Support/Bloom Dashboard/history.sqlite3').as_uri()+'?mode=ro',uri=True)",
        ]
        for action in actions:
            with self.subTest(action=action):
                code = (
                    'import subprocess,socket,sqlite3\nfrom pathlib import Path\nfrom replay_optimizer import install_isolation\ninstall_isolation()\ntry:\n '
                    + action
                    + "\nexcept RuntimeError as e:\n assert str(e).startswith('Replay isolation rejected'),str(e)\nelse:\n raise AssertionError('Live operation escaped the replay guard')"
                )
                result = subprocess.run(
                    [sys.executable, '-B', '-c', code],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
