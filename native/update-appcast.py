#!/usr/bin/env python3
"""Create a checked, versioned Sparkle payload; never uploads or exports keys."""

import argparse, base64, datetime, hashlib, json, plistlib, re, subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

SOURCE = Path(__file__).resolve().parent.parent
SPARKLE = 'http://www.andymatuschak.org/xml-namespaces/sparkle'
ET.register_namespace('sparkle', SPARKLE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dmg', type=Path, required=True)
    parser.add_argument('--app', type=Path, default=SOURCE / '.build/beta/Bloomkeeper Beta.app')
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New evidence directory')
    parser.add_argument(
        '--candidate',
        action='store_true',
        help='Review-only unsigned-by-Apple candidate; never publish this payload',
    )
    args = parser.parse_args()
    dmg = args.dmg.resolve()
    app = args.app.resolve()
    out = args.output.resolve()
    assert dmg.is_file() and not args.dmg.is_symlink()
    digest = hashlib.sha256(dmg.read_bytes()).hexdigest()
    report = json.loads(args.report.read_text())
    assert report['localChecksPassed'] is True and report['gates']['developerId'] is True
    assert report['artifact']['sha256'] == digest, (
        'Release report does not describe these exact bytes'
    )
    receipt = dmg.with_suffix('.notarization.json')
    if not args.candidate:
        r = json.loads(receipt.read_text())
        assert not receipt.is_symlink() and r['status'] == 'Accepted' and r['verified'] is True
        assert r['stapledSha256'] == digest and report['gates']['notarized'] is True
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    config = json.loads((SOURCE / 'native/update-public.json').read_text())
    assert info['CFBundleIdentifier'] == 'local.bloom.dashboard.beta'
    assert info['SUFeedURL'] == config['feedURL'] == 'https://bloomformac.com/updates/beta.xml'
    assert info['SUPublicEDKey'] == config['publicEDKey']
    version = info['CFBundleShortVersionString']
    build = info['CFBundleVersion']
    assert re.fullmatch(
        'Bloomkeeper-' + re.escape(version) + r'-beta[0-9]+-Apple-Silicon\.dmg', dmg.name
    )
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(dmg)], check=True)
    signed = subprocess.run(
        [
            str(SOURCE / '.build/sparkle/bin/sign_update'),
            '--account',
            config['keychainAccount'],
            '-p',
            str(dmg),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    signature = signed.stdout.strip()
    assert len(base64.b64decode(signature, validate=True)) == 64
    # Independently verify against the public key actually embedded in the app.
    subprocess.run(
        [
            str(app / 'Contents/Resources/python/bin/python3'),
            '-I',
            '-B',
            '-c',
            'import sys,base64,pathlib;sys.path.insert(0,sys.argv[1]);from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey;Ed25519PublicKey.from_public_bytes(base64.b64decode(sys.argv[2])).verify(base64.b64decode(sys.argv[3]),pathlib.Path(sys.argv[4]).read_bytes())',
            str(app / 'Contents/Resources/push_vendor'),
            config['publicEDKey'],
            signature,
            str(dmg),
        ],
        check=True,
    )
    url = 'https://bloomformac.com/downloads/' + dmg.name
    rss = ET.Element('rss', {'version': '2.0'})
    channel = ET.SubElement(rss, 'channel')
    ET.SubElement(channel, 'title').text = 'Bloomkeeper beta updates'
    ET.SubElement(channel, 'link').text = config['feedURL']
    ET.SubElement(channel, 'description').text = 'Updates for Bloomkeeper Beta on Apple Silicon.'
    ET.SubElement(channel, 'language').text = 'en'
    item = ET.SubElement(channel, 'item')
    ET.SubElement(item, 'title').text = 'Bloomkeeper ' + version
    ET.SubElement(item, 'pubDate').text = datetime.datetime.now(datetime.timezone.utc).strftime(
        '%a, %d %b %Y %H:%M:%S GMT'
    )
    ET.SubElement(item, '{' + SPARKLE + '}version').text = build
    ET.SubElement(item, '{' + SPARKLE + '}shortVersionString').text = version
    ET.SubElement(item, '{' + SPARKLE + '}minimumSystemVersion').text = info[
        'LSMinimumSystemVersion'
    ]
    ET.SubElement(
        item, 'description'
    ).text = 'Arrange the Earnings dashboard with separate phone and desktop layouts saved in each browser. Compare experimental one-hour earnings baselines with clearer evidence, saved forecast windows and local accuracy tracking. Optimizer trial reviews now close unresolvable comparisons at their deadline, require a supported paid benchmark and explain the paid alternative; confirmation can pause through a brief data gap without counting unseen time.\n\nManual controls start or switch to the model you select. Eligible Manual starts and Optimizer on can try one cache cleanup and remeasure memory before loading. On shows preparation progress, and Manual cancels remaining activation. Optional cleanup permission is enabled or removed through macOS administrator approval in the installed Mac app.\n\nFree reporting, Manual controls, existing settings/history, optional usage sharing and the ready-based 30-day optimizer trial are retained. Forecasts are experimental and advisory, remain on this Mac, and do not drive automatic switches or promise future income. The private phone dashboard updates with its Mac host.\n\nThe previous report-delivery repair is included. Earlier unconfirmed reports are not recovered or retried automatically; update, review and send the problem again.'
    ET.SubElement(item, 'link').text = 'https://bloomformac.com/#release'
    ET.SubElement(
        item,
        'enclosure',
        {
            'url': url,
            'length': str(dmg.stat().st_size),
            'type': 'application/octet-stream',
            '{' + SPARKLE + '}edSignature': signature,
        },
    )
    ET.indent(rss, space='  ')
    out.mkdir(parents=True, exist_ok=False)
    filename = 'beta.candidate.xml' if args.candidate else 'beta.xml'
    ET.ElementTree(rss).write(out / filename, encoding='utf-8', xml_declaration=True)
    assert hashlib.sha256(dmg.read_bytes()).hexdigest() == digest, 'Artifact changed while signing'
    payload = {
        'schema': 'bloom-update-payload-v1',
        'publicationReady': not args.candidate,
        'candidateOnly': args.candidate,
        'version': version,
        'build': build,
        'feedURL': config['feedURL'],
        'enclosureURL': url,
        'file': dmg.name,
        'bytes': dmg.stat().st_size,
        'sha256': digest,
        'edSignature': signature,
        'publicEDKey': config['publicEDKey'],
        'signatureIndependentlyVerified': True,
        'xml': filename,
        'xmlSha256': hashlib.sha256((out / filename).read_bytes()).hexdigest(),
        'privateKeysExported': False,
        'notarizedAndStapled': not args.candidate,
    }
    (out / 'payload.json').write_text(json.dumps(payload, indent=2) + '\n')
    print(json.dumps(payload))


if __name__ == '__main__':
    main()
