#!/usr/bin/env python3
"""Build-only, checksum-pinned Sparkle acquisition. Never exports signing keys."""

import hashlib, json, os, shutil, stat, subprocess, sys, urllib.request, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT.parent / '.build'


def main():
    lock = json.loads((ROOT / 'sparkle-lock.json').read_text())
    downloads = BUILD / 'sparkle-download'
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / ('Sparkle-' + lock['version'] + '.zip')
    if not archive.exists():
        partial = archive.with_suffix('.partial')
        with (
            urllib.request.urlopen(lock['url'], timeout=60) as response,
            partial.open('wb') as output,
        ):
            shutil.copyfileobj(response, output)
        if hashlib.sha256(partial.read_bytes()).hexdigest() != lock['sha256']:
            raise SystemExit('Sparkle checksum mismatch; no archive extracted.')
        partial.replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != lock['sha256']:
        raise SystemExit('Sparkle checksum mismatch; no archive extracted.')
    destination = BUILD / 'sparkle'
    if '--verify' in sys.argv:
        if not destination.is_dir():
            raise SystemExit('Prepare the pinned Sparkle runtime first.')
        with zipfile.ZipFile(archive) as source:
            for entry in source.infolist():
                if entry.is_dir():
                    continue
                target = destination / entry.filename
                expected = source.read(entry)
                actual = (
                    os.readlink(target).encode()
                    if stat.S_ISLNK(entry.external_attr >> 16)
                    else target.read_bytes()
                )
                if actual != expected:
                    raise SystemExit(
                        'Prepared Sparkle differs from pinned archive: ' + entry.filename
                    )
        print('Prepared Sparkle matches the pinned upstream archive.')
        return
    # Verify every path and Unix link before ditto preserves the framework links.
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            target = (destination / entry.filename).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise SystemExit('Unsafe Sparkle archive path')
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                link = (target.parent / source.read(entry).decode()).resolve()
                if not link.is_relative_to(destination.resolve()):
                    raise SystemExit('Unsafe Sparkle archive link')
            elif mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise SystemExit('Unexpected Sparkle archive entry')
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir()
    subprocess.run(['/usr/bin/ditto', '-x', '-k', str(archive), str(destination)], check=True)
    (destination / 'provenance.json').write_text(json.dumps(lock, indent=2) + '\n')
    print(
        json.dumps(
            {'version': lock['version'], 'sha256': lock['sha256'], 'destination': str(destination)}
        )
    )


if __name__ == '__main__':
    main()
