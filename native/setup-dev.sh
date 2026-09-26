#!/bin/zsh
# Install the pinned Web Push dependencies into native/push_vendor so the
# collector and tests can run from source with the system Python.
# Release builds do not use this folder; prepare-runtime.py builds their own.
set -euo pipefail
native_dir="$(cd "$(dirname "$0")" && pwd)"
rm -rf "$native_dir/push_vendor"
/usr/bin/python3 -m pip install --quiet --disable-pip-version-check --no-compile \
    --target "$native_dir/push_vendor" -r "$native_dir/push-requirements.txt"
rm -rf "$native_dir/push_vendor/bin"
echo "Installed development dependencies into native/push_vendor."
