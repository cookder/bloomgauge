"""File-cache cleanup for Darkbloom #743: before each switch, and for cold memory-blocked loads."""

import math, subprocess


class CacheRecoveryError(Exception):
    def __init__(self, message, *, code='cache-command'):
        super().__init__(message)
        self.code = code


def file_cache_blocked(raw, hardware, required):
    """A cold model, almost no MLX allocations, and cache-sized memory deficit.

    Cached pages are only a trigger, never treated as confirmed free memory.
    The caller retains provider/session, idle, power and thermal checks.
    """

    def valid(x):
        return (
            isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0
        )

    available = hardware.get('memoryAvailableGB')
    cached = hardware.get('cachedFilesGB')
    capacity = raw.get('capacity') or {}
    active = capacity.get('gpu_memory_active_gb')
    pool = capacity.get('gpu_memory_cache_gb')
    if not all(valid(x) for x in (available, cached, required, active, pool)):
        return False
    return (
        not raw.get('warm_models')
        and raw.get('inference_active') is False
        and active + pool < 1
        and available < required
        and cached >= max(2, required - available)
    )


def clear_file_cache(runner=subprocess.run):
    """Fixed OS command, no arguments/input/password prompts or shell."""
    try:
        result = runner(
            ['/usr/bin/sudo', '-n', '/usr/sbin/purge'],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise CacheRecoveryError(
            'Cache cleanup could not finish. Check the Mac; no forced restart was sent.'
        ) from None
    if result.returncode != 0:
        raise CacheRecoveryError(
            'Automatic cache cleanup needs authorization on the Mac. Your manual sudo purge workaround remains available.',
            code='cache-permission',
        )
