#!/bin/sh
# One-time setup, only after explicit approval. No provider changes or purge here.
# Grants the invoking Mac account exactly /usr/sbin/purge with NO arguments.
set -eu
if [ "$(/usr/bin/id -u)" != 0 ] || [ -z "${SUDO_USER:-}" ] || [ -z "${SUDO_UID:-}" ]; then
  echo 'Run this setup with sudo from your normal Mac account.' >&2
  exit 1
fi
case "$SUDO_USER" in *[!A-Za-z0-9_.-]*|'') echo 'Unsupported account name.' >&2; exit 1;; esac
if [ "$(/usr/bin/id -u "$SUDO_USER")" != "$SUDO_UID" ] || [ "$SUDO_UID" = 0 ]; then
  echo 'The invoking Mac account could not be verified.' >&2
  exit 1
fi
policy_dir=/private/etc/sudoers.d
policy_file="$policy_dir/bloom-dashboard-cache"
if ! /usr/bin/grep -Eq '^([#@]includedir)[[:space:]]+(/private)?/etc/sudoers\.d[[:space:]]*$' /private/etc/sudoers; then
  echo 'The standard sudoers include directory is not configured; no changes made.' >&2
  exit 1
fi
if [ -L "$policy_dir" ]; then echo 'Unexpected sudoers directory link.' >&2; exit 1; fi
/bin/mkdir -p "$policy_dir"
if [ "$(/usr/bin/stat -f '%u' "$policy_dir")" != 0 ]; then echo 'The policy directory is not root-owned.' >&2; exit 1; fi
case "$(/usr/bin/stat -f '%OLp' "$policy_dir")" in 700|750|755) ;; *) echo 'Unsafe policy directory permissions.' >&2; exit 1;; esac
if [ -e "$policy_file" ] || [ -L "$policy_file" ]; then
  echo 'A cache policy already exists; leaving it unchanged for review.' >&2
  exit 1
fi
temp_file=$(/usr/bin/mktemp "$policy_dir/bloom-cache.XXXXXX")
trap '/bin/rm -f "$temp_file"' EXIT HUP INT TERM
printf '%s ALL=(root) NOPASSWD: /usr/sbin/purge ""\n' "$SUDO_USER" > "$temp_file"
/usr/sbin/chown root:wheel "$temp_file"
/bin/chmod 0440 "$temp_file"
/usr/sbin/visudo -cf "$temp_file"
# Exclusive create prevents overwriting an existing administrator policy.
/bin/ln "$temp_file" "$policy_file"
if ! /usr/sbin/visudo -cf /private/etc/sudoers; then
  /bin/rm -f "$policy_file"
  echo 'Validation failed; the new policy was removed.' >&2
  exit 1
fi
echo 'Enabled passwordless execution of /usr/sbin/purge with no arguments for your Mac account.'
echo 'No cache was cleared and no model was switched by this setup.'
