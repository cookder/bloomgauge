"""Which alerts reach this Mac and the owner's phones, and when (notification settings).

One saved preference (history cache 'notification-settings'): per alert kind a Mac and a
phone switch, the "earnings running high" threshold, one daily quiet window and whether
phone notifications may show amounts. The phone switch for network news is the catalog
watch's own push setting (/api/network/news), kept in step here.
"""

import copy
import math
import re
import time

from web_push import SCREENS

KINDS = (
    'earningsHigh',  # confirmed live pace at or above a threshold for a sustained time
    'switchConsidered',  # the manager starts watching a model, and when it switches to it
    'modelSwitched',  # every confirmed model change (the phone push that already existed)
    'problems',  # no work arriving, model recovery notices; also sent in quiet hours
    'demandSpike',  # demand_alerts.py spikes (in-app only before this)
    'networkNews',  # model_catalog_watch.py: a new model that pays well
)
CHANNELS = ('mac', 'phone')
CACHE_KEY = 'notification-settings'

DEFAULTS = {
    'kinds': {
        'earningsHigh': {'mac': True, 'phone': True},
        'switchConsidered': {'mac': True, 'phone': True},
        'modelSwitched': {'mac': True, 'phone': True},
        'problems': {'mac': True, 'phone': True},
        'demandSpike': {'mac': False, 'phone': False},
        'networkNews': {'mac': False, 'phone': False},
    },
    # Andrew's example (Sep 28): above $0.30/h for a material time. Home pays ~$0.10/ready-h.
    'earnings': {'usdPerHour': 0.30, 'minutes': 30},
    'quietHours': {'enabled': False, 'start': '22:00', 'end': '07:00'},
    # Andrew (Sep 28): amounts on the phone too. Web Push payloads are encrypted end to end
    # (Apple/Google relay them unread); the lock screen is the exposure, so it can be turned off.
    'phoneAmounts': True,
}
LIMITS = {
    'usdPerHour': {'min': 0.01, 'max': 10, 'step': 0.01},
    'minutes': {'min': 10, 'max': 240, 'step': 5},
}
URGENT = frozenset(('problems',))  # still sent during quiet hours


def clock_minutes(value):
    """'HH:MM' -> minutes after midnight, or None."""
    if not isinstance(value, str) or not re.fullmatch(r'\d{2}:\d{2}', value):
        return None
    hours, minutes = int(value[:2]), int(value[3:])
    return hours * 60 + minutes if hours < 24 and minutes < 60 else None


def merged(current, change):
    """Pure: `current` with a partial `change` applied; ValueError names the bad field."""
    if not isinstance(change, dict) or set(change) - {
        'kinds',
        'earnings',
        'quietHours',
        'phoneAmounts',
    }:
        raise ValueError('Unknown notification setting.')
    out = copy.deepcopy(current)
    kinds = change.get('kinds', {})
    if not isinstance(kinds, dict) or set(kinds) - set(KINDS):
        raise ValueError('Unknown alert type.')
    for kind, channels in kinds.items():
        if (
            not isinstance(channels, dict)
            or set(channels) - set(CHANNELS)
            or any(type(v) is not bool for v in channels.values())
        ):
            raise ValueError('Choose on or off for each alert.')
        out['kinds'][kind].update(channels)
    earnings = change.get('earnings', {})
    if not isinstance(earnings, dict) or set(earnings) - {'usdPerHour', 'minutes'}:
        raise ValueError('Unknown earnings alert setting.')
    if 'usdPerHour' in earnings:
        value = earnings['usdPerHour']
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not LIMITS['usdPerHour']['min'] <= value <= LIMITS['usdPerHour']['max']
        ):
            raise ValueError('Choose an hourly amount from $0.01 to $10.')
        out['earnings']['usdPerHour'] = round(float(value), 2)
    if 'minutes' in earnings:
        value = earnings['minutes']
        if (
            type(value) is not int
            or not LIMITS['minutes']['min'] <= value <= LIMITS['minutes']['max']
            or value % LIMITS['minutes']['step']
        ):
            raise ValueError('Choose 10 to 240 minutes, in steps of 5.')
        out['earnings']['minutes'] = value
    quiet = change.get('quietHours', {})
    if not isinstance(quiet, dict) or set(quiet) - {'enabled', 'start', 'end'}:
        raise ValueError('Unknown quiet hours setting.')
    if 'enabled' in quiet:
        if type(quiet['enabled']) is not bool:
            raise ValueError('Choose on or off for quiet hours.')
        out['quietHours']['enabled'] = quiet['enabled']
    for key in ('start', 'end'):
        if key in quiet:
            if clock_minutes(quiet[key]) is None:
                raise ValueError('Use a time like 22:00 for quiet hours.')
            out['quietHours'][key] = quiet[key]
    if out['quietHours']['start'] == out['quietHours']['end']:
        raise ValueError('Pick different start and end times for quiet hours.')
    if 'phoneAmounts' in change:
        if type(change['phoneAmounts']) is not bool:
            raise ValueError('Choose on or off for amounts on the phone.')
        out['phoneAmounts'] = change['phoneAmounts']
    return out


def quiet_now(settings, now):
    """True inside the daily quiet window (local time; a window may cross midnight)."""
    quiet = settings.get('quietHours') or {}
    start, end = clock_minutes(quiet.get('start')), clock_minutes(quiet.get('end'))
    if not quiet.get('enabled') or start is None or end is None or start == end:
        return False
    local = time.localtime(now)
    minute = local.tm_hour * 60 + local.tm_min
    return start <= minute < end if start < end else minute >= start or minute < end


class NotificationSettings:
    def __init__(self, history, catalog=None):
        self.h = history
        self.catalog = catalog  # model_catalog_watch.ModelCatalogWatch (network news push)

    def saved(self):
        """The stored preference over the defaults; anything unreadable falls back."""
        out = copy.deepcopy(DEFAULTS)
        try:
            stored = self.h.cache(CACHE_KEY)
        except (ValueError, TypeError):
            stored = None
        if isinstance(stored, dict):
            for part in ('kinds', 'earnings', 'quietHours', 'phoneAmounts'):
                try:
                    out = merged(out, {part: stored[part]}) if part in stored else out
                except (ValueError, TypeError, KeyError, AttributeError):
                    pass  # one bad part (an older or edited file) keeps its default
        return out

    def get(self):
        out = self.saved()
        catalog = self.catalog_enabled()
        news = out['kinds']['networkNews']
        if catalog is False:
            out['kinds']['networkNews'] = {'mac': False, 'phone': False}
        elif catalog is True and not (news['mac'] or news['phone']):
            # Turned on from the network news card: that switch is the phone one.
            out['kinds']['networkNews'] = {'mac': False, 'phone': True}
        return out

    def catalog_enabled(self):
        if self.catalog is None:
            return None
        try:
            return bool(self.catalog.push_status()['enabled'])
        except Exception:
            return None

    def save(self, change):
        out = merged(self.get(), change)
        news = out['kinds']['networkNews']
        wanted = news['mac'] or news['phone']
        enabled = self.catalog_enabled()
        # First, so that a failure there saves nothing; unknown (None) is left alone.
        if enabled is not None and enabled is not wanted:
            self.catalog.set_push({'action': 'push', 'enabled': wanted})
        with self.h.lock:
            self.h.cache(CACHE_KEY, out)
        return self.get()
