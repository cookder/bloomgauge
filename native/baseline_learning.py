"""Bounded payment-sample discovery. Samples describe jobs, not promised income."""

from collections import defaultdict
from workload import aggregate, credit_measurements

LOOKBACK = 7 * 86400
FRESH_FOR = 48 * 3600
# Learning time (sampling minutes) limits how many runs happen; this spacing
# only stops one model from using it all.
REPEAT_SECONDS = 4 * 3600
SAMPLE_GOAL = 50
MIN_JOB_MINUTES = 10


def measured_jobs(store, account, device, start, end, now, model=None):
    """Exact solo, covered, settled warm minutes; no legacy prompt invention."""
    end = min(end, now - 120)
    with store.h.lock:
        rows = store.h.db.execute(
            """SELECT c.at,c.model,c.micro_usd,c.tokens,
            t.prompt_tokens,t.output_tokens,t.id AS detail_id
            FROM opt_credits c JOIN workload_tokens t ON t.account=c.account AND t.id=c.id
            WHERE c.account=? AND c.at>=? AND c.at<? AND c.model!='base_reward'
              AND c.micro_usd>=0 AND (? IS NULL OR c.model=?)
              AND EXISTS(SELECT 1 FROM opt_identity i WHERE i.device=? AND i.provider=c.provider)
              AND EXISTS(SELECT 1 FROM opt_ready_minutes m WHERE m.account=c.account AND m.device=?
                AND m.model=c.model AND m.at=CAST(c.at/60 AS INTEGER)*60
                AND m.at>=? AND m.at+60<=? AND m.seconds BETWEEN 59.999999 AND 60.000001
                AND EXISTS(SELECT 1 FROM opt_coverage v WHERE v.account=m.account AND v.start<=m.at AND v.end>=m.at+60))
            ORDER BY c.at,c.id""",
            (account, start, end, model, model, device, device, start, end),
        ).fetchall()
    result = []
    for row in rows:
        item = credit_measurements(row)
        if item['prompt'] is not None and item['output'] is not None:
            result.append(item)
    return result


def sample_summary(rows):
    result = aggregate(rows)
    result['jobMinutes'] = len({int(r['at'] // 60) for r in rows})
    result['blocks'] = len({int(r['at'] // 1800) for r in rows})
    result['spanSeconds'] = rows[-1]['at'] - rows[0]['at'] if rows else 0
    return result


def baselines(store, account, device, models, now):
    groups = defaultdict(list)
    for row in measured_jobs(store, account, device, now - LOOKBACK, now, now):
        groups[row['model']].append(row)
    result = {}
    for model in models:
        sample = sample_summary(groups[model])
        jobs = sample['completeTokenSamples']
        minutes = sample['jobMinutes']
        diverse = sample['blocks'] >= 2 and sample['spanSeconds'] >= 1800
        fresh = (
            sample['lastMeasuredAt'] is not None
            and 0 <= now - sample['lastMeasuredAt'] <= FRESH_FOR
        )
        needs = jobs < 100 or minutes < 20 or not diverse or not fresh
        result[model] = {
            **sample,
            'needsSamples': needs,
            'fresh': fresh,
            'diverse': diverse,
            'reason': (
                'No complete prompt/output payment samples in the last seven days.'
                if not jobs
                else 'Refresh payment samples: the last complete job is over 48 hours old.'
                if not fresh
                else 'Build a broader payment baseline across paid minutes and separate periods.'
                if needs
                else 'Recent payment samples cover multiple periods; ordinary passive learning continues.'
            ),
            'priority': (1 - min(1, jobs / 100))
            + (1 - min(1, minutes / 20))
            + (not diverse)
            + (not fresh),
        }
    return result


def opportunity(evidence, signal, runs, model, now, enabled, limits=None):
    repeat = (limits or {}).get('repeatSeconds', REPEAT_SECONDS)
    recent = [
        r
        for r in runs
        if now - 86400 < r['at'] <= now
        and (r.get('decision') or {}).get('explorationTrigger') == 'baseline_learning'
    ]
    demand = signal.get('learningDemand') or {}
    reason = None
    if not enabled:
        reason = 'Baseline-learning trials are off.'
    elif not evidence:
        reason = 'Payment-sample coverage is still being read.'
    elif not evidence['needsSamples']:
        reason = evidence['reason']
    elif any(r['model'] == model and now - repeat < r['at'] for r in recent):
        reason = 'This model already had a baseline-learning trial in the last %d hours.' % (
            repeat // 3600
        )
    elif not demand.get('qualified'):
        reason = 'Too little demand to learn from: a learning run needs at least 10 requests on the network and about 0.3 per warm provider in both of the last two five-minute windows.'
    return {
        'qualified': reason is None,
        'reason': reason or evidence['reason'],
        'evidence': evidence,
        'demand': demand,
        'trialsUsed': len(recent),
        'sampleGoal': SAMPLE_GOAL,
        'jobMinutesGoal': MIN_JOB_MINUTES,
    }
