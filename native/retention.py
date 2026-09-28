"""Keep high-volume, fine-grained history for 90 days.

Without this the database only grew (~45 MB a day in Sep 2026). Only
per-second samples and passive journals are trimmed; the optimizer's own
evidence (credits, ready minutes, network demand, runs, events) is kept, and
90 days covers the longest chart range. Deletes run in small batches so the
one-second sampler never waits long for the history lock; freed pages are
reused by SQLite, so the file stops growing rather than shrinking. A table
listed as (column, seconds) keeps that long instead.
"""

import logging
import time

log = logging.getLogger('bloom.retention')

KEEP_SECONDS = 90 * 86400
BATCH = 5000
EVERY_SECONDS = 86400
FIRST_RUN_SECONDS = 600
# table -> its time column (all in Unix seconds), or (column, seconds to keep)
TABLES = {
    'samples': 'at',
    'pulse_rates': 'at',
    'traffic_intervals': 'end',
    'opportunity_observations': 'at',
    'concurrency_observations': 'at',
    'earnings_forecast_observations': 'created_at',
    'network_cell_rates': ('hour', 30 * 86400),  # public aggregates, network_evidence.py
    'model_catalog_events': 'at',  # network news, model_catalog_watch.py
    'network_incidents': 'start',  # Darkbloom outages (aggregates only), network_health.py
}


def prune(history, now, keep=KEEP_SECONDS, pause=0.05):
    """Delete rows older than keep (or the table's own period); returns {table: rows deleted}."""
    deleted = {}
    with history.lock:
        present = {
            r[0] for r in history.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    for table, spec in TABLES.items():
        if table not in present:
            continue
        column, seconds = spec if isinstance(spec, tuple) else (spec, keep)
        cutoff = now - seconds
        total = 0
        while True:
            with history.lock:
                # Oldest rows have the lowest rowids, so this finds them without a full scan.
                n = history.db.execute(
                    f'DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {column}<? LIMIT ?)',
                    (cutoff, BATCH),
                ).rowcount
                history.db.commit()
            total += max(0, n)
            if n < BATCH:
                break
            time.sleep(pause)
        if total:
            deleted[table] = total
    if deleted:
        log.info('Retention removed rows older than %d days: %s', keep // 86400, deleted)
    return deleted


def loop(history, stop, clock=time.time):
    if stop.wait(FIRST_RUN_SECONDS):
        return
    while True:
        try:
            prune(history, clock())
        except Exception:
            log.exception('Retention pass failed')
        if stop.wait(EVERY_SECONDS):
            return
