"""Read-only proof of the existing, exact passwordless cache permission."""

import re
import subprocess
import time


def cache_permission(runner=subprocess.run):
    status = 'unknown'
    detail = 'Cache permission could not be verified. Check permission on this Mac before cleanup.'
    try:
        result = runner(
            ['/usr/bin/sudo', '-n', '-ll', '/usr/sbin/purge'],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C', 'LANG': 'C'},
        )
        if result.returncode != 0:
            status = 'required'
            detail = 'One-time administrator approval on this Mac is required for unattended file-cache cleanup.'
        elif isinstance(result.stdout, str):
            # A successful listing alone can use cached password authentication.
            # Require one exact no-argument command and an explicit NOPASSWD tag.
            entries = re.split(r'(?m)^Sudoers entry:[^\n]*\n', result.stdout)
            if len(entries) == 2 and not entries[0].strip():
                body = entries[1]
                users = re.search(r'(?m)^\s*RunAsUsers:\s*(.*?)\s*$', body)
                options = re.search(r'(?m)^\s*Options:\s*(.*?)\s*$', body)
                commands = re.search(r'(?ms)^\s*Commands:\s*\n(.*?)^\s*Matched:', body)
                matched = re.search(r'(?m)^\s*Matched:\s*(.*?)\s*$', body)
                flags = {x.strip() for x in options[1].split(',')} if options else set()
                lines = [line.strip() for line in body.splitlines() if line.strip()]
                shape_ok = (
                    len(lines) == 5
                    and lines[0] == 'RunAsUsers: root'
                    and lines[1].startswith('Options: ')
                    and lines[2] == 'Commands:'
                    and lines[3] == '/usr/sbin/purge ""'
                    and lines[4] == 'Matched: /usr/sbin/purge'
                )
                exact = bool(
                    shape_ok
                    and users
                    and users[1] == 'root'
                    and commands
                    and commands[1].strip() == '/usr/sbin/purge ""'
                    and matched
                    and matched[1] == '/usr/sbin/purge'
                )
                if exact and '!authenticate' in flags and 'authenticate' not in flags:
                    status = 'ready'
                    detail = 'This Mac permits unattended file-cache cleanup using only purge with no arguments.'
                elif exact:
                    status = 'required'
                    detail = 'One-time administrator approval on this Mac is required for unattended file-cache cleanup.'
    except (OSError, subprocess.SubprocessError):
        pass
    return {'status': status, 'detail': detail, 'checkedAt': time.time()}
