"""Durable reporting sessions, independent of chart pause or optimizer mode."""

import copy, ctypes, errno, hashlib, json, math, os, pathlib, threading, time
from provider_reporting import FRESH_SECONDS, PRELOAD_FRESH_SECONDS, loaded_models, preloading


def numeric(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def models(raw):
    values = raw.get('advertised_models')
    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(m, str) or not m or len(m) > 512 for m in values)
    ):
        return None
    return sorted(set(values))


def process_identity(raw):
    pid, started = raw.get('pid'), raw.get('started_at')
    if type(pid) is not int or pid <= 0 or not numeric(started):
        return None
    native = raw.get('process_identity') or {}
    micros = (
        native.get('start_time_micros')
        if isinstance(native, dict) and native.get('pid') == pid
        else None
    )
    return {
        'pid': pid,
        'startedAt': started,
        'startMicros': micros if type(micros) is int and micros > 0 else None,
    }


def same_process(a, b):
    return bool(
        a
        and b
        and a['pid'] == b['pid']
        and a['startedAt'] == b['startedAt']
        and (
            a.get('startMicros') is None
            or b.get('startMicros') is None
            or a['startMicros'] == b['startMicros']
        )
    )


class BSDInfo(ctypes.Structure):
    # Darwin proc_bsdinfo from the installed SDK's sys/proc_info.h.
    _fields_ = (
        [
            (name, ctypes.c_uint32)
            for name in (
                'flags',
                'status',
                'xstatus',
                'pid',
                'ppid',
                'uid',
                'gid',
                'ruid',
                'rgid',
                'svuid',
                'svgid',
                'reserved',
            )
        ]
        + [
            ('comm', ctypes.c_char * 16),
            ('name', ctypes.c_char * 32),
        ]
        + [(name, ctypes.c_uint32) for name in ('nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid')]
        + [
            ('nice', ctypes.c_int32),
            ('startSeconds', ctypes.c_uint64),
            ('startMicros', ctypes.c_uint64),
        ]
    )


def matching_process(identity):
    """True: matching process alive; False: ended/reused PID; None: unreadable."""
    if not identity:
        return None
    try:
        lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        call = lib.proc_pidinfo
        call.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        call.restype = ctypes.c_int
        info = BSDInfo()
        size = call(identity['pid'], 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if size == ctypes.sizeof(info):
            if info.status == 5 or info.flags & 4:  # zombie or exiting
                return False
            expected = identity.get('startMicros')
            return expected is None or info.startSeconds * 1000000 + info.startMicros == expected
        if ctypes.get_errno() == errno.ESRCH:
            return False
        # kill(0) reads existence only; it never signals/stops the provider.
        os.kill(identity['pid'], 0)
    except ProcessLookupError:
        return False
    except (OSError, AttributeError, ValueError):
        pass
    return None


COUNTERS = (
    'totalJobs',
    'successfulJobs',
    'failedJobs',
    'uptimeSeconds',
    'challengesPassed',
    'challengesFailed',
)
BASELINE_GROUPS = (
    ('totalJobs', 'successfulJobs', 'failedJobs'),
    ('uptimeSeconds',),
    ('challengesPassed', 'challengesFailed'),
)


class ProviderSessions:
    def __init__(self, history, home, probe=None):
        self.h, self.home = history, pathlib.Path(home)
        self.probe = probe or matching_process
        self.lock = threading.RLock()
        self.current, self.scope = None, None
        self.availability = 'unavailable'
        self.fresh_seconds = FRESH_SECONDS  # longer while Darkbloom preloads its models
        with self.h.lock:
            self.h.db.execute(
                'CREATE TABLE IF NOT EXISTS provider_sessions(id INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT,data TEXT)'
            )
            self.h.db.execute(
                'CREATE INDEX IF NOT EXISTS provider_session_scope ON provider_sessions(scope,id)'
            )
            self.h.db.execute(
                'CREATE TABLE IF NOT EXISTS session_ready_intervals(session INTEGER,start REAL,end REAL,PRIMARY KEY(session,start))'
            )
            self.h.db.commit()

    def save(self):
        with self.h.lock:
            self.h.db.execute(
                'UPDATE provider_sessions SET data=? WHERE id=?',
                (json.dumps(self.current), self.current['id']),
            )
            self.h.db.commit()

    def finish(self, now, reason):
        if self.current and self.current.get('endedAt') is None:
            self.current.update(endedAt=now, endReason=reason)
            self.save()

    def pause_performance(self):
        if self.current and self.current.get('_readyLast'):
            self.current['_readyLast'] = None
            self.save()

    def record_performance(self, counts, written, tracking):
        record = self.current
        p = record.setdefault(
            'performance',
            {
                'seconds': 0,
                'requests': None,
                'tokens': None,
                'since': None,
                'segmentStartedAt': None,
            },
        )
        previous = record.get('_readyLast')
        ready = bool(tracking and tracking.get('counting'))
        current = (
            {'at': written, 'counts': counts, 'proof': (tracking or {}).get('verifiedAt')}
            if ready
            else None
        )
        if ready:
            for key, value in counts.items():
                p.setdefault(key + 'Partial', False)
                if value is None:
                    p[key + 'Partial'] = True
                elif p[key] is None:
                    p[key] = 0
        continuous = bool(
            current
            and previous
            and current['proof'] == previous['proof']
            and 0 < written - previous['at'] <= 15
        )
        if continuous:
            p['seconds'] += written - previous['at']
            for key, value in counts.items():
                before = previous['counts'].get(key)
                if value is not None and before is not None and value >= before:
                    p[key] += value - before
                elif value is None or before is None:
                    p[key + 'Partial'] = True
            with self.h.lock:
                self.h.db.execute(
                    'INSERT INTO session_ready_intervals VALUES(?,?,?) ON CONFLICT(session,start) DO UPDATE SET end=excluded.end',
                    (record['id'], p['segmentStartedAt'], written),
                )
        elif ready and (
            not previous or previous['at'] != written or previous['proof'] != current['proof']
        ):
            p['segmentStartedAt'] = written
            if p['since'] is None:
                p['since'] = written
        p.update(
            status='counting' if ready else 'paused',
            detail=(tracking or {}).get(
                'detail', 'Statistics paused · model readiness is unverified.'
            ),
        )
        record['_readyLast'] = current

    def observe(self, account, raw, now, tracking=None):
        with self.lock:
            raw = raw if isinstance(raw, dict) else {}
            if not account:
                self.finish(now, 'identity_unavailable')
                self.current, self.scope, self.availability = None, None, 'unavailable'
                return None
            account_key = 'provider-session-last:' + digest(account)
            key = raw.get('attestation_public_key')
            scope = (
                digest([account, key])
                if isinstance(key, str) and key
                else self.h.cache(account_key)
            )
            if scope != self.scope:
                self.finish(now, 'identity_changed')
                self.scope, self.current = scope, None
                if scope:
                    with self.h.lock:
                        row = self.h.db.execute(
                            'SELECT data FROM provider_sessions WHERE scope=? ORDER BY id DESC LIMIT 1',
                            (scope,),
                        ).fetchone()
                    self.current = json.loads(row[0]) if row else None
                    if self.current:
                        self.current['_readyLast'] = (
                            None  # Never bridge time while Bloomkeeper was closed or another scope was active.
                        )
                    self.h.cache(account_key, scope)
            current = self.current
            identity = process_identity(raw)
            written, selected = raw.get('written_at'), models(raw)
            self.fresh_seconds = PRELOAD_FRESH_SECONDS if preloading(raw) else FRESH_SECONDS
            fresh = numeric(written) and -5 < now - written < self.fresh_seconds
            candidate = (
                identity
                if identity and selected and fresh and isinstance(key, str) and key
                else None
            )
            previous_process = (current or {}).get('_process')
            if candidate and current and written < current['lastSeenAt']:
                candidate = None  # Never roll back to an older model/process snapshot.
            if (
                candidate
                and same_process(previous_process, identity)
                and identity.get('startMicros') is None
            ):
                identity = candidate = dict(previous_process)
            live = self.probe(candidate or (current or {}).get('_process'))
            if live is False:
                self.pause_performance()
                self.finish(now, 'service_stopped')
                self.availability = 'ended' if current else 'unavailable'
                return self.public(self.current, now)
            if not scope or not candidate or live is not True:
                self.pause_performance()
                self.availability = (
                    'ended' if current and current.get('endedAt') is not None else 'stale'
                )
                return self.public(self.current, now)
            metadata_changed = bool(
                current
                and same_process(previous_process, identity)
                and previous_process != identity
            )
            if metadata_changed:
                # Newly supplied kernel metadata strengthens the same identity;
                # it is not evidence of a restart or a different model session.
                current['_process'] = identity
                current['_signature'] = digest([identity, current['models']])
            signature = digest([identity, selected])
            stats = raw.get('stats') if isinstance(raw.get('stats'), dict) else {}
            counts = {
                name: stats.get(source) if numeric(stats.get(source)) else None
                for name, source in (
                    ('requests', 'requests_served'),
                    ('tokens', 'tokens_generated'),
                )
            }
            if current and signature == current['_signature'] and written < current['lastSeenAt']:
                self.pause_performance()
                self.availability = 'stale'
                return self.public(current, now)
            reset = bool(
                current
                and signature == current['_signature']
                and any(
                    counts[k] is not None
                    and current['_lastCounts'].get(k) is not None
                    and counts[k] < current['_lastCounts'][k]
                    for k in counts
                )
            )
            changed = current and signature != current['_signature']
            if not current or current.get('endedAt') is not None or changed or reset:
                restarted = bool(previous_process and not same_process(previous_process, identity))
                reason = (
                    'service_restarted'
                    if restarted
                    else 'model_changed'
                    if changed
                    else 'counter_reset'
                    if reset
                    else 'service_resumed'
                    if current
                    else 'first_observed'
                )
                self.finish(now, reason)
                # On first adoption or an in-process model change, earlier daemon
                # totals cannot be attributed safely to the selected model set.
                complete = restarted
                started = min(now, identity['startedAt']) if complete else written
                baseline = {k: 0 if complete else v for k, v in counts.items()}
                current = {
                    'models': selected,
                    'startedAt': started,
                    'providerStartedAt': identity['startedAt'],
                    'countersSince': started,
                    'startReason': reason,
                    'counterScope': 'provider_start' if complete else 'observed',
                    'requestsSince': started if baseline['requests'] is not None else None,
                    'tokensSince': started if baseline['tokens'] is not None else None,
                    'endedAt': None,
                    'endReason': None,
                    'lastSeenAt': written,
                    'reputation': None,
                    '_process': identity,
                    '_signature': signature,
                    '_baseline': baseline,
                    '_lastCounts': counts,
                }
                with self.h.lock:
                    cursor = self.h.db.execute(
                        'INSERT INTO provider_sessions(scope,data) VALUES(?,?)', (scope, '{}')
                    )
                    current['id'] = cursor.lastrowid
                self.current = current
            updated = (
                metadata_changed
                or current.get('lastSeenAt') != written
                or current.get('_lastCounts') != counts
                or 'requests' not in current
            )
            for name, count in counts.items():
                if count is not None and current['_baseline'].get(name) is None:
                    current['_baseline'][name] = count
                    current[name + 'Since'] = written
                    current['counterScope'] = 'observed'
                base = current['_baseline'].get(name)
                current[name] = (
                    max(0, count - base) if count is not None and base is not None else None
                )
            last_valid = {
                name: count if count is not None else current['_lastCounts'].get(name)
                for name, count in counts.items()
            }
            current.update(lastSeenAt=written, _lastCounts=last_valid)
            before_performance = copy.deepcopy(
                (current.get('performance'), current.get('_readyLast'))
            )
            self.record_performance(counts, written, tracking)
            updated = updated or before_performance != (
                current.get('performance'),
                current.get('_readyLast'),
            )
            self.availability = 'active'
            if updated:
                self.save()
            return self.public(current, now)

    def public(self, record, now):
        if not record:
            return None
        data = {k: copy.deepcopy(v) for k, v in record.items() if not k.startswith('_')}
        data['label'] = 'Session ' + str(data['id'])
        data['status'] = 'ended' if data['endedAt'] is not None else self.availability
        if data['status'] == 'active' and not -5 < now - data['lastSeenAt'] < self.fresh_seconds:
            data['status'] = 'stale'
        end = (
            data['endedAt']
            if data['endedAt'] is not None
            else now
            if data['status'] == 'active'
            else data['lastSeenAt']
        )
        data['durationSeconds'] = max(0, end - data['startedAt'])
        if data.get('performance') and (
            data['status'] != 'active' or record.get('_readyLast') is None
        ):
            data['performance'].update(
                status='paused', detail='Statistics paused · no fresh, ready model observation.'
            )
        if data.get('reputation'):
            rep = data['reputation']
            baseline, latest = rep.pop('_baseline'), rep.pop('_latest')
            rep.pop('_record', None)
            rep.pop('_lastCounts', None)
            rep.pop('_readySegment', None)
            valid = not rep.get('counterReset', False) and all(
                latest.get(k) is None or baseline.get(k) is None or latest[k] >= baseline[k]
                for k in COUNTERS
            )
            rep['status'] = (
                'reset'
                if not valid
                else 'recorded'
                if data['status'] == 'ended'
                else 'stale'
                if now - rep['asOf'] > 150 or data['status'] != 'active'
                else 'paused'
                if data.get('performance', {}).get('status') != 'counting'
                else 'observed'
            )
            # Darkbloom 0.9.10 sends no score: its change is unknown, the counters still count.
            start, score = baseline.get('score'), latest.get('score')
            rep['scoreStart'], rep['scoreNow'] = start, score
            rep['scoreChange'] = (
                (score - start) * 100 if numeric(start) and numeric(score) else None
            )
            for key in COUNTERS:
                rep[key] = (
                    latest[key] - baseline[key]
                    if valid and latest.get(key) is not None and baseline.get(key) is not None
                    else None
                )
        return data

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            with self.h.lock:
                rows = (
                    self.h.db.execute(
                        'SELECT data FROM provider_sessions WHERE scope=? ORDER BY id DESC LIMIT 20',
                        (self.scope,),
                    ).fetchall()
                    if self.scope
                    else []
                )
            current = self.public(self.current, now)
            return {
                'current': current,
                'recent': [self.public(json.loads(r[0]), now) for r in rows],
                'scope': 'This Mac · reporting sessions · last 20 sessions',
            }

    def reputation_observation(self, data, requested_at, record_id, selected, expected_id, now):
        """Only fresh responses from the current verified network connection qualify."""
        with self.lock:
            record = self.current
            if not record or record.get('endedAt') is not None or self.availability != 'active':
                return False
            performance = record.get('performance') or {}
            segment = performance.get('segmentStartedAt')
            if (
                performance.get('status') != 'counting'
                or record.get('_readyLast') is None
                or segment is None
                or not numeric(requested_at)
                or requested_at < segment
            ):
                return False
            if (
                not numeric(requested_at)
                or not record['startedAt'] <= requested_at <= now + 5
                or now - requested_at > 120
            ):
                return False
            if not record_id or record_id != expected_id or selected != record['models']:
                return False
            if data.get('providerStatus') not in ('online', 'serving'):
                return False
            try:
                raw = json.loads((self.home / '.darkbloom/daemon-state.json').read_text())
                if (
                    not same_process(process_identity(raw), record['_process'])
                    or models(raw) != record['models']
                    or not numeric(raw.get('written_at'))
                    or not -5 < now - raw['written_at'] < 15
                ):
                    return False
                if self.probe(record['_process']) is not True:
                    return False
                # Like its statistics, a set of 3+ models Darkbloom loads on demand is
                # ready with any of them loaded; one model or a pair needs all loaded.
                warm = raw.get('warm_models')
                if not isinstance(warm, list) or not (
                    loaded_models(raw, record['models'])
                    if len(record['models']) > 2
                    else all(m in warm for m in record['models'])
                ):
                    return False
            except (OSError, ValueError, TypeError):
                return False
            values = {key: data.get(key) for key in ('score', *COUNTERS)}
            rep = record.get('reputation')
            if rep and requested_at <= rep['requestedAt']:
                return False
            if (
                not rep
                or rep['_record'] != digest(record_id)
                or rep.get('_readySegment') != segment
            ):
                # Reconnecting the coordinator does not define a new local model
                # session, but it starts a new reputation observation baseline.
                reason = (
                    'connection_changed'
                    if rep and rep['_record'] != digest(record_id)
                    else 'warm_resumed'
                    if rep
                    else 'first_reading'
                )
                rep = {
                    'since': now,
                    'baselineReason': reason,
                    '_baseline': values,
                    '_record': digest(record_id),
                    '_readySegment': segment,
                    '_lastCounts': {},
                    'counterReset': False,
                }
            # Per-field baselines: a counter unknown in the first reading (e.g. a total
            # below its parts) starts from its first known value instead of staying
            # unknown for the whole warm interval. Related counters restart together so
            # jobs and their successes and failures cover the same span.
            for group in BASELINE_GROUPS:
                if any(
                    rep['_baseline'].get(k) is None and values.get(k) is not None
                    for k in group
                ):
                    rep['_baseline'] = {**rep['_baseline'], **{k: values.get(k) for k in group}}
            if any(
                values.get(k) is not None
                and rep['_lastCounts'].get(k) is not None
                and values[k] < rep['_lastCounts'][k]
                for k in COUNTERS
            ):
                rep['counterReset'] = True
            rep['_lastCounts'].update({k: values[k] for k in COUNTERS if values.get(k) is not None})
            rep.update(asOf=now, requestedAt=requested_at, _latest=values)
            record['reputation'] = rep
            self.save()
            return record['id']
