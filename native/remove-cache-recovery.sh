#!/bin/sh
# Remove only BloomGauge's exact one-command rule. Never rewrite another policy.
set -eu
if [ "$(/usr/bin/id -u)" != 0 ] || [ -z "${SUDO_USER:-}" ] || [ -z "${SUDO_UID:-}" ]; then
  echo 'Run with sudo from your normal Mac account.' >&2; exit 1
fi
case "$SUDO_USER" in *[!A-Za-z0-9_.-]*|'') exit 1;; esac
if [ "$(/usr/bin/id -u "$SUDO_USER")" != "$SUDO_UID" ] || [ "$SUDO_UID" = 0 ]; then exit 1; fi
policy_file=/private/etc/sudoers.d/bloom-dashboard-cache
if [ -L /private/etc/sudoers.d ] || [ -L "$policy_file" ]; then echo 'Unexpected policy link; no changes made.' >&2; exit 1; fi
if [ ! -f "$policy_file" ]; then echo 'No BloomGauge cache policy is installed.'; exit 0; fi
expected=$(printf '%s ALL=(root) NOPASSWD: /usr/sbin/purge ""' "$SUDO_USER")
if [ "$(/bin/cat "$policy_file")" != "$expected" ] || [ "$(/usr/bin/stat -f '%u' "$policy_file")" != 0 ]; then
  echo 'The policy differs from BloomGauge’s rule. Ask your administrator to review it; no changes made.' >&2; exit 1
fi
/bin/rm "$policy_file"
/usr/sbin/visudo -cf /private/etc/sudoers
echo 'Removed BloomGauge’s no-password cache permission. No cache or model was changed.'
