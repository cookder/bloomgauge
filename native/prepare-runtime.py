#!/usr/bin/env python3
"""Build-time only: acquire the pinned, relocatable Apple Silicon runtime."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent
BUILD = ROOT.parent / '.build'


def main():
    lock = json.loads((ROOT / 'runtime-lock.json').read_text())
    downloads = BUILD / 'runtime-download'
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / 'python.tar.gz'
    if not archive.exists():
        with urllib.request.urlopen(lock['python']['url'], timeout=60) as response:
            with archive.open('wb') as output:
                shutil.copyfileobj(response, output)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != lock['python']['sha256']:
        raise SystemExit('Python archive checksum mismatch. Remove the cached archive and retry.')
    destination = BUILD / 'portable-runtime'
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir()
    with tarfile.open(archive) as source:
        # The pinned upstream archive has relative in-tree symlinks. Reject
        # absolute / escaping members and links before extracting on Python3.9.
        for member in source.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination.resolve()) or member.isdev():
                raise SystemExit('Unsafe runtime archive entry')
            if member.issym() or member.islnk():
                link = (
                    (target.parent if member.issym() else destination) / member.linkname
                ).resolve()
                if not link.is_relative_to(destination.resolve()):
                    raise SystemExit('Unsafe runtime link')
        source.extractall(destination)
    python = destination / 'python/bin/python3'
    wheels = downloads / 'wheels'
    wheels.mkdir(exist_ok=True)
    subprocess.run(
        [
            str(python),
            '-m',
            'pip',
            'wheel',
            '--wheel-dir',
            str(wheels),
            '--only-binary=cryptography,cffi,aiohttp,frozenlist,multidict,propcache,yarl,charset-normalizer',
            '-r',
            str(ROOT / lock['requirements']),
        ],
        check=True,
    )
    subprocess.run(
        [
            str(python),
            '-m',
            'pip',
            'install',
            '--no-index',
            '--no-compile',
            '--find-links',
            str(wheels),
            '--target',
            str(destination / 'push_vendor'),
            '-r',
            str(ROOT / lock['requirements']),
        ],
        check=True,
    )
    shutil.rmtree(destination / 'push_vendor/bin', ignore_errors=True)
    # No package manager is needed on customer machines. Retain Python's license
    # and the upstream bundled library notices, along with all dependency notices.
    for path in (destination / 'python/lib/python3.12/site-packages').glob('pip*'):
        if path.is_dir():
            shutil.rmtree(path)
    for path in (destination / 'python/bin').glob('pip*'):
        path.unlink()
    for path in destination.rglob('__pycache__'):
        shutil.rmtree(path)
    license_archive = downloads / 'python-full.tar.zst'
    if not license_archive.exists():
        with urllib.request.urlopen(lock['licenses']['url'], timeout=60) as response:
            with license_archive.open('wb') as output:
                shutil.copyfileobj(response, output)
    if hashlib.sha256(license_archive.read_bytes()).hexdigest() != lock['licenses']['sha256']:
        raise SystemExit('Python license archive checksum mismatch.')
    # Node is a build tool only. Its zstd decoder avoids another release dependency.
    unpacked = downloads / 'python-full.tar'
    subprocess.run(
        [
            os.environ.get('BLOOM_BUILD_NODE', 'node'),
            '-e',
            "const fs=require('node:fs');fs.writeFileSync(process.argv[2],require('node:zlib').zstdDecompressSync(fs.readFileSync(process.argv[1])));",
            str(license_archive),
            str(unpacked),
        ],
        check=True,
    )
    licenses = destination / 'python-licenses'
    licenses.mkdir()
    with tarfile.open(unpacked) as source:
        for member in source.getmembers():
            if member.name.startswith('python/licenses/') and member.isfile():
                (licenses / Path(member.name).name).write_bytes(source.extractfile(member).read())
    (destination / 'provenance.json').write_text(
        json.dumps(
            {
                **lock,
                'wheels': [
                    {'file': p.name, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                    for p in sorted(wheels.glob('*.whl'))
                ],
            },
            indent=2,
        )
        + '\n'
    )
    print(destination)


if __name__ == '__main__':
    main()
