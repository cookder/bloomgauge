"""Bounded bundled notes, exposed only for the actual installed app version."""

import json
import pathlib
import plistlib
import re

LIMIT = 32768
VERSION = re.compile(r'\d{1,3}\.\d{1,3}\.\d{1,4}')


def read_bounded(path):
    with path.open('rb') as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('Oversize release metadata')
    return data


def plain(value, maximum):
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and value == value.strip()
        and not any(ord(char) < 32 and char not in '\n\t' for char in value)
        and '<' not in value
        and '>' not in value
    )


def snapshot(resources=None):
    root = (
        pathlib.Path(resources)
        if resources is not None
        else pathlib.Path(__file__).resolve().parent
    )
    version = 'development'
    try:
        value = plistlib.loads(read_bounded(root.parent / 'Info.plist')).get(
            'CFBundleShortVersionString'
        )
        version = value if isinstance(value, str) and VERSION.fullmatch(value) else 'unknown'
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        pass
    result = {'installedVersion': version, 'release': None}
    try:
        notes = json.loads(read_bounded(root / 'release-notes.json'))
        if not isinstance(notes, dict) or set(notes) != {'id', 'version', 'title', 'highlights'}:
            return result
        if (
            notes['version'] != version
            or not VERSION.fullmatch(version)
            or not isinstance(notes['id'], str)
            or not re.fullmatch(r'[a-zA-Z0-9._-]{1,100}', notes['id'])
            or not plain(notes['title'], 120)
            or not isinstance(notes['highlights'], list)
            or not 1 <= len(notes['highlights']) <= 12
        ):
            return result
        for item in notes['highlights']:
            if (
                not isinstance(item, dict)
                or set(item) != {'title', 'detail'}
                or not plain(item['title'], 100)
                or not plain(item['detail'], 600)
            ):
                return result
        result['release'] = notes
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        pass
    return result
