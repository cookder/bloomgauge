"""Which "What's changed" notes this Mac has dismissed, shared by the Mac and phone views.

Stored in the history cache, so a note closed on the phone stays closed on the Mac.
`returning` is decided once, the first time a version with this module runs: a Mac that
had already finished setup (or had run the optimizer) was using an earlier version, so
it gets the note; a brand-new install does not, because the first-On explainer covers it.
"""

import copy
import math
import re
import threading
import time

KEY = 'whats-changed-v1'
NOTE = re.compile(r'[a-z0-9][a-z0-9.-]{0,63}')
SEEN_LIMIT = 16


def valid(state):
    return (
        type(state) is dict
        and state.get('schema') == 1
        and type(state.get('returning')) is bool
        and type(state.get('firstSeenAt')) in (int, float)
        and math.isfinite(state['firstSeenAt'])
        and type(state.get('seen')) is list
        and len(state['seen']) <= SEEN_LIMIT
        and all(type(item) is str and NOTE.fullmatch(item) for item in state['seen'])
    )


class WhatsChanged:
    def __init__(self, collector, enabled=True, now=time.time):
        self.collector, self.enabled, self.now = collector, enabled, now
        self.lock = threading.RLock()
        self.state = None
        self.available = False
        if not enabled:
            return
        try:
            saved = collector.history.cache(KEY)
            if saved is None:
                saved = {
                    'schema': 1,
                    'returning': self.earlier_use(),
                    'firstSeenAt': now(),
                    'seen': [],
                }
                collector.history.cache(KEY, saved)
            elif not valid(saved):
                raise ValueError('Invalid saved note state.')
            self.state = saved
            self.available = True
        except Exception:
            # The page falls back to this browser's own memory of dismissed notes.
            self.state = None

    def earlier_use(self):
        try:
            if (self.collector.history.cache('setup-v1') or {}).get('completed') is True:
                return True
        except Exception:
            pass
        try:
            with self.collector.optimizer.lock:
                started = self.collector.optimizer.state.get('startedAt')
            return type(started) in (int, float) and math.isfinite(started) and started > 0
        except Exception:
            return False

    def status(self, preview=False):
        with self.lock:
            if preview or not self.available or self.state is None:
                return {'schema': 1, 'available': False, 'returning': None, 'seen': []}
            return {
                'schema': 1,
                'available': True,
                'returning': self.state['returning'],
                'seen': list(self.state['seen']),
            }

    def action(self, data, preview=False):
        if preview or not self.enabled:
            raise PermissionError('Notes are read-only in setup preview.')
        if (
            type(data) is not dict
            or set(data) != {'action', 'id'}
            or data.get('action') != 'seen'
            or type(data.get('id')) is not str
            or not NOTE.fullmatch(data['id'])
        ):
            raise ValueError('Choose a valid note.')
        with self.lock:
            if not self.available or self.state is None:
                raise RuntimeError('Note preferences are unavailable.')
            next_state = copy.deepcopy(self.state)
            if data['id'] not in next_state['seen']:
                next_state['seen'] = (next_state['seen'] + [data['id']])[-SEEN_LIMIT:]
                self.collector.history.cache(KEY, next_state)
                self.state = next_state
        return self.status()
