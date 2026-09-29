#!/usr/bin/env python3
"""Explicit one-time storage migration. Not imported or run by the dashboard.

Quit BloomGauge before installing an update and invoking this tool. The SQLite backup
API includes committed WAL records; a pinned read transaction verifies every
table against the same snapshot. The source is retained and an existing
destination is never replaced. No privacy settings or privileges are changed.
"""

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile


def fingerprint(db):
    schema = db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'
    ).fetchall()
    digest = hashlib.sha256(repr(schema).encode())
    tables = {}
    for kind, name, _, _ in schema:
        if kind != 'table':
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        count = 0
        # backup() retains row order, including rowid-less tables. Frame rows
        # with their byte lengths so values cannot collide at row boundaries.
        for row in db.execute('SELECT * FROM ' + quoted):
            encoded = repr(row).encode()
            digest.update(str(len(encoded)).encode() + b':' + encoded)
            count += 1
        tables[name] = count
    return {'sha256': digest.hexdigest(), 'tables': tables}


def migrate(source, destination):
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if os.path.lexists(destination):
        raise FileExistsError('Destination already exists; refusing to overwrite saved history.')
    database = source / 'history.sqlite3'
    if not database.is_file():
        raise FileNotFoundError('Source history.sqlite3 is missing; no migration was performed.')
    allowed = {
        'history.sqlite3',
        'history.sqlite3-wal',
        'history.sqlite3-shm',
        'remote-access.json',
    }
    if any(p.name not in allowed for p in source.iterdir()):
        raise ValueError('Source contains additional files; review them before migrating.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.bloom-migration-', dir=destination.parent))
    try:
        copied = staging / database.name
        copied.touch(mode=0o600)
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as original:
            original.execute('PRAGMA query_only=ON')
            original.execute('BEGIN')
            expected = fingerprint(original)
            if not expected['tables']:
                raise ValueError('Source history has no tables.')
            with closing(sqlite3.connect(copied)) as target:
                original.backup(target)
                if target.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise ValueError('Copied history failed its integrity check.')
                if fingerprint(target) != expected:
                    raise ValueError('Copied history does not match the source snapshot.')
        phone = source / 'remote-access.json'
        if phone.exists():
            config = phone.read_bytes()
            if not isinstance(json.loads(config), dict):
                raise ValueError('Phone configuration must be an object.')
            private_copy = staging / phone.name
            private_copy.touch(mode=0o600)
            private_copy.write_bytes(config)
        # Check again after copying. os.rename also refuses a nonempty target.
        if os.path.lexists(destination):
            raise FileExistsError('Destination appeared during migration; refusing to replace it.')
        staging.rename(destination)
        return {
            **expected,
            'verified': True,
            'phoneConfigPreserved': phone.exists(),
            'sourceRetained': True,
        }
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-directory', required=True, type=Path)
    parser.add_argument(
        '--to-directory',
        type=Path,
        default=Path.home() / 'Library/Application Support/Bloom Dashboard',
    )
    args = parser.parse_args()
    print(json.dumps(migrate(args.from_directory, args.to_directory), indent=2))


if __name__ == '__main__':
    main()
