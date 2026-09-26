"""Optional usage reporting adapter; never controls the provider or optimizer."""

import json
import math
import os
from pathlib import Path
import platform
import plistlib
import threading
import time

from usage_reporting import UsageReporter, STATE_FILENAME

ROOT = Path(__file__).resolve().parent
INVITATION_KEY = 'usage-invitation-v1'
INVITATION_DELAY = 10 * 60


def source_version():
    """The version in release-notes.json, for runs from source without an Info.plist."""
    try:
        return json.loads((ROOT / 'release-notes.json').read_text())['version']
    except (OSError, ValueError, KeyError, TypeError):
        return '0.0.0'


class UsageIntegration:
    def __init__(self, collector, data_path=None, network_enabled=True):
        self.collector = collector
        self.action_lock = threading.RLock()
        self.version = source_version()
        try:
            self.version = plistlib.loads((ROOT.parent / 'Info.plist').read_bytes())[
                'CFBundleShortVersionString'
            ]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.reporter = None
        self.invitation_path = None
        self.invitation_enabled = network_enabled is True
        try:
            directory = (
                Path(data_path).parent / 'usage'
                if data_path and str(data_path) != ':memory:'
                else None
            )
            self.invitation_path = directory / STATE_FILENAME if directory is not None else None
            self.reporter = UsageReporter(
                directory, self.version, metadata=self.metadata(), network_enabled=network_enabled
            )
        except Exception:
            # Optional reporting must never prevent the app or provider from running.
            pass

    def metadata(self):
        with self.collector.lock:
            hardware = dict(self.collector.hardware)
        try:
            os_major = int(platform.mac_ver()[0].split('.')[0])
        except (ValueError, IndexError):
            os_major = 0
        chip = hardware.get('chip')
        chip = chip.removeprefix('Apple ') if isinstance(chip, str) else 'Other'
        memory = hardware.get('memoryTotalGB')
        band = 'unknown'
        if (
            isinstance(memory, (int, float))
            and not isinstance(memory, bool)
            and math.isfinite(memory)
            and memory > 0
        ):
            band = next(
                (
                    label
                    for bound, label in (
                        (16, 'up-to-16'),
                        (32, '17-32'),
                        (64, '33-64'),
                        (128, '65-128'),
                    )
                    if memory <= bound
                ),
                'over-128',
            )
        # The reporter validates these three coarse fields against its allowlist.
        return {'osMajor': os_major, 'chipFamily': chip, 'memoryBand': band}

    def status(self, remote=False):
        safe = {
            'enabled': False,
            'deletionPending': False,
            'sending': False,
            'consentSaved': True,
            'lastSentAt': None,
            'lastError': None,
        }
        try:
            if self.reporter is None:
                raise RuntimeError()
            state = self.reporter.status()
            pending = bool(state.get('deletionPending'))
            error = state.get('error')
            last_sent = state.get('lastSentAt')
            last_sent = (
                last_sent
                if type(last_sent) in (int, float) and math.isfinite(last_sent) and last_sent >= 0
                else None
            )
            safe.update(
                enabled=state.get('enabled') is True,
                deletionPending=pending,
                sending=state.get('busy') is True,
                lastSentAt=last_sent,
            )
            if error == 'opt_out_not_saved':
                safe['consentSaved'] = False
                safe['lastError'] = (
                    'Sharing is stopped for this session, but Bloomkeeper could not save that choice. Keep Bloomkeeper open and retry before quitting.'
                )
            elif error:
                safe['lastError'] = (
                    'Deletion is pending. Retry when this Mac is connected.'
                    if pending
                    else 'Usage sharing is temporarily unavailable. Your Bloomkeeper features are unchanged.'
                )
        except Exception:
            safe['lastError'] = (
                'Usage sharing is unavailable. Your Bloomkeeper features are unchanged.'
            )
        return {
            'schema': 1,
            'appVersion': self.version,
            'localOnly': bool(remote),
            **safe,
            'invitationEligible': self.invitation_eligible(remote),
            'invitationOffered': False,
        }

    def invitation_eligible(self, remote=False):
        """Fail closed on unknown choices. This check never creates analytics state."""
        if (
            remote
            or not self.invitation_enabled
            or self.invitation_path is None
            or self.reporter is None
        ):
            return False
        try:
            # A saved OFF file includes past opt-outs. Do not interpret it as
            # missing consent or ask again, even after deletion has completed.
            try:
                os.lstat(self.invitation_path)
                return False
            except FileNotFoundError:
                pass
            status = self.reporter.status()
            if status.get('enabled') or status.get('deletionPending') or status.get('error'):
                return False
            with self.collector.history.lock:
                if self.collector.history.cache(INVITATION_KEY) is not None:
                    return False
                setup = self.collector.history.cache('setup-v1') or {}
            at = setup.get('completedAt')
            return (
                setup.get('completed') is True
                and type(at) in (int, float)
                and math.isfinite(at)
                and 0 <= at <= time.time() - INVITATION_DELAY
            )
        except Exception:
            return False

    def offer_invitation(self):
        # Persist before returning permission to display. Concurrent windows,
        # relaunches, upgrades and lost responses cannot cause a repeat prompt.
        with self.collector.history.lock:
            if not self.invitation_eligible():
                return self.status()
            self.collector.history.cache(INVITATION_KEY, {'schema': 1, 'state': 'shown'})
        return {**self.status(), 'invitationOffered': True}

    def observe(self):
        """Read only coarse local state, and schedule work off the caller's thread."""
        try:
            reporter = self.reporter
            if reporter is None:
                return
            if reporter.status().get('enabled'):
                reporter.update_metadata(self.metadata())
                setup = self.collector.history.cache('setup-v1') or {}
                with self.collector.optimizer.lock:
                    mode = self.collector.optimizer.state.get('mode')
                with self.collector.lock:
                    earnings = self.collector.earnings
                    connection_error = earnings.get('status') in ('missing', 'stale') and bool(
                        earnings.get('error')
                    )
                reporter.observe(
                    setup_completed=setup.get('completed') is True,
                    setup_error='connection' if connection_error else 'none',
                    optimizer_used=mode in ('week', 'optimize', 'combo', 'demand'),
                )
            reporter.tick()
        except Exception:
            # Never propagate an analytics/storage/transport error into app work.
            pass

    def action(self, data, remote=False):
        with self.action_lock:
            return self._action(data, remote)

    def _action(self, data, remote=False):
        if type(data) is not dict or type(data.get('action')) is not str:
            raise ValueError('Choose a valid usage-sharing action.')
        action = data['action']
        expected = {'action', 'enabled'} if action == 'consent' else {'action'}
        if set(data) != expected or action not in (
            'consent',
            'retry-delete',
            'dashboard-opened',
            'offer-invitation',
            'dismiss-invitation',
        ):
            raise ValueError('Choose a valid usage-sharing action.')
        if remote and action != 'dashboard-opened':
            raise PermissionError('Manage usage sharing on the Mac.')
        if action == 'consent' and type(data['enabled']) is not bool:
            raise ValueError('Choose whether to share optional usage.')
        if self.reporter is None:
            raise RuntimeError('Usage sharing is unavailable.')
        if action == 'offer-invitation':
            return self.offer_invitation()
        if action == 'dismiss-invitation':
            self.collector.history.cache(INVITATION_KEY, {'schema': 1, 'state': 'declined'})
        elif action == 'consent':
            self.reporter.update_metadata(self.metadata())
            self.reporter.set_consent(data['enabled'])
            self.observe()
        elif action == 'retry-delete':
            self.reporter.retry_delete()
        else:
            # Setup/background polling must not imply that a dashboard was opened.
            setup = self.collector.history.cache('setup-v1') or {}
            if setup.get('completed') is True:
                self.reporter.record('dashboardOpened')
                if remote:
                    self.reporter.record('phoneUsed')
        return self.status(remote)

    def close(self):
        try:
            if self.reporter is not None:
                self.reporter.close()
        except Exception:
            pass
