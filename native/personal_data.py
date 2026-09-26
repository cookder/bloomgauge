"""Build-time pattern for the builder's personal data (release checks only, never bundled)."""

import os, re, subprocess
from pathlib import Path


def personal_pattern(source):
    """The builder's home folder, their email addresses and private tailnet URLs.

    Emails come from git's user.email plus BLOOM_PERSONAL_EMAILS (comma-separated),
    read at run time so they never appear in the source. Bundled packages credit
    their authors by email, so a generic address pattern can't be used; with no
    address known the check would silently skip emails, so it refuses instead.
    """
    try:
        owner = subprocess.run(
            ['git', '-C', str(source), 'config', 'user.email'], capture_output=True, text=True
        ).stdout.strip()
    except OSError:
        owner = ''
    emails = {e.strip() for e in [owner, *os.environ.get('BLOOM_PERSONAL_EMAILS', '').split(',')]}
    emails.discard('')
    if not emails:
        raise SystemExit(
            'No personal email to check for: set git user.email or BLOOM_PERSONAL_EMAILS.'
        )
    return re.compile(
        '|'.join(
            [re.escape(str(Path.home())) + r'\b', *map(re.escape, sorted(emails))]
            + [r'https://[^\s\"\']+\.ts\.net']
        )
    )
