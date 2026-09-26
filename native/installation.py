"""Per-install id and build edition. Local only; no accounts or licences."""

import json
import pathlib
import re
import uuid

ROOT = pathlib.Path(__file__).resolve().parent
KEY = 'bloom-installation-v1'


def installation_id(history):
    """Random id saved in history. My Macs uses it to tell installs apart."""
    saved = history.cache(KEY)
    if isinstance(saved, str) and re.fullmatch(r'[a-f0-9]{32}', saved):
        return saved
    value = uuid.uuid4().hex
    history.cache(KEY, value)
    return value


def personal_edition(config=ROOT / 'product-config.json'):
    """True only in builds made for the maintainer's own Mac (release-manifest.py)."""
    try:
        return json.loads(config.read_text()).get('edition') == 'personal'
    except (OSError, ValueError, AttributeError):
        return False
