"""Darkbloom 0.9.11 compatibility (released Sep 27, 2026; provider commit 2b714c4).

0.9.11 keeps the 0.9.10 daemon-state.json schema, CLI and LaunchAgent layout. It drops
the legacy provider.toml locations: `ConfigManager.defaultConfigPath` is now only
`~/.config/darkbloom/provider.toml` (or `--config`), with no copy from the old paths.
0.9.9 and 0.9.10 keep the fallback.
"""

import copy, json, pathlib, tempfile, unittest
import manager
from model_combinations import (
    combination_config_error,
    configured_reserve_gb,
    default_config_path,
    provider_version,
)
from optimizer import activity_counters, drained_idle, graceful_drain
from provider_reporting import preloading, state_fresh

GEMMA = 'gemma-4-26b-qat-4bit'
QWEN = 'Qwen3.5-9B'
NOW = 1_790_000_000.0


def daemon_state(version='0.9.11', **changes):
    """daemon-state.json as DaemonStateFile writes it (snake_case keys), serving GEMMA."""
    state = {
        'schema': 1,
        'pid': 4242,
        'process_identity': {'pid': 4242, 'start_time_micros': 1789996400000000},
        'version': version,
        'written_at': NOW - 2,
        'started_at': NOW - 3600,
        'attestation_public_key': 'fixture-key',
        'trust': {
            'trust_level': 'hardware',
            'status': 'online',
            'reason': 'fixture',
            'received_at': NOW - 3500,
        },
        'coordinator_url': 'wss://api.darkbloom.dev/ws/provider',
        'current_model': GEMMA,
        'warm_models': [GEMMA],
        'advertised_models': [GEMMA],
        'startup_preload_pending_models': [],
        'lifecycle': {'outcome': 'serving', 'remaining': 0, 'coordinator_acknowledged': True},
        'config_path': '/Users/fixture/.config/darkbloom/provider.toml',
        'runtime_capabilities': ['mlx_nax'],
        'inference_active': False,
        'stats': {'requests_served': 12, 'tokens_generated': 3400, 'usage_gaps': 0},
        'system': {'memory_pressure': 0.2, 'cpu_usage': 0.1, 'thermal_state': 'nominal'},
        'capacity': {'total_memory_gb': 64, 'gpu_memory_active_gb': 18.5},
        'slots': [
            {
                'model': GEMMA,
                'kv_backend': 'contiguous',
                'kv_backend_requested': 'auto',
                'mtp_enabled': True,
                'mtp_active': True,
            }
        ],
        'connectivity': {'reconnect_count': 0},
    }
    state.update(copy.deepcopy(changes))
    return state


def toml_0911(models, idle=60, reserve=4, slots=1):
    """provider.toml as 0.9.11 saves it: no `config_version` stamp, `mtp_mode` only."""
    return (
        '[provider]\nname = "fixture"\nmemory_reserve_gb = %d\nauto_update = true\n\n'
        '[backend]\nport = 8100\nenabled_models = %s\nidle_timeout_mins = %d\n'
        'max_model_slots = %d\nengine_v2_max_concurrent = 4\nmtp_mode = "auto"\n\n'
        '[coordinator]\nurl = "wss://api.darkbloom.dev/ws/provider"\n'
        'heartbeat_interval_secs = 30\n' % (reserve, json.dumps(models), idle, slots)
    )


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)
        self.canonical = self.home / '.config/darkbloom/provider.toml'
        self.app = self.home / 'Library/Application Support/darkbloom/provider.toml'

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def provider(self, version):
        self.write(self.home / '.darkbloom/daemon-state.json', json.dumps(daemon_state(version)))


class ConfigPathTests(HomeCase):
    def test_0911_reads_only_the_canonical_provider_toml(self):
        # An old Application Support config that 0.9.10 would have loaded (and copied).
        self.write(self.app, toml_0911([QWEN], idle=30, reserve=8, slots=1))
        self.provider('0.9.11')
        self.assertEqual(default_config_path(self.home), self.canonical)
        self.assertEqual(manager.config_path(self.home, ['--local-endpoint']), self.canonical)
        # Darkbloom runs on defaults: no pinned selection, no idle choice, reserve 4 GB.
        self.assertIsNone(manager.toml_selection(self.home, []))
        self.assertFalse(manager.idle_unload_chosen(self.home, []))
        self.assertTrue(manager.idle_unload_chosen(self.home, ['--idle-timeout', '20']))
        self.assertEqual(configured_reserve_gb(self.home, []), 4)
        self.assertIsNone(combination_config_error(self.home, [], {}))

    def test_0911_canonical_file_is_read_as_before(self):
        self.write(self.app, toml_0911([QWEN], idle=30))
        self.write(self.canonical, toml_0911([GEMMA], idle=60, reserve=6, slots=2))
        for version in ('0.9.9', '0.9.10', '0.9.11', '0.9.12'):
            with self.subTest(version=version):
                self.provider(version)
                self.assertEqual(manager.config_path(self.home, []), self.canonical)
                self.assertEqual(manager.toml_selection(self.home, []), GEMMA)
                self.assertFalse(manager.idle_unload_chosen(self.home, []))
                self.assertEqual(configured_reserve_gb(self.home, []), 6)

    def test_older_providers_keep_the_legacy_fallback(self):
        self.write(self.app, toml_0911([QWEN], idle=30, reserve=8, slots=1))
        for version in ('0.9.9', '0.9.10'):
            with self.subTest(version=version):
                self.provider(version)
                self.assertEqual(manager.config_path(self.home, []), self.app)
                self.assertEqual(manager.toml_selection(self.home, []), QWEN)
                self.assertTrue(manager.idle_unload_chosen(self.home, []))
                self.assertEqual(configured_reserve_gb(self.home, []), 8)
                self.assertIsNotNone(combination_config_error(self.home, [], {}))

    def test_an_unknown_version_keeps_the_legacy_fallback(self):
        self.write(self.app, toml_0911([QWEN]))
        state = self.home / '.darkbloom/daemon-state.json'
        for text in (None, '', '{', '[]', '{"version": 11}', '{"version": "v0.9.11"}', '{}'):
            with self.subTest(text=text):
                if text is not None:
                    self.write(state, text)
                self.assertIsNone(provider_version(self.home))
                self.assertEqual(manager.config_path(self.home, []), self.app)

    def test_a_custom_config_is_used_on_every_version(self):
        custom = self.home / 'custom.toml'
        self.write(custom, toml_0911([QWEN]))
        self.write(self.canonical, toml_0911([GEMMA]))
        self.provider('0.9.11')
        self.assertEqual(manager.config_path(self.home, ['--config', str(custom)]), custom)
        self.assertEqual(manager.toml_selection(self.home, ['-c', str(custom)]), QWEN)

    def test_version_parsing(self):
        for version, expected in (
            ('0.9.11', (0, 9, 11)),
            ('0.9.9', (0, 9, 9)),
            ('0.10.0', (0, 10, 0)),
            ('0.9.11-rc.1', (0, 9, 11)),
        ):
            with self.subTest(version=version):
                self.provider(version)
                self.assertEqual(provider_version(self.home), expected)


class DaemonStateTests(unittest.TestCase):
    """0.9.11 writes the 0.9.10 schema; the version gates read 0.9.11 as newer than 0.9.9."""

    def test_version_gates(self):
        self.assertTrue(graceful_drain(daemon_state('0.9.11')))
        self.assertTrue(graceful_drain(daemon_state('0.9.10')))
        self.assertTrue(graceful_drain(daemon_state('0.9.9')))
        self.assertFalse(graceful_drain(daemon_state('0.9.8')))

    def test_liveness_preload_drain_and_counters(self):
        raw = daemon_state()
        self.assertTrue(state_fresh(raw, NOW))
        self.assertFalse(state_fresh(raw, NOW + 20))
        self.assertFalse(preloading(raw))
        loading = daemon_state(
            startup_preload_pending_models=[GEMMA], warm_models=[], written_at=NOW - 30
        )
        self.assertTrue(preloading(loading))
        self.assertTrue(state_fresh(loading, NOW))
        self.assertEqual(activity_counters(raw), (12, 3400))
        self.assertFalse(drained_idle(raw))
        drained = daemon_state(
            lifecycle={'outcome': 'drained', 'remaining': 0, 'coordinator_acknowledged': True},
            warm_models=[],
        )
        self.assertTrue(drained_idle(drained))

    def test_next_release_fields_change_nothing(self):
        """main after 0.9.11 (#1206) adds optional load-readiness fields."""
        raw = daemon_state('0.9.12')
        extended = daemon_state(
            '0.9.12',
            request_work_pending=False,
            load_transition_active=False,
            capacity={
                'total_memory_gb': 64,
                'gpu_memory_active_gb': 18.5,
                'load_usable_gb': 30.2,
                'load_headroom_gb': 5.5,
                'free_for_load_gb': 26.0,
                'load_transition_active': False,
            },
        )
        for check in (graceful_drain, drained_idle, activity_counters, preloading):
            self.assertEqual(check(extended), check(raw))
        self.assertEqual(state_fresh(extended, NOW), state_fresh(raw, NOW))


if __name__ == '__main__':
    unittest.main()
