import json
import subprocess
import unittest
from types import SimpleNamespace
from optimizer import Optimizer

ROWS = {'models': [{'id': 'gemma-4-26b-qat-4bit'}, {'id': 'gpt-oss-20b'}]}


class LocalModelsTests(unittest.TestCase):
    def fake(self, accepts_all=True, fail=None):
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            if fail:
                raise subprocess.CalledProcessError(1, args, '', fail)
            if '--all' in args and not accepts_all:
                raise subprocess.CalledProcessError(64, args, '', "Error: Unknown option '--all'")
            return SimpleNamespace(stdout=json.dumps(ROWS))

        return SimpleNamespace(runner=runner, binary='darkbloom', list_all=None), calls

    def test_lists_every_downloaded_model_with_all(self):
        o, calls = self.fake()
        self.assertEqual(Optimizer.local_models(o, ['--config', 'p.toml']), ROWS['models'])
        self.assertEqual(calls, [['darkbloom', 'models', 'list', '--json', '--all', '--config', 'p.toml']])
        self.assertIs(o.list_all, True)

    def test_older_cli_without_all_falls_back_once_and_remembers(self):
        o, calls = self.fake(accepts_all=False)
        self.assertEqual(Optimizer.local_models(o), ROWS['models'])
        self.assertIs(o.list_all, False)
        Optimizer.local_models(o)
        self.assertEqual([c[4:] for c in calls], [['--all'], [], []])

    def test_other_failures_are_raised_without_giving_up_on_all(self):
        o, _ = self.fake(fail='coordinator unreachable')
        with self.assertRaises(subprocess.CalledProcessError):
            Optimizer.local_models(o)
        self.assertIsNone(o.list_all)


if __name__ == '__main__':
    unittest.main()
