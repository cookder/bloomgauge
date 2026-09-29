"""Reading the provider's launch agent while Darkbloom rewrites it (flaky-CI regression)."""

import pathlib
import plistlib
import tempfile
import unittest

import threading
from types import SimpleNamespace

from optimizer import Optimizer, read_launch_agent

AGENT = {'Label': 'io.darkbloom.provider', 'ProgramArguments': ['/bin/darkbloom', 'serve', '--model', 'a']}


class LaunchAgentReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / 'agent.plist'
        self.full = plistlib.dumps(AGENT)

    def read_while_written(self, stages):
        """Each wait lets the writer advance one stage, as Darkbloom's in-place rewrite would."""
        stages = list(stages)
        self.path.write_bytes(stages.pop(0))
        waits = []

        def wait(seconds):
            waits.append(seconds)
            if stages:
                self.path.write_bytes(stages.pop(0))

        return read_launch_agent(self.path, wait), waits

    def test_half_written_then_complete_is_read(self):
        for partial in (b'', self.full[: len(self.full) // 2], b'<?xml version="1.0"'):
            with self.subTest(partial=partial[:20]):
                agent, waits = self.read_while_written([partial, self.full])
                self.assertEqual(agent, AGENT)
                self.assertEqual(len(waits), 1)

    def test_still_broken_after_retries_is_a_plain_value_error(self):
        broken = self.full[:40]
        with self.assertRaises(ValueError) as caught:
            self.read_while_written([broken, broken, broken, broken])
        self.assertIn('being rewritten', str(caught.exception))

    def test_complete_file_needs_no_wait(self):
        agent, waits = self.read_while_written([self.full])
        self.assertEqual((agent, waits), (AGENT, []))

    def test_missing_file_is_not_retried(self):
        with self.assertRaises(FileNotFoundError):
            read_launch_agent(self.path / 'missing', lambda s: self.fail('no retry'))


class DroppedEnvironmentNoteTests(unittest.TestCase):
    def note(self, state, passed, saved):
        saves = []
        o = SimpleNamespace(lock=threading.RLock(), state=state, save=lambda: saves.append(1))
        Optimizer.note_dropped_environment(o, passed, saved)
        return o.state, saves

    def test_dropped_keys_are_named_and_empty_values_ignored(self):
        state, saves = self.note({}, {'HF_HOME': '/x', 'DARKBLOOM_PREFIX_CACHE': '', 'KEEP': '1'}, {'KEEP': '1'})
        self.assertEqual(state['environmentDropped']['keys'], ['HF_HOME'])
        self.assertEqual(saves, [1])

    def test_a_clean_start_clears_an_old_note(self):
        state, saves = self.note({'environmentDropped': {'keys': ['HF_HOME'], 'at': 1}}, {'KEEP': '1'}, {'KEEP': '1'})
        self.assertNotIn('environmentDropped', state)
        self.assertEqual(saves, [1])
        state, saves = self.note({}, {}, {})
        self.assertEqual((state, saves), ({}, []))


if __name__ == '__main__':
    unittest.main()
