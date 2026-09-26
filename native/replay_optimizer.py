#!/usr/bin/env python3
"""Replay fixed optimizer evidence offline. Never issues real provider commands."""

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import traceback
import unittest
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent


def install_isolation():
    protected = [
        str((Path.home() / '.darkbloom').resolve()),
        str((Path.home() / 'Library/Application Support/Bloom Dashboard').resolve()),
    ]

    def audit(event, args):
        if event in (
            'subprocess.Popen',
            'os.system',
            'os.posix_spawn',
            'os.posix_spawnp',
            'socket.connect',
            'socket.connect_ex',
            'socket.bind',
            'socket.sendto',
            'socket.getaddrinfo',
        ):
            raise RuntimeError('Replay isolation rejected process/network access')
        if (
            event in ('open', 'sqlite3.connect')
            and args
            and isinstance(args[0], (str, bytes, Path))
        ):
            name = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            if event == 'sqlite3.connect' and name.startswith('file:'):
                name = unquote(urlparse(name).path)
            path = str(Path(name).resolve())
            if any(path == prefix or path.startswith(prefix + '/') for prefix in protected):
                raise RuntimeError('Replay isolation rejected live provider/history access')

    sys.addaudithook(audit)


def at_pointer(value, path):
    for key in path.lstrip('/').split('/'):
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def execute(case):
    from demand_optimizer import decide, policy
    from demand_targets import target_status, fresh_paid

    value = copy.deepcopy(case['inputs'])
    if case['kind'] == 'decision':
        rules = policy(value.pop('policy', None))
        target = value.pop('target_context', None)
        if target:
            goal = target_status(
                target['evidence'],
                target['since'],
                value['now'],
                rules,
                target.get('last_switch', 0),
                target.get('live_paid'),
            )
            next(row for row in value['rows'] if row['id'] == value['current'])[
                'earningsTarget'
            ] = goal
        return decide(rules=rules, **value)
    if case['kind'] == 'fresh_paid':
        return fresh_paid(**value)
    if case['kind'] == 'controller_test':
        from test_demand_controller import DemandControllerTests

        allowed = {
            'test_failed_load_uses_guarded_recovery_then_pauses',
            'test_busy_final_recheck_dispatches_qualified_demand_switch',
            'test_user_pause_before_final_command_wins',
            'test_restart_never_reuses_old_confirmation',
        }
        if value.get('test') not in allowed:
            raise ValueError('Unknown controller scenario')
        output = io.StringIO()
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(
            unittest.TestSuite([DemandControllerTests(value['test'])])
        )
        if result.testsRun != 1:
            raise ValueError('Controller scenario did not run')
        return {
            'successful': result.wasSuccessful(),
            'testCount': result.testsRun,
            'detail': output.getvalue()
            if not result.wasSuccessful()
            else 'Isolated controller scenario passed',
        }
    raise ValueError('Unsupported replay kind')


def run(fixtures, comparison=None):
    install_isolation()
    files = sorted(fixtures.glob('*.json'))
    if not files:
        raise ValueError('No fixtures; an empty replay cannot pass')
    cases = []
    seen = set()
    for path in files:
        case = json.loads(path.read_text())
        if (
            case.get('schema') != 1
            or not case.get('expected')
            or not case.get('provenance')
            or case.get('id') in seen
        ):
            raise ValueError('Malformed, duplicate or unasserted fixture: ' + path.name)
        seen.add(case['id'])
        row = {
            'id': case['id'],
            'title': case['title'],
            'kind': case['kind'],
            'provenance': case['provenance'],
            'fixtureSha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        try:
            value = execute(case)
            observed = {pointer: at_pointer(value, pointer) for pointer in case['expected']}
            row.update(
                passed=observed == case['expected'], expected=case['expected'], observed=observed
            )
            if 'reason' in value:
                row['reason'] = value['reason']
            if case['kind'] == 'controller_test' and not value['successful']:
                row['detail'] = value['detail']
        except Exception:
            row.update(passed=False, error=traceback.format_exc())
        cases.append(row)
    previous = {row['id']: row for row in (comparison or {}).get('cases', [])}
    changes = []
    for row in cases:
        old = previous.get(row['id'])
        if old and (
            old.get('observed') != row.get('observed') or old.get('passed') != row['passed']
        ):
            changes.append(
                {
                    'id': row['id'],
                    'previous': old.get('observed'),
                    'current': row.get('observed'),
                    'passed': row['passed'],
                }
            )
    return {
        'schema': 'bloom-optimizer-replay-v1',
        'at': datetime.now(timezone.utc).isoformat(),
        'passed': all(row['passed'] for row in cases),
        'caseCount': len(cases),
        'cases': cases,
        'changedOutcomes': changes,
        'comparisonProvided': comparison is not None,
        'sourceHashes': {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                'demand_optimizer.py',
                'demand_targets.py',
                'demand_spikes.py',
                'demand_trials.py',
                'optimizer.py',
            )
        },
        'isolation': 'No live provider/history reads, process launches or network connections allowed.',
        'limits': [
            'Two historical incidents have explicitly partial reconstructed context. Other cases are labeled synthetic or isolated controller scenarios.',
            'No counterfactual earnings, optimality or causal ROI estimate. Passing guards does not prove future earnings.',
        ],
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path, default=ROOT / 'replay/fixtures')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compare', type=Path)
    args = parser.parse_args()
    baseline = json.loads(args.compare.read_text()) if args.compare else None
    if baseline and baseline.get('schema') != 'bloom-optimizer-replay-v1':
        raise SystemExit('Comparison must be a replay report')
    result = run(args.fixtures, baseline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    print(
        json.dumps(
            {
                'passed': result['passed'],
                'caseCount': result['caseCount'],
                'failed': [r['id'] for r in result['cases'] if not r['passed']],
                'changedOutcomes': len(result['changedOutcomes']),
                'report': str(args.output),
            }
        )
    )
    sys.exit(0 if result['passed'] else 1)
