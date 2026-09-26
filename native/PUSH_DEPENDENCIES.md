# Web Push runtime dependencies

Bloomkeeper packages the pinned Python dependencies in `push_vendor/` for the beta's bundled Python 3.12 runtime on Apple Silicon. Metadata and license files remain in that folder. The original native/push_vendor directory is the legacy personal build dependency; portable builds use .build/portable-runtime/push_vendor. `push-requirements.txt` is the exact version manifest. No dependencies are downloaded by the running app.

The Web Push implementation uses pywebpush, py-vapid, http-ece and cryptography for standard AES128GCM payload encryption and VAPID signatures. Upstream source: https://github.com/web-push-libs/pywebpush and https://github.com/web-push-libs/vapid. pywebpush 2.1.2 is the newest release accepting this Python runtime; later releases require Python 3.10. Dependency versions were resolved September 13, 2026.

The portable runtime bundles OpenSSL and certifi trust roots. Certificate verification remains enabled. Bloomkeeper supplies its own restricted, certificate-verifying stdlib HTTPS transport to pywebpush rather than using urllib3. An isolated sender process has a 15-second total deadline, including DNS; socket timeout is eight seconds. This changes transport restrictions, not cryptography.

Legacy personal Python 3.9 rebuild only (not beta): use `python3 -m pip install --no-compile --only-binary=cryptography,cffi,aiohttp --target native/push_vendor -r native/push-requirements.txt`. The packaging script copies the folder and signs native extension modules. Recreate a clean vendor directory when updating dependencies, then run `python3 -m unittest test_web_push` from `native/` and verify the installed app.

Included packages:

- aiohappyeyeballs 2.6.1: PSF-2.0
- aiohttp 3.13.5: Apache-2.0 AND MIT
- aiosignal 1.4.0: Apache 2.0
- async-timeout 5.0.1: Apache 2
- attrs 26.1.0: MIT
- certifi 2026.7.22: MPL-2.0
- cffi 2.0.0: MIT
- charset-normalizer 3.5.1: MIT
- cryptography 50.0.1: Apache-2.0 OR BSD-3-Clause
- frozenlist 1.8.0: Apache-2.0
- http-ece 1.2.1: MIT
- idna 3.19: BSD-3-Clause
- multidict 6.7.1: Apache License 2.0
- propcache 0.4.1: Apache-2.0
- py-vapid 1.9.4: MPL-2.0
- pycparser 2.23: BSD-3-Clause
- pywebpush 2.1.2: MPL-2.0
- requests 2.32.5: Apache-2.0
- six 1.17.0: MIT
- typing_extensions 4.16.0: PSF-2.0
- urllib3 2.6.3: MIT
- yarl 1.22.0: Apache-2.0

Portable rebuild: `python3 native/prepare-runtime.py` verifies the pinned upstream archive, resolves the exact dependency versions to compatible arm64 wheels, records their SHA-256 hashes, includes runtime licenses, and removes pip from the customer bundle. No dependency downloads occur at app startup. Build with Node 22.15+ on PATH or BLOOM_BUILD_NODE set for the license archive decoder.
