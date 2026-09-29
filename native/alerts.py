"""Alerts to this Mac and the owner's phones: detectors, fan-out and the Mac relay queue.

Detectors (collector.notification_loop, every 5 s): earnings running high (the live meter's
confirmed pace, pulse_rates), the manager considering a switch (the first passing arming
check, then the excursion start) and demand spikes (demand_alerts.py). Model switches,
"no work arriving" and network news keep their own sources and reach the channels through
Channel. notify_settings.py decides which kinds go where and when.

Mac notifications: the Swift app polls POST /api/notifications/native (token route) and
posts what is queued here with UNUserNotificationCenter; nothing is sent off the Mac.
"""

import copy
import hashlib
import json
import logging
import math
import re
import secrets
import time

from model_combinations import selection_label
from notify_settings import CHANNELS, DEFAULTS, KINDS, LIMITS, SCREENS, URGENT, quiet_now

log = logging.getLogger('bloom.alerts')

MAC_TTL = 900  # a queued Mac notification older than this is dropped (like phone pushes)
MAC_BATCH = 10
RELAY_FRESH = 120  # the Swift relay polls every 15 s; silent this long = not running
KEEP_SECONDS = 7 * 86400
RECENT = 10
EARNINGS_EVERY = 60
COVERAGE_MIN = 0.8  # share of the window's minutes with a live reading
REARM_SECONDS = 1800  # pace below the threshold this long before a new episode may alert
DAILY_CAP = 3
ARMING_FRESH = 3600  # only a first check made in the last hour is news
ARMING_COOLDOWN = 3 * 3600  # per model: an arming that resets and restarts is not news again
EXCURSION_FRESH = 900
SPIKE_FRESH = 900
RECENT_SESSIONS = 8  # live-meter sessions searched for the pace window (restarts are rare)

# send() results. SENT and OFF are final (callers stop); the others are tried again while
# the alert is fresh: QUIET (quiet hours), RETRY (the phone push service queue was busy;
# the Mac copy is idempotent), NOBODY (only the phone is wanted and none can receive).
SENT, OFF, QUIET, RETRY, NOBODY = 'sent', 'off', 'quiet', 'retry', 'nobody'


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def event_key(account, kind, event_id):
    return hashlib.sha256(('%s:%s:%s' % (account, kind, event_id)).encode()).hexdigest()[:32]


def clock_label(at):
    return time.strftime('%I:%M %p', time.localtime(at)).lstrip('0')


def money(value):
    return '$%.2f' % value if value >= 0.1 else '$%.3f' % value


def without_amounts(body):
    """The phone text of a notice with dollar amounts: its sentences without them."""
    sentences = re.split(r'(?<=[.!?])\s+', body.strip())
    kept = [x for x in sentences if '$' not in x]
    if len(kept) == len(sentences):
        return body
    return ' '.join(kept + ['Open BloomGauge for the amounts.']) if kept else (
        'Open BloomGauge for the details.'
    )


class Channel:
    """A web_push-compatible sender for one alert kind, for the existing sources
    (switch_alerts.send_pending, stall_control.send_pending, the catalog watch)."""

    def __init__(self, alerts, kind):
        self.alerts, self.kind = alerts, kind

    def enqueue_switch(self, account, event_id, previous, model, reason='automatic'):
        from switch_alerts import REASONS

        settings = self.alerts.settings.get()
        # An excursion start is told by switchConsidered, with its evidence, where it is on.
        skip = {
            c
            for c in CHANNELS
            if reason == 'excursion' and settings['kinds']['switchConsidered'][c]
        }
        body = '%s → %s. %s' % (previous, model, REASONS.get(reason, REASONS['automatic']))
        return self.alerts.send(
            account,
            self.kind,
            event_id,
            'BloomGauge · model switched',
            body,
            phone=lambda: self.alerts.push.enqueue_switch(
                account, event_id, previous, model, reason
            ),
            skip=skip,
            settings=settings,
        ) in (SENT, OFF)

    def enqueue_notice(self, account, event_id, title, body, screen='test'):
        return self.alerts.send(account, self.kind, event_id, title, body, screen=screen) in (
            SENT,
            OFF,
        )

    def has_event(self, account, event_id):
        return self.alerts.push.has_event(account, event_id) or self.alerts.handled(
            account, self.kind, event_id
        )


class Alerts:
    def __init__(self, history, settings, push, clock=time.time):
        self.h, self.settings, self.push, self.clock = history, settings, push, clock
        self.relay = {'polledAt': None, 'permission': 'unknown', 'deliveredAt': None}
        self.earnings_at = None
        self.test_at = 0
        with self.h.lock:
            self.h.db.executescript("""
                CREATE TABLE IF NOT EXISTS native_notifications(
                    id TEXT PRIMARY KEY,account TEXT,at REAL,kind TEXT,title TEXT,body TEXT,
                    screen TEXT,acked REAL);
                CREATE TABLE IF NOT EXISTS alert_events(
                    id TEXT PRIMARY KEY,account TEXT,at REAL,kind TEXT,title TEXT,channels TEXT);
                CREATE INDEX IF NOT EXISTS alert_events_recent ON alert_events(account,at);
            """)
            self.h.db.commit()

    def channel(self, kind):
        return Channel(self, kind)

    # --- fan-out ------------------------------------------------------------------

    def handled(self, account, kind, event_id):
        with self.h.lock:
            return bool(
                self.h.db.execute(
                    'SELECT 1 FROM alert_events WHERE id=?', (event_key(account, kind, event_id),)
                ).fetchone()
            )

    def send(
        self,
        account,
        kind,
        event_id,
        title,
        body,
        screen='test',
        phone_body=None,
        phone=None,
        skip=(),
        settings=None,
        now=None,
    ):
        """One of SENT, OFF, QUIET, RETRY, NOBODY (see above). Only SENT is recorded, so an
        alert held by quiet hours, a busy push queue or a missing phone goes once it can.

        phone_body: the phone text without amounts (used unless phoneAmounts is on); other
        notices lose their sentences with amounts on the phone, except network news (public
        network estimates, as before); phone: a callable replacing the default phone notice."""
        now = self.clock() if now is None else now
        if not account or kind not in KINDS or not isinstance(event_id, (str, int)):
            return NOBODY
        key = event_key(account, kind, event_id)
        if self.handled(account, kind, event_id):
            return SENT
        settings = settings or self.settings.get()
        wanted = [c for c in CHANNELS if settings['kinds'][kind][c] and c not in skip]
        if not wanted:
            return OFF
        if kind not in URGENT and quiet_now(settings, now):
            return QUIET
        used, waiting = [], False
        if 'mac' in wanted and self.queue_mac(key, account, kind, title, body, screen, now):
            used.append('mac')
        if 'phone' in wanted and self.phone_ready(account):
            if settings['phoneAmounts']:
                text = body
            elif phone_body is not None:
                text = phone_body
            else:
                text = body if kind == 'networkNews' else without_amounts(body)
            try:
                sent = (
                    phone()
                    if phone
                    else self.push.enqueue_notice(account, event_id, title, text, screen=screen)
                )
            except Exception:
                log.exception('Phone alert failed')
                sent = False
            if sent or self.push.has_event(account, event_id):
                used.append('phone')
            else:
                waiting = True
        if waiting:
            return RETRY
        if not used:
            return NOBODY
        self.mark(key, account, kind, title, used, now)
        return SENT

    def phone_ready(self, account):
        """A subscribed phone can receive; never creates push keys (web_push.ready)."""
        try:
            return bool(self.push.ready(account))
        except Exception:
            return False

    def mark(self, key, account, kind, title, channels, now):
        with self.h.lock:
            self.h.db.execute(
                'INSERT OR IGNORE INTO alert_events VALUES(?,?,?,?,?,?)',
                (key, account, now, kind, title[:80], ','.join(channels)),
            )
            self.h.db.execute('DELETE FROM alert_events WHERE at<?', (now - KEEP_SECONDS,))
            self.h.db.commit()

    def recent(self, account):
        with self.h.lock:
            rows = self.h.db.execute(
                """SELECT kind,at,title,channels FROM alert_events
                WHERE account=? AND channels!='' ORDER BY at DESC LIMIT ?""",
                (account, RECENT),
            ).fetchall()
        return [
            {'kind': r[0], 'at': r[1], 'title': r[2], 'channels': r[3].split(',')} for r in rows
        ]

    # --- Mac relay ----------------------------------------------------------------

    def queue_mac(self, key, account, kind, title, body, screen, now):
        screen = screen if screen in SCREENS else 'test'
        with self.h.lock:
            self.h.db.execute(
                'INSERT OR IGNORE INTO native_notifications VALUES(?,?,?,?,?,?,?,NULL)',
                (key, account, now, kind, title.strip()[:80], body.strip()[:300], screen),
            )
            self.h.db.execute(
                'DELETE FROM native_notifications WHERE at<?', (now - KEEP_SECONDS,)
            )
            self.h.db.commit()
        return True

    def poll(self, account, permission, now=None):
        now = self.clock() if now is None else now
        self.relay['polledAt'] = now
        if permission in ('granted', 'denied', 'notDetermined'):
            self.relay['permission'] = permission
        with self.h.lock:
            rows = self.h.db.execute(
                """SELECT id,kind,title,body,screen,at FROM native_notifications
                WHERE account=? AND acked IS NULL AND at>=? ORDER BY at LIMIT ?""",
                (account or '', now - MAC_TTL, MAC_BATCH),
            ).fetchall()
        return {
            'items': [
                dict(zip(('id', 'kind', 'title', 'body', 'screen', 'at'), row)) for row in rows
            ],
            'pollSeconds': 15,
        }

    def ack(self, ids, now=None):
        now = self.clock() if now is None else now
        with self.h.lock:
            done = self.h.db.executemany(
                'UPDATE native_notifications SET acked=? WHERE id=? AND acked IS NULL',
                [(now, i) for i in ids],
            ).rowcount
            self.h.db.commit()
        if done and self.relay['permission'] == 'granted':  # denied: acked items are dropped
            self.relay['deliveredAt'] = now
        return {'ok': True}

    def test_mac(self, account, now=None):
        now = self.clock() if now is None else now
        if 0 <= now - self.test_at < 10:
            raise ValueError('Wait a few seconds before sending another test.')
        self.test_at = now
        self.queue_mac(
            secrets.token_hex(16),
            account or '',
            'test',
            'BloomGauge · test',
            'Mac notifications work. Click to open BloomGauge.',
            'test',
            now,
        )

    def mac_status(self, account, now):
        polled = self.relay['polledAt']
        with self.h.lock:
            pending = self.h.db.execute(
                'SELECT COUNT(*) FROM native_notifications WHERE account=? AND acked IS NULL AND at>=?',
                (account or '', now - MAC_TTL),
            ).fetchone()[0]
        return {
            'available': polled is not None and 0 <= now - polled <= RELAY_FRESH,
            'permission': self.relay['permission'],
            'lastPolledAt': polled,
            'lastDeliveredAt': self.relay['deliveredAt'],
            'pending': pending,
        }

    def view(self, account, now=None):
        now = self.clock() if now is None else now
        settings = self.settings.get()
        try:
            phone = self.push.status(account)
        except Exception:
            log.exception('Phone notification status failed')
            phone = None
        return {
            'settings': settings,
            'defaults': copy.deepcopy(DEFAULTS),
            'limits': copy.deepcopy(LIMITS),
            'mac': self.mac_status(account, now),
            'phone': phone,
            'earningsNow': self.earnings_pace(account, now, settings['earnings']['minutes']),
            'recent': self.recent(account),
        }

    # --- detectors ----------------------------------------------------------------

    def tick(self, account, device, now, optimizer=None, model=None):
        if not account:
            return
        for check in (
            lambda: self.check_earnings(account, now, model),
            lambda: optimizer is not None and self.check_manager(account, now, optimizer),
            lambda: device and self.check_spikes(account, device, now),
        ):
            try:
                check()
            except Exception:
                log.exception('Alert check failed')  # one detector never stops the others

    def earnings_pace(self, account, now, minutes):
        """The live meter's mean $/h over the last `minutes` (pulse_rates, one reading a
        second): per-minute means averaged over the minutes that have readings."""
        start = now - minutes * 60
        with self.h.lock:
            # The primary key (account, session, at) serves both queries; filtering on `at`
            # alone would read every row the account has.
            rows = self.h.db.execute(
                """SELECT CAST((at-?)/60 AS INTEGER) AS b,AVG(rate60) FROM pulse_rates
                WHERE account=? AND session IN (SELECT DISTINCT session FROM pulse_rates
                    WHERE account=? ORDER BY session DESC LIMIT ?)
                AND at>? AND at<=? AND rate60 IS NOT NULL GROUP BY b""",
                (start, account or '', account or '', RECENT_SESSIONS, start, now),
            ).fetchall()
        values = {r[0]: r[1] for r in rows if finite(r[1]) and 0 <= r[0] < minutes}

        def mean(part):
            return sum(part) / len(part) if part else None

        half = minutes / 2
        return {
            'usdPerHour': mean(list(values.values())),
            'minutes': minutes,
            'coverage': min(1.0, len(values) / minutes),
            # Sustained, not one big job: each half of the window on its own.
            'halves': [
                mean([v for b, v in values.items() if (b < half) == first]) for first in (True, False)
            ],
        }

    def check_earnings(self, account, now, model=None):
        if self.earnings_at is not None and 0 <= now - self.earnings_at < EARNINGS_EVERY:
            return
        self.earnings_at = now
        settings = self.settings.get()
        threshold, minutes = settings['earnings']['usdPerHour'], settings['earnings']['minutes']
        pace = self.earnings_pace(account, now, minutes)
        rate = pace['usdPerHour']
        key = 'alerts-earnings:' + hashlib.sha256(account.encode()).hexdigest()
        with self.h.lock:
            saved = self.h.cache(key)
        state = saved if isinstance(saved, dict) else {}
        state = {
            'active': state.get('active') is True,
            'belowSince': state.get('belowSince') if finite(state.get('belowSince')) else None,
            'day': state.get('day') if isinstance(state.get('day'), str) else None,
            'count': state.get('count') if type(state.get('count')) is int else 0,
            # An alert that could not go yet (turned off, quiet hours, no phone): same id later.
            'episode': state.get('episode') if finite(state.get('episode')) else None,
        }
        day = time.strftime('%Y-%m-%d', time.localtime(now))
        if state['day'] != day:
            state.update(day=day, count=0)
        high = rate is not None and all(h is not None and h >= threshold for h in pace['halves'])
        if state['active']:
            # An episode ends once the pace has stayed below the threshold for 30 min.
            if high:
                state['belowSince'] = None
            else:
                state['belowSince'] = state['belowSince'] or now
                if now - state['belowSince'] >= REARM_SECONDS:
                    state.update(active=False, belowSince=None)
        elif high and pace['coverage'] >= COVERAGE_MIN and state['count'] < DAILY_CAP:
            serving = (', serving ' + selection_label(model)) if model else ''
            episode = state['episode'] or now
            status = self.send(
                account,
                'earningsHigh',
                'earnings-high-%d' % episode,
                'BloomGauge · earnings running high',
                '%s/h over the last %d min (your alert: %s/h)%s.'
                % (money(rate), minutes, money(threshold), serving),
                screen='overview',
                phone_body='Earnings have been above your alert level for the last %d min%s.'
                % (minutes, serving),
                settings=settings,
                now=now,
            )
            if status == SENT:
                state.update(active=True, belowSince=None, count=state['count'] + 1, episode=None)
            else:
                state['episode'] = episode
        elif not high:
            state['episode'] = None
        if state != saved:
            with self.h.lock:
                self.h.cache(key, state)

    def check_manager(self, account, now, optimizer):
        with optimizer.lock:
            saved = (optimizer.state or {}).get('manager') or {}
            m = copy.deepcopy({k: saved.get(k) for k in ('arming', 'excursion', 'home')})
            view = (getattr(optimizer, 'last_demand_decision', None) or {}).get('manager') or {}
            view = copy.deepcopy({k: view.get(k) for k in ('arming', 'home')})
        home = (view.get('home') or m.get('home') or {}).get('model')
        home_label = selection_label(home) if home else 'your home model'
        arming = m.get('arming') or {}
        since = arming.get('since')
        told_key = 'alerts-arming:' + hashlib.sha256(account.encode()).hexdigest()
        with self.h.lock:
            told = self.h.cache(told_key)
        told = told if isinstance(told, dict) else {}
        last_told = told.get(arming.get('model')) if isinstance(arming.get('model'), str) else None
        if (
            isinstance(arming.get('model'), str)
            and (arming.get('checks') or 0) >= 1
            and finite(since)
            and 0 <= now - since <= ARMING_FRESH
            and not (
                finite(last_told) and last_told != since and 0 <= since - last_told < ARMING_COOLDOWN
            )
        ):
            shown = view.get('arming') or {}
            ratio = shown.get('ratio') if shown.get('model') == arming['model'] else None
            last = arming.get('lastCheckAt') if finite(arming.get('lastCheckAt')) else since
            pays = (
                'paying %.1fx %s' % (ratio, home_label)
                if finite(ratio)
                else 'paying more than %s' % home_label
            )
            if (
                self.send(
                    account,
                    'switchConsidered',
                    'manager-arming-%s-%d' % (arming['model'], since),
                    'BloomGauge · considering a switch',
                    'Public data shows %s %s on Macs like this one. If that holds at the next '
                    'hourly check (about %s), BloomGauge switches to it for a while.'
                    % (selection_label(arming['model']), pays, clock_label(last + 3600)),
                    now=now,
                )
                == SENT
                and last_told != since
            ):
                told = {
                    k: v for k, v in told.items() if finite(v) and 0 <= now - v < ARMING_COOLDOWN
                }
                told[arming['model']] = since
                with self.h.lock:
                    self.h.cache(told_key, told)
        excursion = m.get('excursion') or {}
        started = excursion.get('startedAt')
        if (
            isinstance(excursion.get('target'), str)
            and finite(started)
            and not excursion.get('endReason')
            and 0 <= now - started <= EXCURSION_FRESH
        ):
            target = selection_label(excursion['target'])
            was = (' (was %s)' % selection_label(excursion['from'])) if excursion.get('from') else ''
            back = 'BloomGauge returns to %s afterwards.' % home_label
            predicted = excursion.get('predictedUsdPerHour')
            self.send(
                account,
                'switchConsidered',
                'manager-excursion-%s-%d' % (excursion['target'], started),
                'BloomGauge · switched to catch demand',
                'Now serving %s%s to catch high demand%s. %s'
                % (
                    target,
                    was,
                    '; expected about %s/h' % money(predicted) if finite(predicted) else '',
                    back,
                ),
                phone_body='Now serving %s%s to catch high demand. %s' % (target, was, back),
                now=now,
            )

    def check_spikes(self, account, device, now):
        with self.h.lock:
            rows = self.h.db.execute(
                """SELECT id,model,payload FROM demand_alert_events
                WHERE account=? AND device=? AND at>=? ORDER BY id LIMIT 50""",
                (account, device, now - SPIKE_FRESH),
            ).fetchall()
        for row_id, model, payload in rows:
            try:
                ratio = json.loads(payload).get('pressureRatio')
            except (ValueError, TypeError, AttributeError):
                ratio = None
            self.send(
                account,
                'demandSpike',
                'demand-spike-%d' % row_id,
                'BloomGauge · demand spike',
                '%s: demand is %s its usual level on Darkbloom right now.'
                % (
                    selection_label(model),
                    '%.1fx' % ratio if finite(ratio) else 'well above',
                ),
                screen='demand',
                now=now,
            )
