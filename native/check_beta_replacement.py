#!/usr/bin/env python3
"""Check a specific beta replacement/rollback with empty, temporary preview data."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import tempfile

from release_check import Preview, lifecycle


def metadata(app):
    value = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    assert value['CFBundleIdentifier'] == 'local.bloom.dashboard.beta'
    assert value['CFBundleExecutable'] == 'BloomDashboardBeta'
    assert value['BloomDataDirectory'] == 'Bloom Dashboard Beta'
    subprocess.run(
        ['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)],
        check=True,
        capture_output=True,
    )
    return value['CFBundleShortVersionString']


def stored(data):
    with sqlite3.connect(data / 'history.sqlite3') as db:
        schema = db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
        mode = json.loads(
            db.execute("SELECT data FROM cache WHERE key='optimizer-settings'").fetchone()[0]
        )['mode']
        credit = db.execute(
            'SELECT micro_usd FROM opt_credits WHERE account=?', ('fixture-owner',)
        ).fetchall()
        return {'schema': schema, 'mode': mode, 'credit': credit}


def verify(preview):
    assert preview.get('/api/health')['ready'] is True
    setup = preview.get('/api/setup')
    assert setup['completed'] is True and setup['tariff']['rate'] == 0.16
    value = stored(preview.data)
    assert value['mode'] == 'observe' and value['credit'] == [(123456,)]
    return value


def check(previous_dmg, previous_sha256, app):
    assert hashlib.sha256(previous_dmg.read_bytes()).hexdigest() == previous_sha256, (
        'Previous artifact checksum differs'
    )
    with tempfile.TemporaryDirectory(prefix='bloom-beta-replacement-') as temp:
        root = Path(temp)
        mount = root / 'previous-volume'
        mount.mkdir()
        subprocess.run(
            [
                '/usr/bin/hdiutil',
                'attach',
                '-readonly',
                '-nobrowse',
                '-noautoopen',
                '-mountpoint',
                str(mount),
                str(previous_dmg),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        try:
            old = root / 'old' / app.name
            old.parent.mkdir()
            shutil.copytree(mount / app.name, old, symlinks=True)
        finally:
            subprocess.run(
                ['/usr/bin/hdiutil', 'detach', str(mount)],
                check=True,
                capture_output=True,
                timeout=60,
            )
        new = root / 'new' / app.name
        new.parent.mkdir()
        shutil.copytree(app, new, symlinks=True)
        versions = {'previous': metadata(old), 'candidate': metadata(new)}
        run = root / 'runtime'
        run.mkdir()
        before = Preview(old, run)
        after = Preview(new, run)
        try:
            before.start()
            lifecycle(before)
            original = verify(before)
            before.stop()
            # Make a consistent backup before replacement. The tests never use real runtime data.
            with (
                sqlite3.connect(before.data / 'history.sqlite3') as src,
                sqlite3.connect(root / 'pre-update.sqlite3') as backup,
            ):
                src.backup(backup)
            after.start()
            upgraded = verify(after)
            after.stop()
            assert upgraded['schema'] == original['schema'], (
                'Schema changed: no rollback attempt is allowed without an explicit migration plan'
            )
            before.start()
            rolled_back = verify(before)
            assert rolled_back == original
            return {
                'passed': True,
                'versions': versions,
                'dataWasTemporary': True,
                'providerWorkersEnabled': False,
                'setupTariffObservationModeAndCreditPreserved': True,
                'unchangedSchemaVerifiedBeforeRollback': True,
                'previousVersionReopenedCandidateHistory': True,
                'preUpdateBackupCreated': True,
                'automaticUpdaterTested': False,
                'quarantinedInstallTested': False,
                'limit': 'Only this exact beta pair on the development Mac; no general downgrade or independent-Mac claim.',
            }
        finally:
            after.stop()
            before.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous-dmg', type=Path, required=True)
    parser.add_argument('--previous-sha256', required=True)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Choose a new output file; previous evidence is not overwritten')
    try:
        result = check(args.previous_dmg.resolve(), args.previous_sha256, args.app.resolve())
    except Exception as error:
        result = {'passed': False, 'error': str(error)}
    result.update(
        schema='bloom-beta-replacement-v1',
        at=datetime.now(timezone.utc).isoformat(),
        previousArtifactSha256=args.previous_sha256,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    raise SystemExit(0 if result['passed'] else 1)
