"""Development tool, not bundled: build shared-priors.json, the starting curves
the shadow estimator uses for models a Mac hasn't run much.

Run it on a copy of the history database, never the live file:
  sqlite3 "file:$HOME/Library/Application Support/Bloom Dashboard/history.sqlite3?mode=ro" ".backup /tmp/bloom.sqlite3"
  python3 native/build_priors.py /tmp/bloom.sqlite3 --out native/shared-priors.json
  # add opt-in summaries downloaded from bloomformac.com/owner:
  python3 native/build_priors.py /tmp/bloom.sqlite3 --summaries ~/Downloads/pay-summaries.json --out native/shared-priors.json

--until builds from history before a date only (for an honest backtest).
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

import demand_curves as curves
from demand_baselines import network_minutes
from history import History
from optimizer_store import OptimizerStore

MIN_HOURS = 2
MIN_PERIODS = 4
# Opt-in summaries are unauthenticated, so one sender must not be able to steer a
# curve: each counts for at most a day of warm time, the combined value is a
# weighted median, and a model nobody here runs needs several Macs behind it.
SUMMARY_HOURS_CAP = 24
MIN_SUMMARY_MACS = 3
MAX_LEVEL = 5.0


def local_curves(store, until):
    db = store.h.db
    account, device = db.execute("""SELECT account,device FROM opt_ready_minutes
        GROUP BY 1,2 ORDER BY COUNT(*) DESC LIMIT 1""").fetchone()
    earned = store.evidence(account, device, until - curves.LOOKBACK_SECONDS, until, until)
    models = sorted(m for m in earned if len(curves.members(m)) == 1)
    network = network_minutes(store, models, until - curves.LOOKBACK_SECONDS, until)
    return {
        m: {**c, 'macs': 1, 'hardware': [hardware()]}
        for m, c in curves.summary(earned, network, until, MIN_HOURS, MIN_PERIODS).items()
    }


def weighted_median(pairs):
    pairs = sorted(pairs)
    total, running = sum(w for _, w in pairs), 0
    for value, weight in pairs:
        running += weight
        if running >= total / 2:
            return value
    return pairs[-1][0]


def valid_curve(c):
    finite = lambda v: type(v) in (int, float) and v == v and abs(v) != float('inf')
    rng = c.get('range')
    return (
        isinstance(c, dict)
        and finite(c.get('level'))
        and 0 < c['level'] <= MAX_LEVEL
        and finite(c.get('exponent'))
        and 0 <= c['exponent'] <= 2
        and finite(c.get('hours'))
        and 0 < c['hours'] <= 24 * 7
        and type(c.get('periods')) is int
        and c['periods'] >= 1
        and (
            rng is None
            or isinstance(rng, list)
            and len(rng) == 2
            and all(finite(x) and x >= 0 for x in rng)
            and rng[0] <= rng[1]
        )
    )


def clean(summaries, known):
    """Drop malformed curves and models Darkbloom has never listed; cap each Mac's weight."""
    out, dropped = [], []
    for s in summaries:
        kept = {}
        for m, c in s.items():
            if m in known and valid_curve(c):
                kept[m] = {**c, 'hours': min(c['hours'], SUMMARY_HOURS_CAP), 'optIn': True}
            else:
                dropped.append(m)
        out.append(kept)
    return out, dropped


def merge(base, summaries):
    """Warm-hour-weighted median of each model's level and exponent across Macs."""
    groups = {}
    for source in [base] + summaries:
        for m, c in source.items():
            groups.setdefault(m, []).append(c)
    out = {}
    for m, rows in groups.items():
        hours = sum(r['hours'] for r in rows)
        if hours < MIN_HOURS:
            continue
        if all(r.get('optIn') for r in rows) and len(rows) < MIN_SUMMARY_MACS:
            continue
        mean = lambda k: weighted_median([(r[k], r['hours']) for r in rows])
        ranges = [r['range'] for r in rows if r.get('range')]
        out[m] = {
            'level': round(mean('level'), 5),
            'exponent': round(mean('exponent'), 4),
            'range': [round(min(r[0] for r in ranges), 3), round(max(r[1] for r in ranges), 3)]
            if ranges
            else None,
            'hours': round(hours, 1),
            'periods': sum(r['periods'] for r in rows),
            'macs': len(rows),
            'hardware': sorted({h for r in rows for h in r.get('hardware', [])}),
        }
    return out


def by_hardware(base, summaries):
    """The same merge within each chip family and memory band, for Macs that match."""
    classes = {}
    for source in [base] + summaries:
        for h in {h for c in source.values() for h in c.get('hardware', [])}:
            classes.setdefault(h, []).append(
                {m: c for m, c in source.items() if h in c.get('hardware', [])}
            )
    return {h: merged for h, sources in sorted(classes.items()) if (merged := merge({}, sources))}


def compare(old, new):
    lines = []
    for m in sorted(set(old) | set(new)):
        a, b = old.get(m), new.get(m)
        if not a or not b:
            lines.append(f'{m}: {"added" if b else "removed"}')
            continue
        change = b['level'] / a['level'] - 1 if a['level'] else float('inf')
        if abs(change) >= 0.2 or abs(b['exponent'] - a['exponent']) >= 0.3:
            lines.append(
                f'{m}: level {a["level"]} -> {b["level"]} ({change:+.0%}), exponent {a["exponent"]} -> {b["exponent"]}, Macs {a.get("macs")} -> {b.get("macs")}'
            )
    return '\n'.join(lines) or 'No model moved by 20% or more.'


def hardware():
    """'chip family|memory band', matching usage_integration.metadata."""
    try:
        gb = (
            int(
                subprocess.run(
                    ['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True, timeout=5
                ).stdout
            )
            / 2**30
        )
    except (OSError, ValueError):
        gb = 0
    band = (
        next(
            (
                label
                for bound, label in (
                    (16, 'up-to-16'),
                    (32, '17-32'),
                    (64, '33-64'),
                    (128, '65-128'),
                )
                if gb <= bound
            ),
            'over-128',
        )
        if gb
        else 'unknown'
    )
    return chip() + '|' + band


def chip():
    try:
        return (
            subprocess.run(
                ['sysctl', '-n', 'machdep.cpu.brand_string'],
                capture_output=True,
                text=True,
                timeout=5,
            )
            .stdout.strip()
            .removeprefix('Apple ')
        )
    except OSError:
        return 'unknown'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('database', help='a copy of history.sqlite3')
    parser.add_argument('--until', help='YYYY-MM-DD: use history before this UTC date only')
    parser.add_argument('--summaries', help='pay summaries JSON downloaded from /owner')
    parser.add_argument('--out', default='-')
    parser.add_argument(
        '--compare',
        help='existing shared-priors.json: print what changes, to review before shipping',
    )
    args = parser.parse_args()
    until = (
        datetime.strptime(args.until, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()
        if args.until
        else time.time()
    )
    store = OptimizerStore(History(args.database))
    base = local_curves(store, until)
    summaries = []
    if args.summaries:
        data = json.load(open(args.summaries))
        summaries = [
            {
                m: {**c, 'macs': 1, 'hardware': [s['chipFamily'] + '|' + s['memoryBand']]}
                for m, c in s['models'].items()
                if isinstance(c, dict)
            }
            for s in data.get('summaries', [])
            if isinstance(s, dict)
            and isinstance(s.get('models'), dict)
            and isinstance(s.get('chipFamily'), str)
            and isinstance(s.get('memoryBand'), str)
        ]
        known = set(base) | {
            r[0] for r in store.h.db.execute('SELECT DISTINCT model FROM opt_network')
        }
        summaries, dropped = clean(summaries, known)
        if dropped:
            print(
                f'Dropped {len(dropped)} unknown or malformed curves: {sorted(set(dropped))[:20]}',
                file=sys.stderr,
            )
    models = merge(base, summaries)
    result = {
        'schema': 1,
        'method': curves.METHOD_VERSION,
        'builtAt': datetime.fromtimestamp(until, timezone.utc).strftime('%Y-%m-%d'),
        'sources': {'andrew': {'chip': chip(), 'models': len(base)}, 'optInMacs': len(summaries)},
        'models': dict(sorted(models.items())),
        'byHardware': by_hardware(base, summaries),
    }
    if args.compare:
        print(
            compare(json.load(open(args.compare)).get('models', {}), result['models']),
            file=sys.stderr,
        )
    text = json.dumps(result, indent=1) + '\n'
    if args.out == '-':
        print(text, end='')
    else:
        open(args.out, 'w').write(text)


if __name__ == '__main__':
    main()
