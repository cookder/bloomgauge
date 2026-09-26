Apple SMC read layout and M5 sensor mappings are adapted from Stats by Serhiy Mytrovtsiy (MIT).
Source: https://github.com/exelban/stats (SMC/smc.swift and Modules/Sensors/values.swift).
License: native/STATS_LICENSE. Dashboard only implements sensor reads, with no fan or power controls.
The UI uses the dependencies and licenses listed in package.json and pnpm-lock.yaml.

Portable beta runtime: CPython 3.12.14 from astral-sh/python-build-standalone (September 1, 2026 arm64 build), including OpenSSL and SQLite. The verified release archives are pinned in native/runtime-lock.json. Bundles include python-licenses/ with upstream component license texts and python/lib/python3.12/LICENSE.txt.

Web Push: exact package versions and licenses in native/PUSH_DEPENDENCIES.md and push-requirements.txt; packaged metadata and license texts remain in push_vendor/. The beta rebuilds extensions for Python 3.12. No owner credentials or generated VAPID keys are bundled.

The packaged THIRD_PARTY_WEB.txt collects installed web dependency license/notice texts (including build dependencies). runtime-provenance.json records the archive and built wheel hashes; build-manifest.json inventories files before signing. This is a release inventory, not a completed commercial provenance audit.

Updates: Sparkle 2.10.0 (Sparkle Project), MIT license and included component notices in SPARKLE_LICENSE. Official source: https://github.com/sparkle-project/Sparkle . native/sparkle-lock.json pins the official release archive and upstream Swift Package Manager SHA256. The bundle includes sparkle-provenance.json and the framework; build-only signing tools and private update keys are not distributed. Bloomkeeper omits the optional XPC services because its app is not sandboxed. Updates use Sparkle's standard permission and installation UI; unattended installation and system profiling are disabled.
