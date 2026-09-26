"""Backend logging: a small rotating file next to the history database, plus
the most recent warnings in memory for diagnostics.

Background loops must keep running when something fails, but a failure should
leave a trace. Identical messages are logged at most once a minute, so a loop
failing every second can't flood the file. Nothing here records earnings,
account identifiers, tokens or request bodies; callers log fixed messages and
exception types.
"""

import collections
import logging
import logging.handlers
import threading
import time
from pathlib import Path

REPEAT_SECONDS = 60
RECENT = 50
MAX_BYTES = 1024 * 1024
BACKUPS = 3

recent = collections.deque(maxlen=RECENT)
# Silent until setup() runs (tests, tools): no fallback printing to stderr.
logging.getLogger('bloom').addHandler(logging.NullHandler())
_lock = threading.Lock()


class _Repeats(logging.Filter):
    """Drop a message identical to one this handler wrote in the last minute."""

    def __init__(self):
        super().__init__()
        self.seen = {}

    def filter(self, record):
        key = (
            record.name,
            record.levelno,
            record.getMessage(),
            record.exc_info[0] if record.exc_info else None,
        )
        now = time.monotonic()
        with _lock:
            last = self.seen.get(key)
            if last and now - last < REPEAT_SECONDS:
                return False
            self.seen[key] = now
            if len(self.seen) > 500:
                for k in sorted(self.seen, key=self.seen.get)[:250]:
                    del self.seen[k]
        return True


class _Recent(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.WARNING:
            recent.append(
                {
                    'at': record.created,
                    'level': record.levelname.lower(),
                    'source': record.name,
                    'message': record.getMessage()[:300],
                    'error': record.exc_info[0].__name__
                    if record.exc_info and record.exc_info[0]
                    else None,
                }
            )


def setup(data_path=None, level=logging.INFO):
    """Configure the 'bloom' logger once. data_path: the history database file."""
    root = logging.getLogger('bloom')
    if getattr(root, '_bloom_configured', False):
        return root
    root.setLevel(level)
    root.propagate = False
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
    handlers = [logging.StreamHandler(), _Recent()]
    if data_path and str(data_path) != ':memory:':
        try:
            folder = Path(data_path).parent / 'logs'
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            handlers.append(
                logging.handlers.RotatingFileHandler(
                    folder / 'bloom.log', maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding='utf-8'
                )
            )
        except OSError:
            pass
    for handler in handlers:
        handler.addFilter(_Repeats())
        handler.setFormatter(fmt)
        root.addHandler(handler)
    root._bloom_configured = True
    return root


def recent_warnings():
    return list(recent)
