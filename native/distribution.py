"""Operator-only Developer ID preflight and resumable DMG notarization.

Credentials stay in Keychain. This tool does not install or publish BloomGauge.
Submission receipts bind Apple's job to exact bytes; a pending job is never
silently resubmitted. Run finish again after Apple has processed the upload.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid


def run(command, timeout=55, json_output=False):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Apple output may contain account details; keep it out of shared logs.
        raise RuntimeError(
            f'{Path(command[0]).name} {command[1]} failed (exit {result.returncode}).'
        )
    return result.stdout if json_output else result.stdout + result.stderr


def preflight(identity, profile=None):
    rows = []
    identities = run(['/usr/bin/security', 'find-identity', '-v', '-p', 'codesigning'])
    matches = re.findall(r'\b([0-9A-Fa-f]{40})\s+"(Developer ID Application:[^"\n]+)"', identities)
    valid = any(identity in (fingerprint, name) for fingerprint, name in matches)
    rows.append({'check': 'Developer ID Application identity', 'passed': valid})
    for tool in ('notarytool', 'stapler'):
        try:
            run(['/usr/bin/xcrun', '--find', tool])
            found = True
        except (RuntimeError, subprocess.TimeoutExpired):
            found = False
        rows.append({'check': tool, 'passed': found})
    if profile:
        try:
            run(
                [
                    '/usr/bin/xcrun',
                    'notarytool',
                    'history',
                    '--keychain-profile',
                    profile,
                    '--output-format',
                    'json',
                ]
            )
            authenticated = True
        except (RuntimeError, subprocess.TimeoutExpired):
            authenticated = False
        rows.append({'check': 'Keychain notarization authentication', 'passed': authenticated})
    else:
        rows.append({'check': 'Keychain notarization profile configured', 'passed': False})
    return {
        'checks': rows,
        'ready': all(row['passed'] for row in rows),
        'validDeveloperIdIdentities': len(matches),
    }


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def signature(dmg):
    run(['/usr/bin/codesign', '--verify', '--strict', str(dmg)])
    details = run(['/usr/bin/codesign', '-dv', '--verbose=4', str(dmg)])
    if 'Authority=Developer ID Application:' not in details:
        raise RuntimeError(
            'The DMG must be signed with Developer ID Application before submission.'
        )


def receipt_path(dmg):
    return dmg.with_suffix('.notarization.json')


def save_receipt(path, data, *, create=False):
    if path.is_symlink():
        raise RuntimeError('Refusing a symlink submission receipt.')
    if create:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        temporary = None
    else:
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if temporary:
            os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def valid_job(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def submit(dmg, profile):
    receipt = receipt_path(dmg)
    if receipt.exists() or receipt.is_symlink():
        raise RuntimeError(
            'A submission receipt already exists. Use finish; do not rebuild or resubmit this artifact.'
        )
    signature(dmg)
    data = {
        'schema': 'bloom-notarization-v1',
        'artifact': dmg.name,
        'submittedSha256': sha256(dmg),
        'submissionId': None,
        'status': 'Upload started',
        'startedAt': datetime.now(timezone.utc).isoformat(),
        'verified': False,
    }
    # Preserve uncertainty if the upload succeeds but its response is lost.
    save_receipt(receipt, data, create=True)
    output = run(
        [
            '/usr/bin/xcrun',
            'notarytool',
            'submit',
            str(dmg),
            '--keychain-profile',
            profile,
            '--output-format',
            'json',
            '--no-wait',
        ],
        timeout=300,
        json_output=True,
    )
    response = json.loads(output)
    job = response.get('id', '')
    if not valid_job(job):
        raise RuntimeError(
            'No valid submission ID returned. Check Apple history before attempting another upload.'
        )
    data.update(submissionId=job, status='Submitted')
    save_receipt(receipt, data)
    return {
        'status': 'Submitted',
        'submissionId': job,
        'receipt': receipt.name,
        'next': 'Run finish with the same DMG and Keychain profile after Apple processes it.',
    }


def finish(dmg, profile):
    receipt = receipt_path(dmg)
    if receipt.is_symlink():
        raise RuntimeError('Refusing a symlink submission receipt.')
    data = json.loads(receipt.read_text())
    if data.get('schema') != 'bloom-notarization-v1' or data.get('artifact') != dmg.name:
        raise RuntimeError('Submission receipt does not match this artifact.')
    if sha256(dmg) not in (data.get('submittedSha256'), data.get('stapledSha256')):
        raise RuntimeError('DMG changed since submission. Do not staple or publish this artifact.')
    job = data.get('submissionId')
    if not valid_job(job):
        raise RuntimeError(
            'Upload outcome is uncertain. Inspect notarytool history and reconcile its receipt before retrying.'
        )
    signature(dmg)
    response = json.loads(
        run(
            [
                '/usr/bin/xcrun',
                'notarytool',
                'info',
                job,
                '--keychain-profile',
                profile,
                '--output-format',
                'json',
            ],
            json_output=True,
        )
    )
    if response.get('id') != job:
        raise RuntimeError('Apple returned a different submission ID.')
    data['status'] = response.get('status', 'Unknown')
    save_receipt(receipt, data)
    if data['status'] != 'Accepted':
        return {'status': data['status'], 'verified': False, 'submissionId': job}
    run(['/usr/bin/xcrun', 'stapler', 'staple', str(dmg)])
    data['stapledSha256'] = sha256(dmg)
    save_receipt(receipt, data)
    run(['/usr/bin/xcrun', 'stapler', 'validate', str(dmg)])
    run(['/usr/bin/codesign', '--verify', '--strict', str(dmg)])
    run(
        [
            '/usr/sbin/spctl',
            '--assess',
            '--type',
            'open',
            '--context',
            'context:primary-signature',
            str(dmg),
        ]
    )
    data.update(verified=True, verifiedAt=datetime.now(timezone.utc).isoformat())
    save_receipt(receipt, data)
    dmg.with_suffix('.dmg.sha256').write_text(f'{data["stapledSha256"]}  {dmg.name}\n')
    return {
        'status': 'Accepted',
        'verified': True,
        'sha256': data['stapledSha256'],
        'next': 'Run release_check.py against these exact bytes, then test the real downloaded installer on an independent Mac.',
    }


def installer_readme(path, signed):
    text = path.read_text()
    if signed:
        text = text.replace(
            'DEVELOPER PREVIEW. This build is not yet Apple-notarized or approved for public distribution.\n'
            'Do not bypass Gatekeeper to distribute this preview. A signed, notarized beta follows Apple Developer ID setup and independent Mac testing.',
            'PRIVATE BETA CANDIDATE. This app is signed with Apple Developer ID.\n'
            'Your beta organizer must finish notarization and download checks before sharing this installer. '
            'If macOS blocks it, contact the organizer; do not disable security protections.',
        )
        text = text.replace(
            'this preview is not notarized.',
            'Apple notarization does not replace independent device testing.',
        )
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('preflight')
    check.add_argument('--identity', default=os.environ.get('BLOOM_SIGN_IDENTITY', ''))
    check.add_argument('--profile', default=os.environ.get('BLOOM_NOTARY_PROFILE', ''))
    for verb in ('submit', 'finish'):
        command = commands.add_parser(verb)
        command.add_argument('--dmg', type=Path, required=True)
        command.add_argument('--profile', default=os.environ.get('BLOOM_NOTARY_PROFILE', ''))
    notes = commands.add_parser('installer-notes')
    notes.add_argument('--signed', action='store_true')
    notes.add_argument('--source', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'installer-notes':
            print(installer_readme(args.source, args.signed), end='')
            return 0
        if args.command == 'preflight':
            result = preflight(args.identity, args.profile)
            code = 0 if result['ready'] else 2
        else:
            if not args.profile:
                raise RuntimeError(
                    'Set BLOOM_NOTARY_PROFILE to an existing Keychain profile; do not pass a password.'
                )
            if args.dmg.is_symlink() or not args.dmg.is_file() or args.dmg.suffix != '.dmg':
                raise RuntimeError('Choose a regular DMG file, not a symlink.')
            result = (submit if args.command == 'submit' else finish)(
                args.dmg.absolute(), args.profile
            )
            code = (
                0
                if result.get('verified') or args.command == 'submit'
                else 3
                if result['status'] == 'In Progress'
                else 1
            )
        print(json.dumps(result, indent=2))
        return code
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
