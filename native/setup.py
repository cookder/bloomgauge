"""Local-only first launch. Completing setup never enables model control."""

import os
import platform
import time


def earnings_connection(earnings, login_present, now):
    """Expose actionable, non-sensitive setup states without guessing auth failures."""
    updated = earnings.get('updatedAt')
    fresh = type(updated) in (int, float) and 0 <= now - updated <= 60
    if earnings.get('status') == 'ok' and fresh:
        return {'status': 'connected', 'detail': 'Connected to your existing Darkbloom login.'}
    if not login_present:
        return {
            'status': 'sign_in_required',
            'detail': 'Sign in using Darkbloom on this Mac. Credentials stay on your Mac.',
        }
    if earnings.get('error') == 'Login expired. Sign in again using Darkbloom.':
        return {
            'status': 'sign_in_required',
            'detail': 'Darkbloom could not authorize this login. Sign in again using Darkbloom on this Mac.',
        }
    if earnings.get('status') == 'connecting':
        return {'status': 'connecting', 'detail': 'Login found. Checking the earnings connection…'}
    if updated is not None:
        return {
            'status': 'stale',
            'detail': 'The last earnings reading is out of date. BloomGauge is reconnecting automatically; check your internet connection if this continues.',
        }
    return {
        'status': 'unavailable',
        'detail': 'The earnings connection is not confirmed yet. BloomGauge is retrying automatically; check your internet connection if this continues.',
    }


class Setup:
    def __init__(self, history, home):
        self.h, self.home = history, home
        # Run before Optimizer creates its default settings. An existing install
        # must keep its mode, plan, data and tariff when this feature arrives.
        if self.h.cache('setup-v1') is None:
            existing = bool(self.h.cache('optimizer-settings') or self.h.cache('account'))
            self.h.cache(
                'setup-v1',
                {
                    'completed': existing,
                    'existingInstall': existing,
                    'completedAt': time.time() if existing else None,
                },
            )

    def status(self, collector, remote=False):
        state = self.h.cache('setup-v1')
        if remote:
            return {'completed': state['completed'], 'localOnly': True}
        with collector.lock:
            hardware = dict(collector.hardware)
            earnings = dict(collector.earnings)
            provider = (collector.snapshot or {}).get('provider', {})
        with collector.optimizer.lock:
            mode = collector.optimizer.state['mode']
        version = platform.mac_ver()[0]
        compatible = (
            platform.machine() == 'arm64' and bool(version) and int(version.split('.')[0]) >= 14
        )
        login_present = (self.home / '.darkbloom/auth_token').is_file()
        connection = earnings_connection(earnings, login_present, time.time())
        return {
            **state,
            'localOnly': False,
            'compatible': compatible,
            'macOS': version,
            'architecture': platform.machine(),
            'chip': hardware.get('chip'),
            'memoryGB': hardware.get('memoryTotalGB'),
            'providerInstalled': os.access(self.home / '.darkbloom/bin/darkbloom', os.X_OK),
            'loginPresent': login_present,
            'earningsConnected': connection['status'] == 'connected',
            'earningsConnection': connection,
            'providerOnline': bool(provider.get('online')),
            'mode': mode,
            'tariff': collector.energy.tariff(),
        }

    def complete(self, data):
        if (
            type(data) is not dict
            or data.get('understood') is not True
            or data != {'action': 'complete', 'understood': True}
        ):
            raise ValueError('Confirm observation mode before opening the dashboard.')
        with self.h.lock:
            state = self.h.cache('setup-v1')
            if not state['completed']:
                state = {**state, 'completed': True, 'completedAt': time.time()}
                self.h.cache('setup-v1', state)
        return state
