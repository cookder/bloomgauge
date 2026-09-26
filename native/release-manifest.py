"""Allowlisted release metadata and privacy checks (build-time only)."""

import base64, hashlib, json, plistlib, re, sys
from pathlib import Path

# The builder's own home folder must never reach a shipped UI asset.
HOME = re.escape(str(Path.home()))

app = Path(sys.argv[1])
channel = sys.argv[2]
beta = channel == 'beta'
resources = app / 'Contents/Resources'
# Include licenses from installed web dependencies, not application/user files.
sections = []
for metadata in sorted(
    (Path(__file__).resolve().parent.parent / 'node_modules/.pnpm').glob(
        '*/node_modules/**/package.json'
    )
):
    try:
        d = json.loads(metadata.read_text())
    except Exception:
        continue
    if not d.get('name') or not d.get('version'):
        continue
    notices = [
        p
        for p in metadata.parent.iterdir()
        if p.is_file()
        and p.name.lower().split('.')[0] in ('license', 'licence', 'copying', 'notice')
    ]
    if notices:
        sections.append(
            d['name']
            + ' '
            + d['version']
            + '\n'
            + '\n'.join(p.read_text(errors='replace') for p in notices)
        )
(resources / 'THIRD_PARTY_WEB.txt').write_text(
    'Web dependency notices (includes build tools)\n\n'
    + '\n\n---\n\n'.join(dict.fromkeys(sections))
)
name = 'Bloomkeeper Beta' if beta else 'Bloomkeeper'
# Personal builds share the current release version so the installed app never looks older.
version = '1.36.57'
build = '13657'
# The personal edition adds the forecast lab (installation.personal_edition).
(resources / 'product-config.json').write_text(
    json.dumps({'edition': 'free' if beta else 'personal'}, indent=2) + '\n'
)
plist = {
    'CFBundleName': name,
    'CFBundleDisplayName': name,
    'CFBundleIdentifier': 'local.bloom.dashboard.beta' if beta else 'local.bloom.dashboard',
    'CFBundleExecutable': 'BloomDashboardBeta' if beta else 'BloomDashboard',
    'CFBundleIconFile': 'Bloom.icns',
    'CFBundlePackageType': 'APPL',
    'CFBundleShortVersionString': version,
    'CFBundleVersion': build,
    'LSMinimumSystemVersion': '14.0',
    'NSHighResolutionCapable': True,
    'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
    'NSHumanReadableCopyright': 'Bloomkeeper. Includes third-party software; see Third Party Notices.',
    # The data folder keeps the pre-rename name so history and settings carry over.
    'BloomDataDirectory': 'Bloom Dashboard Beta' if beta else 'Bloom Dashboard',
    'BloomReleaseChannel': channel,
}
if beta:
    updates = json.loads((Path(__file__).resolve().parent / 'update-public.json').read_text())
    assert updates['feedURL'] == 'https://bloomformac.com/updates/beta.xml'
    assert len(base64.b64decode(updates['publicEDKey'], validate=True)) == 32
    # Omitting SUEnableAutomaticChecks preserves Sparkle's standard permission
    # prompt. Checking is optional; every installation requires user approval.
    plist.update(
        SUFeedURL=updates['feedURL'],
        SUPublicEDKey=updates['publicEDKey'],
        SUAllowsAutomaticUpdates=False,
        SUAutomaticallyUpdate=False,
        SUEnableSystemProfiling=False,
        SUVerifyUpdateBeforeExtraction=True,
        SUEnableJavaScript=False,
        SUScheduledCheckInterval=21600,
    )
(app / 'Contents/Info.plist').write_bytes(plistlib.dumps(plist))
blocked = re.compile(
    '(^|/)(auth_token|daemon-state\\.json|remote-access\\.json|community-insights\\.json|web-push\\.json|usage-reporting\\.json|\\.usage-reporting\\.lock|\\.usage-reporting-[^/]+\\.tmp|\\.env|\\.DS_Store)$|\\.(sqlite3?|db|pem|key)(-|$)'
)
files = []
for p in sorted(app.rglob('*')):
    if p.is_symlink():
        if not p.resolve().is_relative_to(app.resolve()):
            raise SystemExit('External link in bundle: ' + str(p.relative_to(app)))
        continue
    if not p.is_file():
        continue
    rel = str(p.relative_to(app))
    if blocked.search(rel) and rel != 'Contents/Resources/push_vendor/certifi/cacert.pem':
        raise SystemExit('Private/runtime file found: ' + rel)
    # CA roots are intentionally bundled; no customer certificate or key is.
    if '/web/' in rel and p.suffix in ('.js', '.html', '.css'):
        body = p.read_text()
        # This exact contact link was explicitly approved for public support.
        body = body.replace('mailto:support@bloomkeeper.io', '')
        if re.search(HOME + r'\b|[A-Za-z0-9._%+-]+@gmail\.com|https://[^\s\"\']+\.ts\.net', body):
            raise SystemExit('Personal data found in UI asset')
    files.append(
        {
            'path': rel,
            'bytes': p.stat().st_size,
            'sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
        }
    )
(resources / 'build-manifest.json').write_text(
    json.dumps(
        {'version': version, 'channel': channel, 'hashes': 'before code signing', 'files': files},
        indent=2,
    )
    + '\n'
)
