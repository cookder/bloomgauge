"""A local, validated digest written by the user's scheduled Codex Slack review.

This module never connects to Slack or executes advice from a discussion.
The HTTP view is read-only; the local CLI publishes summaries atomically.
"""

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

SCHEMA = 1
LIMIT = 2 * 1024 * 1024
KINDS = ('team_update', 'model_tip', 'operations', 'measurement')
STATES = ('ok', 'partial', 'blocked', 'error')


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def text(value, limit, optional=False):
    if optional and value is None:
        return ''
    if not isinstance(value, str) or len(value) > limit or (not optional and not value.strip()):
        raise ValueError('A digest text field is missing or too long.')
    if re.search(r'xox[baprs]-|\.ts\.net\b', value, re.I):
        raise ValueError('Do not include credentials or private dashboard addresses.')
    return value.strip()


def slack_link(value):
    value = text(value, 400)
    parsed = urlsplit(value)
    if (
        parsed.scheme != 'https'
        or parsed.username
        or parsed.password
        or parsed.port
        or not re.fullmatch(r'[a-z0-9][a-z0-9-]*\.slack\.com', parsed.netloc)
        or not re.fullmatch(r'/archives/[CG][A-Z0-9]+/p[0-9]{15,20}', parsed.path)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            'Sources must be direct HTTPS Slack message links without query parameters.'
        )
    return value


def validate_review(payload, now):
    if not isinstance(payload, dict) or payload.get('status') not in STATES:
        raise ValueError('A review needs a valid status.')
    allowed = {'status', 'detail', 'from', 'to', 'channels', 'headline', 'summary', 'items'}
    if set(payload) - allowed:
        raise ValueError('Unexpected review fields.')
    status = payload['status']
    start, end = payload.get('from'), payload.get('to')
    if not number(start) or not number(end) or end < start or end > now + 60:
        raise ValueError('Use a real, ordered review period.')
    channels = payload.get('channels', [])
    if not isinstance(channels, list) or len(channels) > 12:
        raise ValueError('Invalid channel coverage.')
    channels = list(dict.fromkeys(text(c, 80) for c in channels))
    if status in ('ok', 'partial') and not channels:
        raise ValueError('A completed review must name the channels actually read.')
    items = payload.get('items', [])
    if not isinstance(items, list) or len(items) > 12 or (status in ('blocked', 'error') and items):
        raise ValueError('Invalid insights for this review status.')
    clean = []
    for item in items:
        if not isinstance(item, dict) or set(item) - {
            'kind',
            'title',
            'body',
            'relevance',
            'measure',
            'models',
            'sources',
        }:
            raise ValueError('Invalid insight.')
        if item.get('kind') not in KINDS:
            raise ValueError('Unknown insight category.')
        sources = item.get('sources')
        if not isinstance(sources, list) or not 1 <= len(sources) <= 4:
            raise ValueError('Every insight needs at least one source.')
        references = []
        for source in sources:
            if not isinstance(source, dict) or set(source) != {
                'url',
                'channel',
                'at',
                'author',
                'authority',
            }:
                raise ValueError('Invalid source fields.')
            if not number(source['at']) or not start <= source['at'] <= end:
                raise ValueError('Source time must be inside the reviewed period.')
            if source['authority'] not in ('team', 'community', 'unverified'):
                raise ValueError('Unknown source authority.')
            references.append(
                {
                    'url': slack_link(source['url']),
                    'channel': text(source['channel'], 80),
                    'at': source['at'],
                    'author': text(source['author'], 80),
                    'authority': source['authority'],
                }
            )
        if item['kind'] == 'team_update' and not any(s['authority'] == 'team' for s in references):
            raise ValueError('A team update must include a verified team source.')
        models = item.get('models', [])
        if not isinstance(models, list) or len(models) > 8:
            raise ValueError('Invalid model labels.')
        title = text(item.get('title'), 140)
        key = json.dumps([item['kind'], title, sorted(s['url'] for s in references)])
        clean.append(
            {
                'id': hashlib.sha256(key.encode()).hexdigest()[:20],
                'kind': item['kind'],
                'title': title,
                'body': text(item.get('body'), 1400),
                'relevance': text(item.get('relevance'), 600, True),
                'measure': text(item.get('measure'), 600, True),
                'models': [text(m, 100) for m in models],
                'sources': references,
            }
        )
    if len({i['id'] for i in clean}) != len(clean):
        raise ValueError('Remove duplicate insights from the review.')
    return {
        'at': now,
        'status': status,
        'detail': text(payload.get('detail'), 500),
        'from': start,
        'to': end,
        'channels': channels,
        'headline': text(payload.get('headline'), 140, not clean),
        'summary': text(payload.get('summary'), 1200, not clean),
        'items': clean,
    }


class CommunityInsights:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else None
        self.path = self.directory / 'community-insights.json' if self.directory else None

    @staticmethod
    def empty():
        return {
            'schemaVersion': SCHEMA,
            'schedule': {'enabled': False, 'hours': [8, 20], 'timeZone': 'America/Chicago'},
            'lastAttempt': None,
            'lastSuccessAt': None,
            'reviewedThrough': None,
            'digests': [],
        }

    def read(self):
        if self.path is None or not self.path.exists():
            return self.empty()
        if self.path.is_symlink() or self.path.stat().st_size > LIMIT:
            raise ValueError('The saved digest is unavailable.')
        value = json.loads(self.path.read_text())
        if not isinstance(value, dict) or value.get('schemaVersion') != SCHEMA:
            raise ValueError('The saved digest format is unavailable.')
        # Only accept the bounded schema produced by this module, including on disk.
        schedule = value.get('schedule')
        if (
            not isinstance(schedule, dict)
            or type(schedule.get('enabled')) is not bool
            or schedule.get('hours') != [8, 20]
            or schedule.get('timeZone') != 'America/Chicago'
        ):
            raise ValueError('Invalid saved schedule.')
        digests = value.get('digests')
        if not isinstance(digests, list) or len(digests) > 30:
            raise ValueError('Invalid saved digest history.')
        for digest in digests:
            at = digest.get('at') if isinstance(digest, dict) else None
            if not number(at):
                raise ValueError('Invalid saved review time.')
            payload = {k: v for k, v in digest.items() if k not in ('at', 'id')}
            payload['items'] = [
                {k: v for k, v in i.items() if k != 'id'} for i in payload.get('items', [])
            ]
            if validate_review(payload, at) != {k: v for k, v in digest.items() if k != 'id'}:
                raise ValueError('Invalid saved digest.')
        attempt = value.get('lastAttempt')
        if attempt is not None:
            if not isinstance(attempt, dict) or not number(attempt.get('at')):
                raise ValueError('Invalid saved review status.')
            validate_review(
                {**{k: v for k, v in attempt.items() if k != 'at'}, 'items': []}, attempt['at']
            )
        for key in ('lastSuccessAt', 'reviewedThrough'):
            if value.get(key) is not None and not number(value[key]):
                raise ValueError('Invalid saved coverage.')
        return {k: value.get(k) for k in self.empty()}

    def update(self, change):
        if not self.directory:
            raise ValueError('A local data directory is required.')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.directory / '.community-insights.lock'
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.read()
            change(state)
            encoded = json.dumps(state, ensure_ascii=False, allow_nan=False).encode()
            if len(encoded) > LIMIT:
                raise ValueError('Digest history exceeds the local size limit.')
            handle, temporary = tempfile.mkstemp(prefix='.community-', dir=self.directory)
            try:
                with os.fdopen(handle, 'wb') as out:
                    out.write(encoded)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def ingest(self, payload, now=None):
        now = time.time() if now is None else now
        review = validate_review(payload, now)

        def save(state):
            previous = state.get('lastAttempt')
            if previous and now < previous['at']:
                raise ValueError('An older review cannot replace a newer check.')
            state['lastAttempt'] = {k: v for k, v in review.items() if k != 'items'}
            if review['status'] == 'ok':
                state['lastSuccessAt'] = now
                state['reviewedThrough'] = max(state.get('reviewedThrough') or 0, review['to'])
            if review['items']:
                digest = {
                    **review,
                    'id': hashlib.sha256(json.dumps(review, sort_keys=True).encode()).hexdigest()[
                        :20
                    ],
                }
                state['digests'] = [digest] + [
                    d for d in state['digests'] if d['id'] != digest['id']
                ]
                state['digests'] = state['digests'][:30]

        self.update(save)

    def configure(self, enabled):
        self.update(lambda state: state['schedule'].update(enabled=enabled))

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        try:
            state = self.read()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return {
                **self.empty(),
                'at': now,
                'status': 'error',
                'detail': 'The saved community digest could not be read. Your earnings history is unaffected.',
                'nextCheckAt': None,
            }
        last = state['lastAttempt']
        status = last['status'] if last else 'waiting'
        if last and last['status'] in ('ok', 'partial') and now - last['at'] > 15 * 3600:
            status = 'stale'
        local = dt.datetime.fromtimestamp(now, ZoneInfo('America/Chicago'))
        times = [
            local.replace(hour=h, minute=0, second=0, microsecond=0) + dt.timedelta(days=d)
            for d in (0, 1)
            for h in (8, 20)
        ]
        next_at = (
            min(t.timestamp() for t in times if t.timestamp() > now)
            if state['schedule']['enabled']
            else None
        )
        return {
            **state,
            'at': now,
            'status': status,
            'nextCheckAt': next_at,
            'detail': last['detail'] if last else 'Waiting for the first Slack review.',
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--data-dir', default=str(Path.home() / 'Library/Application Support/Bloom Dashboard')
    )
    commands = parser.add_subparsers(dest='command', required=True)
    ingest = commands.add_parser('ingest')
    ingest.add_argument('--input', required=True)
    commands.add_parser('status')
    configure = commands.add_parser('schedule')
    configure.add_argument('state', choices=('enabled', 'disabled'))
    args = parser.parse_args()
    store = CommunityInsights(args.data_dir)
    try:
        if args.command == 'ingest':
            path = Path(args.input)
            if path.stat().st_size > 96 * 1024:
                raise ValueError('Review input exceeds 96 KB.')
            store.ingest(json.loads(path.read_text()))
        elif args.command == 'schedule':
            store.configure(args.state == 'enabled')
        result = store.snapshot()
        print(
            json.dumps(
                {
                    'status': result['status'],
                    'lastSuccessAt': result['lastSuccessAt'],
                    'reviewedThrough': result['reviewedThrough'],
                    'digests': len(result['digests']),
                    'schedule': result['schedule'],
                },
                indent=2,
            )
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(1, 'Community digest was not updated: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
