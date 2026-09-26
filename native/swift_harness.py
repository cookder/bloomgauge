"""Test helper, not bundled: compile a Swift test harness once per source.

Each harness took ~15 s to compile on every run. Binaries are cached by a hash of
the source and the compiler version, so an unchanged App.swift reuses them.
"""

import hashlib
import os
import pathlib
import subprocess
import tempfile

CACHE = pathlib.Path(tempfile.gettempdir()) / 'bloom-swift-tests'
_version = None


def compile_swift(source, timeout=120):
    """Returns the path of an executable built from source (a string)."""
    global _version
    if _version is None:
        _version = subprocess.run(
            ['/usr/bin/swiftc', '--version'], capture_output=True, text=True, timeout=60
        ).stdout
    key = hashlib.sha256((_version + '\0' + source).encode()).hexdigest()[:24]
    binary = CACHE / key
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
    CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=CACHE) as tmp:
        swift, out = pathlib.Path(tmp) / 'main.swift', pathlib.Path(tmp) / 'main'
        swift.write_text(source)
        result = subprocess.run(
            [
                '/usr/bin/swiftc',
                '-module-cache-path',
                str(CACHE / 'modules'),
                str(swift),
                '-o',
                str(out),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        os.replace(out, binary)
    return binary
