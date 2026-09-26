"""Read saved pulse observations across this account/device's model sessions.

Session boundaries survive aggregation. No rates are reconstructed, no device
identifiers are returned, and the existing current-session routes stay valid.
"""

import json
import math


def model_history(history, account, session, start, end, table, read_session):
    if table not in ('pulse_rates', 'traffic_intervals'):
        raise ValueError('Unknown pulse history')
    minimum = 3 if table == 'traffic_intervals' else 1
    result = {
        'scope': 'models',
        'sessionId': session,
        'sessions': [],
        'samples': [],
        'coverageStart': None,
        'coverageEnd': None,
        'bucketSeconds': minimum,
        'count': 0,
    }
    if not account or type(session) is not int:
        return result
    first, key = ('start', 'end') if table == 'traffic_intervals' else ('at', 'at')
    with history.lock:
        # Scope is the provider-session hash of account + device identity. The
        # session is supplied by the collector, never selected by the client.
        anchor = history.db.execute(
            'SELECT scope FROM provider_sessions WHERE id=?', (session,)
        ).fetchone()
        if not anchor or not anchor['scope']:
            return result
        # Per-session index seeks on the (account, session, time) primary key. A
        # grouped MIN/MAX over the join scanned every row for the account (seconds
        # on a month of per-second data) while the caller held the history lock.
        records = history.db.execute(
            f"""SELECT * FROM (SELECT s.id,s.data,
            (SELECT {first} FROM {table} WHERE account=? AND session=s.id ORDER BY {key} LIMIT 1) AS first,
            (SELECT {key} FROM {table} WHERE account=? AND session=s.id ORDER BY {key} DESC LIMIT 1) AS last
            FROM provider_sessions s WHERE s.scope=?) WHERE first IS NOT NULL ORDER BY first,id""",
            (account, account, anchor['scope']),
        ).fetchall()
        if not records:
            return result
        result['coverageStart'] = min(r['first'] for r in records)
        result['coverageEnd'] = max(r['last'] for r in records)
        low, high = max(start, result['coverageStart']), min(end, result['coverageEnd'])
        # One resolution budget for the full chart, rather than 600 points for
        # every old session. A session is never averaged with a different one.
        step = max(minimum, math.ceil(max(0, high - low) / 600))
        result['bucketSeconds'] = step
        for record in records:
            if record['last'] < start or record['first'] > end:
                continue
            try:
                data = json.loads(record['data'])
                models = data.get('models')
                if (
                    not isinstance(models, list)
                    or not models
                    or any(not isinstance(m, str) or not m or len(m) > 512 for m in models)
                ):
                    models = []
            except (ValueError, TypeError, AttributeError):
                models = []
            saved = read_session(record['id'], step)
            result['count'] += saved['count']
            result['sessions'].append(
                {
                    'id': record['id'],
                    'models': sorted(set(models)),
                    'firstAt': record['first'],
                    'lastAt': record['last'],
                }
            )
            result['samples'].extend(
                {**point, 'sessionId': record['id']} for point in saved['samples']
            )
        result['samples'].sort(key=lambda point: (point['at'], point['sessionId']))
    return result
