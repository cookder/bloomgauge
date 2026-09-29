"""Development tool, not bundled: compare the shadow pressure curves with the
matched estimator on history, or print the live shadow scores.

Always run it on a copy, never the live database:
  sqlite3 "file:$HOME/Library/Application Support/Bloom Dashboard/history.sqlite3?mode=ro" ".backup /tmp/bloom.sqlite3"
  python3 native/backtest_curves.py /tmp/bloom.sqlite3 --days 16      # walk-forward backtest
  python3 native/backtest_curves.py /tmp/bloom.sqlite3 --live          # the journal's own scores

The backtest rebuilds each half-hour checkpoint from data available then:
five-minute network signals, the session start of the running model, and
the realized steady pay of every model that ran 20+ minutes in the next 30.
"""

import argparse
import collections
import json
import time
from datetime import datetime, timezone
from history import History
from optimizer_store import OptimizerStore
from demand_baselines import network_minutes, conditional
from demand_optimizer import estimate
from demand_curve_journal import CurveJournal, MIN_SCORED_MINUTES
import demand_curves as curves


def signal_at(samples, t):
    rows = [r for r in samples if t - 300 <= r[0] < t]
    warm = [r for r in rows if r[3] >= 1]
    if not rows:
        return None
    mean = lambda f: sum(f(r) for r in rows) / len(rows)
    return {
        'observedAt': rows[-1][0],
        'coverage': len(rows) / 10,
        'status': 'normal',
        'active': mean(lambda r: r[1]),
        'queued': mean(lambda r: r[2]),
        'load': mean(lambda r: r[1] + r[2]),
        'warmProviders': mean(lambda r: r[3]),
        'pressure': sum((r[1] + r[2]) / r[3] for r in warm) / len(warm) if warm else None,
    }


def backtest(store, days, shared=None, since=None):
    db = store.h.db
    account, device = db.execute("""SELECT account,device FROM opt_ready_minutes
        GROUP BY 1,2 ORDER BY COUNT(*) DESC LIMIT 1""").fetchone()
    end = db.execute('SELECT MAX(at) FROM opt_ready_minutes').fetchone()[0]
    start = end - days * 86400
    earned = store.evidence(account, device, start - curves.LOOKBACK_SECONDS, end + 60, end + 400)
    if since:
        # Simulate a Mac new to BloomGauge at `since`: its own paid history before then is hidden.
        for row in earned.values():
            row['minutes'] = [m for m in row['minutes'] if m['at'] >= since]
    models = sorted(m for m in earned if len(curves.members(m)) == 1)
    network_models = [r[0] for r in db.execute('SELECT DISTINCT model FROM opt_network')]
    network = network_minutes(store, network_models, start - curves.LOOKBACK_SECONDS, end)
    samples = collections.defaultdict(list)
    for at, m, a, q, w in db.execute(
        'SELECT at,model,active,queued,warm FROM opt_network WHERE at>=? ORDER BY at',
        (start - 400,),
    ):
        samples[m].append((at, a, q, w))
    ready = {
        int(at): m
        for at, m in db.execute(
            """SELECT at,model FROM opt_ready_minutes
        WHERE account=? AND device=? AND at>=? AND seconds>0""",
            (account, device, start - 86400),
        )
    }

    def session_start(m, t):
        s = int(t // 60) * 60 - 60
        if ready.get(s) != m:
            return None
        while ready.get(s - 60) == m or ready.get(s - 120) == m:
            s -= 60
        return s

    rows = []
    t = int(start // 1800) * 1800 + 1800
    while t + 1800 + 120 <= end:
        current = ready.get(t - 60) or ready.get(t - 120)
        runs = {}
        for m in models:
            rate, n = curves.steady_rate(earned[m]['minutes'], t, t + 1800)
            if n >= MIN_SCORED_MINUTES:
                runs[m] = rate
        if runs:
            signals = {
                m: s
                for m in set(network_models) | set(models)
                for s in [signal_at(samples.get(m, []), t)]
                if s
            }
            predicted = curves.build(earned, network, signals, current, t, shared)
            for m, actual in runs.items():
                sig = signals.get(m, {})
                matched = estimate(
                    earned[m],
                    network.get(m, {}),
                    sig,
                    t,
                    session_start(m, t) if m == current else None,
                    conditional(earned[m], network.get(m, {}), sig, t),
                )
                row = predicted.get(m) or {}
                rows.append(
                    {
                        'model': m,
                        'at': t,
                        'actual': actual,
                        'serving': m == current,
                        'curve': row.get('usdPerHour'),
                        'lower': row.get('lower'),
                        'upper': row.get('upper'),
                        'matched': matched['rate'] if matched else None,
                    }
                )
        t += 1800
    return rows


def summarize(rows):
    def line(label, part):
        paired = [r for r in part if r['curve'] is not None and r['matched'] is not None]
        if not paired:
            return f'{label:32s}      -'
        mae = lambda k: sum(abs(r[k] - r['actual']) for r in paired) / len(paired)
        inside = sum(r['lower'] <= r['actual'] <= r['upper'] for r in paired) / len(paired)
        return f'{label:32s} {len(paired):5d}  curve {mae("curve"):.4f}  matched {mae("matched"):.4f}  in range {inside:.0%}'

    print(f'{"windows (MAE $/warm-hour)":32s} {"n":>5s}')
    print(line('all', rows))
    print(line('model already running', [r for r in rows if r['serving']]))
    print(line('model just switched to', [r for r in rows if not r['serving']]))
    for m in sorted({r['model'] for r in rows}):
        print(line('  ' + m[:30], [r for r in rows if r['model'] == m]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('database', help='a copy of history.sqlite3')
    parser.add_argument('--days', type=float, default=16)
    parser.add_argument(
        '--priors', help='shared-priors.json to test (build it with --until before the window)'
    )
    parser.add_argument(
        '--new-mac-since', help="YYYY-MM-DD: hide this Mac's own paid history before this UTC date"
    )
    parser.add_argument(
        '--live', action='store_true', help="print the shadow journal's own evaluation"
    )
    args = parser.parse_args()
    store = OptimizerStore(History(args.database))
    if args.live:
        row = store.h.db.execute(
            'SELECT account,device FROM demand_curve_observations ORDER BY checkpoint DESC LIMIT 1'
        ).fetchone()
        if not row:
            raise SystemExit('No shadow predictions recorded yet.')
        print(
            json.dumps(
                CurveJournal(store, enabled=True).evaluation(row[0], row[1], time.time()), indent=1
            )
        )
    else:
        import shared_priors

        summarize(
            backtest(
                store,
                args.days,
                shared_priors.load(args.priors) if args.priors else None,
                datetime.strptime(args.new_mac_since, '%Y-%m-%d')
                .replace(tzinfo=timezone.utc)
                .timestamp()
                if args.new_mac_since
                else None,
            )
        )


if __name__ == '__main__':
    main()
