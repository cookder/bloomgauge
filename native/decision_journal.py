"""Background decision progress. Reads never advance confirmation or hold time."""

import json
import re


CODES = (
    (
        'protected',
        ('preserving productive', 'keeping the current model', 'earning at least', 'protect level'),
    ),
    ('memory', ('memory', 'cache')),
    ('temperature', ('hot.', 'temperatures settle', 'thermal')),
    ('power', ('battery', 'ac power')),
    ('identity', ('identity', 'roster', 'account or device', 'launch settings')),
    ('readiness', ('warm readiness', 'pre-warming', 'model is not ready', 'provider is offline')),
    (
        'network',
        (
            'network observations',
            'demand is stale',
            'sustained load',
            'no measured demand',
            'two five-minute',
        ),
    ),
    ('daily_limit', ('24-hour', 'downtime budget')),
    ('retry', ('retry', 'failed to load')),
    ('confirmation', ('confirming ', 'checking that the better', 'confirming persistence')),
    ('minimum_run', ('minimum run',)),
    ('trial', ('measuring the current trial', 'measuring current trial')),
    (
        'earnings_evidence',
        (
            'paid history',
            'covered',
            'earnings before',
            'measured earnings',
            'learning this session',
        ),
    ),
    ('freshness', ('fresh', 'readings', 'could not evaluate', 'could not be verified')),
    (
        'selection',
        ('not selected', 'no alternative model', 'no longer eligible', 'not locally available'),
    ),
    ('gain', ('gain', 'improvement', 'opportunity changed')),
)
# Words match at word starts ('gain' is not 'again'), and the first sentence, which
# states the main reason, is classified before the details that follow it.
PATTERNS = [
    (code, re.compile('|'.join(r'\b' + re.escape(w) for w in words))) for code, words in CODES
]


def reason_code(reason):
    text = reason.lower()
    first = re.split(r'(?<=[.;])\s', text, maxsplit=1)[0]
    for part in (first, text):
        for code, pattern in PATTERNS:
            if pattern.search(part):
                return code
    return 'observation'


class DecisionJournal:
    def __init__(self, history):
        self.h = history
        with self.h.lock:
            self.h.db.executescript("""CREATE TABLE IF NOT EXISTS optimizer_decisions(
                id INTEGER PRIMARY KEY,account TEXT,device TEXT,at REAL,updated REAL,
                observed_seconds REAL,mode TEXT,model TEXT,target TEXT,phase TEXT,
                code TEXT,reason TEXT,payload TEXT);
                CREATE INDEX IF NOT EXISTS optimizer_decision_scope ON optimizer_decisions(account,device,updated);""")
            self.h.db.commit()

    def record(
        self,
        account,
        device,
        now,
        mode,
        model,
        target,
        phase,
        reason,
        decision=None,
        confirmation=None,
    ):
        if not account or not device:
            return
        code = (
            phase if phase in ('off', 'switching', 'completed', 'failed') else reason_code(reason)
        )
        decision = decision or {}
        source = (
            decision.get('sourceAt')
            or max(
                (
                    r.get('signal', {}).get('observedAt') or 0
                    for r in decision.get('opportunities', [])
                ),
                default=0,
            )
            or None
        )
        payload = {
            'sourceAt': source,
            'confirmationSeconds': (confirmation or {}).get('seconds', 0),
            'requiredSeconds': (confirmation or {}).get(
                'requiredSeconds', decision.get('policy', {}).get('confirmationMinutes', 0) * 60
            ),
        }
        encoded = json.dumps(payload, allow_nan=False)
        with self.h.lock:
            old = self.h.db.execute(
                'SELECT * FROM optimizer_decisions WHERE account=? AND device=? ORDER BY id DESC LIMIT 1',
                (account, device),
            ).fetchone()
            # A slightly older write (out of order) is ignored. A backward clock change
            # starts a new row rather than ignoring every decision until the clock catches up.
            if old and 0 < old['updated'] - now < 300:
                return
            same = (
                old
                and 0 <= now - old['updated'] <= 150
                and all(
                    old[k] == v
                    for k, v in (
                        ('mode', mode),
                        ('model', model),
                        ('target', target),
                        ('phase', phase),
                        ('code', code),
                        ('reason', reason),
                    )
                )
            )
            if same:
                self.h.db.execute(
                    'UPDATE optimizer_decisions SET updated=?,observed_seconds=observed_seconds+?,reason=?,payload=? WHERE id=?',
                    (now, now - old['updated'], reason, encoded, old['id']),
                )
            else:
                self.h.db.execute(
                    'INSERT INTO optimizer_decisions(account,device,at,updated,observed_seconds,mode,model,target,phase,code,reason,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (
                        account,
                        device,
                        now,
                        now,
                        0,
                        mode,
                        model,
                        target,
                        phase,
                        code,
                        reason,
                        encoded,
                    ),
                )
            self.h.db.commit()

    def snapshot(self, account, device, now):
        start = now - 86400
        with self.h.lock:
            rows = self.h.db.execute(
                """SELECT id,at,updated,observed_seconds,mode,model,target,phase,code,reason,payload
                FROM optimizer_decisions WHERE account=? AND device=? AND updated>=? AND at<=? ORDER BY id DESC LIMIT 30""",
                (account, device, start, now),
            ).fetchall()
            # Display history is bounded; totals still cover every observed
            # interval in the scoped day, including frequent reason changes.
            totals = self.h.db.execute(
                """SELECT code,
                SUM(MIN(observed_seconds,MAX(0,MIN(updated,?)-MAX(at,?)))) AS seconds
                FROM optimizer_decisions WHERE account=? AND device=? AND updated>=? AND at<=?
                AND phase IN ('waiting','confirming','watching') GROUP BY code""",
                (now, start, account, device, start, now),
            ).fetchall()
        history = []
        for r in rows:
            item = {
                **{
                    k: r[k]
                    for k in (
                        'id',
                        'at',
                        'updated',
                        'mode',
                        'model',
                        'target',
                        'phase',
                        'code',
                        'reason',
                    )
                },
                'observedSeconds': min(
                    r['observed_seconds'], max(0, min(r['updated'], now) - max(r['at'], start))
                ),
                **json.loads(r['payload']),
            }
            history.append(item)
        current = history[0] if history else None
        return {
            'at': now,
            'current': current,
            'fresh': bool(current and 0 <= now - current['updated'] <= 90),
            'history': history,
            'totals': [dict(r) for r in sorted(totals, key=lambda r: (-r['seconds'], r['code']))],
            'windowHours': 24,
            'interruptBusy': True,
        }
