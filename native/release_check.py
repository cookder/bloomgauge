#!/usr/bin/env python3
"""Repeatable local release checks. Does not install, publish, or control Darkbloom.

Exit0: local checks passed and signing/notarization passed (external beta remains separate).
Exit1: a local check failed. Exit2: local checks passed; signing/notarization blocked.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from distribution import installer_readme
from personal_data import personal_pattern

SOURCE = Path(__file__).resolve().parent.parent
NATIVE = SOURCE / 'native'


class Checks:
    def __init__(self, output):
        self.output = output
        output.mkdir(parents=True, exist_ok=False)
        self.rows = []

    def command(self, name, command, cwd=SOURCE, timeout=600):
        start = time.monotonic()
        log = self.output / (name + '.log')
        with log.open('w') as stream:
            try:
                result = subprocess.run(
                    [str(x) for x in command],
                    cwd=cwd,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                )
                code = result.returncode
            except subprocess.TimeoutExpired:
                code = 124
            except OSError as error:
                stream.write(str(error) + '\n')
                code = 127
        row = {
            'name': name,
            'status': 'passed' if code == 0 else 'failed',
            'exitCode': code,
            'seconds': round(time.monotonic() - start, 2),
            'log': log.name,
        }
        self.rows.append(row)
        print(json.dumps(row), flush=True)
        return code == 0

    def check(self, name, function):
        try:
            detail = function()
            row = {'name': name, 'status': 'passed', 'detail': detail}
        except Exception as error:
            row = {'name': name, 'status': 'failed', 'error': str(error)}
        self.rows.append(row)
        print(json.dumps(row), flush=True)
        return row['status'] == 'passed'


def artifact(app):
    resources = app / 'Contents/Resources'
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    assert (
        info['CFBundleIdentifier'] == 'local.bloom.dashboard.beta'
        and info['CFBundleExecutable'] == 'BloomDashboardBeta'
    ), 'Not an isolated beta bundle'
    assert info['BloomDataDirectory'] == 'Bloom Dashboard Beta'
    manifest = json.loads((resources / 'build-manifest.json').read_text())
    assert info['CFBundleShortVersionString'] == manifest['version'], 'Version mismatch'
    checked = 0
    for p in resources.glob('*.py'):
        assert (NATIVE / p.name).is_file() and p.read_bytes() == (NATIVE / p.name).read_bytes(), (
            'Stale packaged source: ' + p.name
        )
        checked += 1
    assert (resources / 'diagnostics.py').is_file()
    assert all(
        (resources / name).is_file()
        for name in ('usage_reporting.py', 'usage_integration.py', 'feature_discovery.py')
    )
    assert json.loads((resources / 'product-config.json').read_text()) == {'edition': 'free'}, (
        'Beta must be the free edition'
    )
    for p in (SOURCE / 'dist/local').rglob('*'):
        if p.is_file():
            packaged = resources / 'web' / p.relative_to(SOURCE / 'dist/local')
            assert packaged.is_file() and p.read_bytes() == packaged.read_bytes(), (
                'Stale packaged UI'
            )
    for entry in manifest['files']:
        p = app / entry['path']
        assert p.is_file(), 'Missing packaged resource'
        # Native code changes during signing; verify static resources against build hashes.
        if p.suffix in ('.py', '.js', '.json', '.html', '.css', '.txt', '.md'):
            assert hashlib.sha256(p.read_bytes()).hexdigest() == entry['sha256'], (
                'Static resource hash mismatch'
            )
    personal = personal_pattern(SOURCE)
    blocked = re.compile(
        '(^|/)(auth_token|daemon-state\\.json|remote-access\\.json|community-insights\\.json|web-push\\.json|usage-reporting\\.json|\\.usage-reporting\\.lock|\\.usage-reporting-[^/]+\\.tmp|\\.env|\\.DS_Store)$|\\.(sqlite3?|db|key)(-|$)'
    )
    for p in app.rglob('*'):
        if p.is_symlink():
            assert p.resolve().is_relative_to(app.resolve()), 'External symlink'
        if not p.is_file():
            continue
        rel = str(p.relative_to(app))
        assert not blocked.search(rel), 'Runtime/private file in bundle'
        if (
            p.suffix in ('.py', '.js', '.html', '.json', '.css')
            and p.stat().st_size < 8 * 1024 * 1024
        ):
            text = p.read_text(errors='replace')
            assert not personal.search(text), 'Personal data in distributable text'
    return {
        'version': info['CFBundleShortVersionString'],
        'pythonSourcesMatch': checked,
        'runtimeFiles': len(manifest['files']),
        'privateDataScan': 'passed',
    }


def updater_bundle(app):
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    config = json.loads((NATIVE / 'update-public.json').read_text())
    assert info['SUFeedURL'] == config['feedURL'] and info['SUPublicEDKey'] == config['publicEDKey']
    assert 'SUEnableAutomaticChecks' not in info, 'Standard permission prompt must remain enabled'
    for key in (
        'SUAllowsAutomaticUpdates',
        'SUAutomaticallyUpdate',
        'SUEnableSystemProfiling',
        'SUEnableJavaScript',
    ):
        assert info[key] is False, key + ' must be disabled'
    assert info['SUVerifyUpdateBeforeExtraction'] is True
    assert info['SUScheduledCheckInterval'] == 21600, 'Opt-in beta update checks must use six hours'
    framework = app / 'Contents/Frameworks/Sparkle.framework'
    assert not (framework / 'XPCServices').exists(), 'Unused sandbox services must not be shipped'
    provenance = json.loads((app / 'Contents/Resources/sparkle-provenance.json').read_text())
    assert provenance == json.loads((NATIVE / 'sparkle-lock.json').read_text())
    assert (app / 'Contents/Resources/SPARKLE_LICENSE').read_bytes() == (
        SOURCE / '.build/sparkle/LICENSE'
    ).read_bytes()
    assert (
        plistlib.loads((framework / 'Resources/Info.plist').read_bytes())[
            'CFBundleShortVersionString'
        ]
        == provenance['version']
    )

    def team(path):
        result = subprocess.run(
            ['/usr/bin/codesign', '-dv', '--verbose=4', str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        return re.search(r'^TeamIdentifier=(.*)$', result.stderr, re.M).group(1)

    app_team = team(app)
    for code in (
        framework,
        framework / 'Versions/B/Autoupdate',
        framework / 'Versions/B/Updater.app',
    ):
        subprocess.run(
            ['/usr/bin/codesign', '--verify', '--deep', '--strict', str(code)],
            check=True,
            capture_output=True,
        )
        assert team(code) == app_team, 'Updater helper has a different signing team'
    binary = app / 'Contents/MacOS' / info['CFBundleExecutable']
    linked = subprocess.run(
        ['/usr/bin/otool', '-L', str(binary)], capture_output=True, text=True, check=True
    ).stdout
    assert '@rpath/Sparkle.framework/' in linked
    strings = subprocess.run(
        ['/usr/bin/strings', str(binary)], capture_output=True, text=True, check=True
    ).stdout
    assert 'local.bloom.dashboard.updater-test.' not in strings, (
        'Isolated test code leaked into production build'
    )
    assert 'bloom-update-test-' not in strings
    return {
        'sparkleVersion': provenance['version'],
        'feedURL': config['feedURL'],
        'sameSigningTeam': True,
        'standardPermissionPrompt': True,
        'unattendedInstall': False,
        'systemProfiling': False,
        'verifyBeforeExtraction': True,
        'isolatedTestCodeAbsent': True,
    }


def disk_image_contents(app, dmg):
    with tempfile.TemporaryDirectory(prefix='bloom-dmg-check-') as temp:
        mount = Path(temp) / 'volume'
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
                str(dmg),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        try:
            assert {p.name for p in mount.iterdir()} == {
                app.name,
                'Applications',
                'Read Me First.txt',
                'Beta Test Kit',
            }, 'Unexpected disk image contents'
            assert (mount / 'Applications').is_symlink() and os.readlink(
                mount / 'Applications'
            ) == '/Applications'
            signature = subprocess.run(
                ['/usr/bin/codesign', '-dv', '--verbose=4', str(app)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            signed = (
                signature.returncode == 0
                and 'Authority=Developer ID Application:' in signature.stderr
            )
            readme = (mount / 'Read Me First.txt').read_text()
            assert readme == installer_readme(NATIVE / 'BETA_README.txt', signed), (
                'Stale installation guide'
            )
            version = plistlib.loads((app / 'Contents/Info.plist').read_bytes())[
                'CFBundleShortVersionString'
            ]
            assert re.search(r'\b' + re.escape(version) + r'\b', readme.splitlines()[0]), (
                'Installer guide version differs from app'
            )
            kit = mount / 'Beta Test Kit'
            assert {p.name for p in kit.iterdir()} == {
                'TESTER_GUIDE.md',
                'RESULT_TEMPLATE.json',
                'EARNINGS_TEMPLATE.csv',
            }
            for p in kit.iterdir():
                assert p.read_bytes() == (NATIVE / 'beta' / p.name).read_bytes(), 'Stale beta kit'
            packaged = mount / app.name

            def inventory(root):
                return {
                    str(p.relative_to(root)): ('link', os.readlink(p))
                    if p.is_symlink()
                    else ('file', hashlib.sha256(p.read_bytes()).hexdigest())
                    for p in root.rglob('*')
                    if p.is_file() or p.is_symlink()
                }

            assert inventory(packaged) == inventory(app), 'DMG app differs from checked app'
            return {
                'appBytesMatchCheckedBundle': True,
                'installerAndTesterKitMatchSource': True,
                'installerVersionMatchesBundle': True,
                'onlyAllowlistedRootEntries': True,
            }
        finally:
            subprocess.run(
                ['/usr/bin/hdiutil', 'detach', str(mount)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )


class Preview:
    def __init__(self, app, root):
        self.app = app
        self.root = root
        self.data = root / 'data'
        self.data.mkdir(exist_ok=True)
        self.proc = None

    def start(self):
        resources = self.app / 'Contents/Resources'
        self.errors = (self.root / 'preview-stderr.log').open('a')
        self.proc = subprocess.Popen(
            [
                str(resources / 'python/bin/python3'),
                '-I',
                '-B',
                '-u',
                str(resources / 'runtime-entry.py'),
                '--port',
                '0',
                '--remote-port',
                '0',
                '--static',
                str(resources / 'web'),
                '--data',
                str(self.data / 'history.sqlite3'),
                '--setup-preview',
            ],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=self.errors,
            text=True,
        )
        ready, _, _ = select.select([self.proc.stdout], [], [], 30)
        if not ready:
            raise RuntimeError('Isolated preview did not start')
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError('Preview exited; see private test log')
        self.url = json.loads(line)['url']
        assert self.url.startswith('http://127.0.0.1:') and not self.url.endswith(
            (':8765', ':8766')
        )
        return self

    def get(self, path, data=None, headers=None):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.url + path, data=body, headers=headers or {})
        with opener.open(req, timeout=20) as response:
            return json.load(response)

    def stop(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
            self.proc.stdout.close()
            self.proc = None
        if hasattr(self, 'errors'):
            self.errors.close()


def lifecycle(preview):
    setup = preview.get('/api/setup')
    assert setup['completed'] is False
    discovery = preview.get('/api/discovery')
    assert discovery['available'] is False and discovery['features'] == [], (
        'Setup preview must never show discovery prompts'
    )
    usage = preview.get('/api/usage')
    assert (
        usage['enabled'] is False
        and usage['deletionPending'] is False
        and usage['lastSentAt'] is None
    )
    assert not (preview.data / 'usage/usage-reporting.json').exists(), (
        'Analytics identity created before consent'
    )
    assert not {'analyticsId', 'secret', 'installationId'} & set(usage), (
        'Private identifier exposed by status'
    )
    with sqlite3.connect(preview.data / 'history.sqlite3') as db:
        state = json.loads(
            db.execute("SELECT data FROM cache WHERE key='optimizer-settings'").fetchone()[0]
        )
        assert state['mode'] == 'observe' and not state.get('pending')
    preview.get(
        '/api/setup',
        {'action': 'complete', 'understood': True},
        {'Content-Type': 'application/json', 'X-Bloom-Action': 'setup'},
    )
    usage_headers = {'Content-Type': 'application/json', 'X-Bloom-Action': 'usage'}
    sharing = preview.get('/api/usage', {'action': 'consent', 'enabled': True}, usage_headers)
    assert sharing['enabled'] is True and sharing['lastSentAt'] is None, (
        'Preview must not upload measurements'
    )
    off = preview.get('/api/usage', {'action': 'consent', 'enabled': False}, usage_headers)
    assert off['enabled'] is False and off['deletionPending'] is True, (
        'Offline deletion must remain visibly pending'
    )
    tariff = preview.get('/api/setup')['tariff']
    preview.get(
        '/api/energy/tariff',
        {'rate': 0.16, 'label': 'Beta fixture rate', 'expectedId': tariff['id']},
        {'Content-Type': 'application/json', 'X-Bloom-Action': 'energy-tariff'},
    )
    preview.stop()
    discovery_saved = {
        'schema': 1,
        'healthySeconds': 7200,
        'features': {
            'phone': {'used': False, 'dismissed': True, 'snoozedUntil': 0},
            'optimizer': {'used': False, 'dismissed': False, 'snoozedUntil': time.time() + 86400},
        },
    }
    with sqlite3.connect(preview.data / 'history.sqlite3') as db:
        db.execute(
            'INSERT INTO opt_credits VALUES(?,?,?,?,?,?,?)',
            ('fixture-owner', 1, 'fixture-provider', 100, 'fixture-model', 123456, 20),
        )
        db.execute(
            'INSERT OR REPLACE INTO cache VALUES(?,?,?)',
            ('feature-discovery-v1', time.time(), json.dumps(discovery_saved)),
        )
        db.commit()
    preview.start()
    setup = preview.get('/api/setup')
    assert setup['completed'] is True and setup['tariff']['rate'] == 0.16
    usage = preview.get('/api/usage')
    assert (
        usage['enabled'] is False
        and usage['deletionPending'] is True
        and usage['lastSentAt'] is None
    )
    with sqlite3.connect(preview.data / 'history.sqlite3') as db:
        assert (
            db.execute(
                'SELECT micro_usd FROM opt_credits WHERE account=?', ('fixture-owner',)
            ).fetchone()[0]
            == 123456
        )
        assert (
            json.loads(
                db.execute("SELECT data FROM cache WHERE key='feature-discovery-v1'").fetchone()[0]
            )
            == discovery_saved
        )
    assert preview.get('/api/discovery')['features'] == [], (
        'Preview must remain quiet even with ready saved preferences'
    )
    try:
        preview.get(
            '/api/discovery',
            {'action': 'dismiss', 'feature': 'phone'},
            {'Content-Type': 'application/json', 'X-Bloom-Action': 'discovery'},
        )
    except urllib.error.HTTPError as error:
        assert error.code == 403
    else:
        raise AssertionError('Preview accepted a discovery preference action')
    # Controls stay denied even if an isolated saved setting is accidentally permissive.
    try:
        preview.get(
            '/api/optimizer',
            {'action': 'start'},
            {'Content-Type': 'application/json', 'X-Bloom-Action': 'optimizer'},
        )
    except urllib.error.HTTPError as error:
        assert error.code == 403
    else:
        raise AssertionError('Preview accepted a provider-control action')
    return {
        'newInstallObserveOnly': True,
        'setupTariffCreditHistorySurviveRestart': True,
        'providerControlsDenied': True,
        'usageDefaultsOff': True,
        'usageStatusExcludesIdentifiers': True,
        'offlineUsageDeletionPersists': True,
        'discoveryPreviewSuppressed': True,
        'discoveryPreferencesSurviveRestart': True,
        'actualSleepWakeTested': False,
        'signedUpdaterTested': False,
    }


def main(args):
    out = args.output.resolve()
    checks = Checks(out)
    app = args.app.resolve()
    dmg = args.dmg.resolve()
    node = Path(args.node).resolve()
    runtime = SOURCE / '.build/portable-runtime/python/bin/python3'
    if args.build:
        if not checks.command(
            'typescript', [node, SOURCE / 'node_modules/typescript/bin/tsc', '--noEmit']
        ):
            return finish(checks, app, dmg)
        if not checks.command(
            'ui-build',
            [
                node,
                SOURCE / 'node_modules/vite/bin/vite.js',
                'build',
                '--config',
                'vite.local.config.ts',
            ],
        ):
            return finish(checks, app, dmg)
        if not checks.command('beta-build', [NATIVE / 'package-beta.sh'], timeout=900):
            return finish(checks, app, dmg)
    else:
        checks.command('typescript', [node, SOURCE / 'node_modules/typescript/bin/tsc', '--noEmit'])
    checks.command(
        'javascript-tests',
        [
            node,
            '--test',
            *sorted(NATIVE.glob('test_*.mjs')),
            *sorted((SOURCE / 'lib').glob('*.test.mjs')),
        ],
        timeout=300,
    )
    # Test dlopen under the exact packaged interpreter's signature, not only the
    # unsiged development runtime. This catches ad-hoc/hardened Team ID failures.
    resources = app / 'Contents/Resources'
    checks.command(
        'packaged-cryptography',
        [
            resources / 'python/bin/python3',
            '-I',
            '-B',
            '-c',
            "import sys;sys.path.insert(0,sys.argv[1]);from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey;from pywebpush import webpush;from py_vapid import Vapid;k=Ed25519PrivateKey.generate();s=k.sign(b'packaged-runtime-check');k.public_key().verify(s,b'packaged-runtime-check');print('License and Web Push cryptography load and verify in the signed bundle.')",
            resources / 'push_vendor',
        ],
    )
    # Use the same bundled interpreter/dependencies as customers, isolated from system Python.
    with tempfile.TemporaryDirectory(prefix='bloom-release-tests-') as temp:
        copied = Path(temp) / 'native'
        shutil.copytree(NATIVE, copied, ignore=shutil.ignore_patterns('push_vendor', '__pycache__'))
        shutil.copytree(SOURCE / '.build/portable-runtime/push_vendor', copied / 'push_vendor')
        checks.command(
            'python-tests',
            [
                runtime,
                '-I',
                '-B',
                '-m',
                'unittest',
                'discover',
                '-s',
                copied,
                '-p',
                'test_*.py',
                '-v',
            ],
            cwd=temp,
            timeout=900,
        )
        checks.command(
            'optimizer-replay',
            [
                runtime,
                '-B',
                copied / 'replay_optimizer.py',
                '--output',
                out / 'optimizer-replay.json',
            ],
            cwd=temp,
        )
    okay = checks.check('artifact-contents', lambda: artifact(app))
    okay = checks.check('updater-bundle', lambda: updater_bundle(app)) and okay
    okay = (
        checks.command(
            'bundle-signature', ['/usr/bin/codesign', '--verify', '--deep', '--strict', app]
        )
        and okay
    )
    checks.command('disk-image-integrity', ['/usr/bin/hdiutil', 'verify', dmg])
    checks.check('disk-image-contents', lambda: disk_image_contents(app, dmg))
    if okay:
        with tempfile.TemporaryDirectory(prefix='bloom-isolated-preview-') as temp:
            root = Path(temp)
            relocated = root / app.name
            shutil.copytree(app, relocated, symlinks=True)
            preview = Preview(relocated, root)
            try:

                def start_preview():
                    assert preview.start().get('/api/health')['ready'] is True, (
                        'Preview is not ready'
                    )
                    return {'urlIsIsolated': True, 'ready': True}

                started = checks.check('relocated-startup', start_preview)
                if started and preview.proc and preview.proc.poll() is None:
                    checks.check('persistent-first-launch', lambda: lifecycle(preview))
                    browser = [
                        node,
                        NATIVE / 'release-browser.mjs',
                        '--url',
                        preview.url,
                        '--output',
                        out / 'browser',
                        '--playwright',
                        args.playwright,
                    ]
                    if args.chromium:
                        browser.extend(['--chromium', args.chromium])
                    checks.command('browser-checks', browser, timeout=600)
            finally:
                preview.stop()
                if (root / 'preview-stderr.log').exists():
                    shutil.copy2(root / 'preview-stderr.log', out / 'preview-stderr.log')
    return finish(checks, app, dmg)


def finish(checks, app, dmg):
    signed = False
    notarized = False
    if app.is_dir():
        signature = subprocess.run(
            ['/usr/bin/codesign', '-dv', '--verbose=4', str(app)], capture_output=True, text=True
        )
        signed = (
            signature.returncode == 0 and 'Authority=Developer ID Application:' in signature.stderr
        )
    if signed and dmg.is_file():
        result = subprocess.run(
            ['/usr/bin/xcrun', 'stapler', 'validate', str(dmg)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        notarized = result.returncode == 0
        if notarized:
            notarized = checks.command(
                'gatekeeper-disk-image',
                [
                    '/usr/sbin/spctl',
                    '--assess',
                    '--type',
                    'open',
                    '--context',
                    'context:primary-signature',
                    dmg,
                ],
            )
            notarized = (
                checks.command(
                    'gatekeeper-application',
                    ['/usr/sbin/spctl', '--assess', '--type', 'execute', app],
                )
                and notarized
            )
    required = {
        'typescript',
        'javascript-tests',
        'python-tests',
        'optimizer-replay',
        'artifact-contents',
        'bundle-signature',
        'packaged-cryptography',
        'disk-image-integrity',
        'disk-image-contents',
        'relocated-startup',
        'persistent-first-launch',
        'browser-checks',
        'updater-bundle',
    }
    passed = {row['name'] for row in checks.rows if row['status'] == 'passed'}
    local = required <= passed and all(row['status'] == 'passed' for row in checks.rows)
    result = {
        'schema': 'bloom-release-check-v1',
        'at': datetime.now(timezone.utc).isoformat(),
        'localChecksPassed': local,
        'requiredChecks': sorted(required),
        'missingOrFailedChecks': sorted(required - passed),
        'checks': checks.rows,
        'artifact': {
            'name': dmg.name,
            'sha256': hashlib.sha256(dmg.read_bytes()).hexdigest() if dmg.is_file() else None,
        },
        'gates': {
            'developerId': signed,
            'notarized': notarized,
            'independentMacBeta': 'not_run',
            'physicalPhone': 'not_run',
            'actualSleepWake': 'not_run',
            'signedUpdateAndRollback': 'separate_isolated_test_required',
            'upstreamCommercialClearance': 'not_verified',
        },
        'readyForCommercialRelease': False,
        'externalBetaCompleted': False,
    }
    (checks.output / 'release-report.json').write_text(json.dumps(result, indent=2) + '\n')
    print(
        json.dumps(
            {
                'localChecksPassed': local,
                'developerId': signed,
                'notarized': notarized,
                'commercialReleaseReady': False,
                'report': str(checks.output / 'release-report.json'),
            }
        ),
        flush=True,
    )
    return 1 if not local else 0 if signed and notarized else 2


def release_image():
    """The current beta's disk image name, from release-notes.json (as package-beta.sh)."""
    notes = json.loads((NATIVE / 'release-notes.json').read_text())
    return f"Bloomkeeper-{notes['version']}-{notes['id'].rsplit('-', 1)[1]}-Apple-Silicon.dmg"


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--output',
        type=Path,
        required=True,
        help='New directory; previous evidence is never overwritten',
    )
    parser.add_argument('--node', required=True)
    parser.add_argument(
        '--playwright', required=True, help='Path to installed Playwright index.mjs'
    )
    parser.add_argument('--chromium', help='Optional installed Chromium/Chrome executable')
    parser.add_argument('--app', type=Path, default=SOURCE / '.build/beta/Bloomkeeper Beta.app')
    parser.add_argument(
        '--dmg',
        type=Path,
        default=SOURCE / '.build/releases' / release_image(),
    )
    parser.add_argument('--build', action='store_true')
    sys.exit(main(parser.parse_args()))
