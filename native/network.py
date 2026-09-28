"""Public, read-only network endpoints used by the official Darkbloom console."""

import copy, json, logging, threading, time, urllib.request, urllib.error

log = logging.getLogger('bloom.network')

# The saved copy only seeds stale values at startup. Writing all ~1.6 MB (mostly
# the provider list in /v1/stats) on every poll cost ~12 GB of writes a day.
SAVE_SECONDS = 300


class Network:
    def __init__(self, history, stop):
        self.history = history
        self.stop = stop
        self.lock = threading.RLock()
        self.saved_at = 0
        # Callables (key, data, at) run after each successful fetch, on that fetch's thread.
        self.listeners = []
        # Callables (key, error, at) run after each failed fetch: error is 'HTTP <code>' for
        # Darkbloom's own reply, else 'Connection unavailable' (network_health.py).
        self.error_listeners = []
        self.state = history.cache('network') or {}
        for entry in self.state.values():
            entry['status'] = 'stale'

    def start(self):
        threading.Thread(target=self.backfill, daemon=True).start()
        for key, path, interval in [
            ('totals', '/v1/network/totals', 60),
            ('capacity', '/v1/models/capacity', 30),
            ('stats', '/v1/stats', 60),
            ('series', '/v1/network/series?window=30m', 60),
            ('pricing', '/v1/pricing', 900),
        ]:
            threading.Thread(target=self.loop, args=(key, path, interval), daemon=True).start()

    def loop(self, key, path, interval):
        while not self.stop.is_set():
            try:
                d = self.fetch(path)
                if key == 'series':
                    self.history.save_network_series(d)
                with self.lock:
                    self.state[key] = {
                        'status': 'ok',
                        'updatedAt': time.time(),
                        'data': d
                        if key != 'series'
                        else {
                            'window': d['window'],
                            'start_at': d['start_at'],
                            'end_at': d['end_at'],
                        },
                        'error': None,
                    }
                    self.save()
                for listener in list(self.listeners):
                    try:
                        listener(key, d, time.time())
                    except Exception:
                        log.exception('Network listener failed for %s', key)
            except Exception as exc:
                code = (
                    f'HTTP {exc.code}'
                    if isinstance(exc, urllib.error.HTTPError)
                    else 'Connection unavailable'
                )
                with self.lock:
                    previous = self.state.get(key, {})
                    self.state[key] = {
                        **previous,
                        'status': 'stale' if previous.get('data') else 'missing',
                        'error': code,
                    }
                for listener in list(self.error_listeners):
                    try:
                        listener(key, code, time.time())
                    except Exception:
                        log.exception('Network error listener failed for %s', key)
            self.stop.wait(interval)

    def backfill(self):
        while not self.stop.is_set():
            loaded = []
            errors = []
            for window in ['30d', '7d', '24h']:
                if self.stop.is_set():
                    return
                try:
                    data = self.fetch('/v1/network/series?window=' + window)
                    self.history.save_network_series(data)
                    loaded.append(window)
                except Exception as exc:
                    errors.append(
                        window
                        + ': '
                        + (
                            f'HTTP {exc.code}'
                            if isinstance(exc, urllib.error.HTTPError)
                            else 'unavailable'
                        )
                    )
            with self.lock:
                self.state['backfill'] = {
                    'status': 'ok' if not errors else 'partial' if loaded else 'missing',
                    'updatedAt': time.time(),
                    'data': {'windows': loaded},
                    'error': '; '.join(errors) if errors else None,
                }
                self.save()
            self.stop.wait(600)

    def fetch(self, path):
        req = urllib.request.Request(
            'https://api.darkbloom.dev' + path,
            headers={'Accept': 'application/json', 'User-Agent': 'BloomDashboard/1.1'},
        )
        with urllib.request.urlopen(req, timeout=18) as res:
            raw = res.read(16 * 1024 * 1024)
            data = json.loads(raw)
        if not isinstance(data, dict) or 'error' in data:
            raise ValueError('Invalid response')
        if '/series' in path and not isinstance(data.get('time_series'), list):
            raise ValueError('Invalid series')
        if '/capacity' in path and not isinstance(data.get('models'), list):
            raise ValueError('Invalid capacity')
        if path == '/v1/stats' and not isinstance(data.get('providers'), list):
            raise ValueError('Invalid stats')
        if path == '/v1/network/totals' and not isinstance(data.get('jobs'), (int, float)):
            raise ValueError('Invalid totals')
        if path == '/v1/pricing' and not isinstance(data.get('prices'), list):
            raise ValueError('Invalid pricing')
        return data

    def save(self, force=False):
        """At most every 5 minutes, without the provider list (refetched within a minute)."""
        with self.lock:
            if not force and time.time() - self.saved_at < SAVE_SECONDS:
                return
            self.saved_at = time.time()
            state = dict(self.state)
            stats = state.get('stats')
            if (
                isinstance(stats, dict)
                and isinstance(stats.get('data'), dict)
                and 'providers' in stats['data']
            ):
                state['stats'] = {
                    **stats,
                    'data': {k: v for k, v in stats['data'].items() if k != 'providers'},
                }
            self.history.cache('network', state)

    def snapshot(self, key=None):
        """A deep copy of everything, or of one entry (e.g. 'capacity', a few KB)."""
        with self.lock:
            return copy.deepcopy(self.state if key is None else self.state.get(key) or {})
