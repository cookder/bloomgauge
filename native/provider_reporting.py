"""Read-only aggregate reporting for model sets Bloomkeeper does not control.

Never grants optimizer eligibility, runs a decode, or changes the provider.
Counter deltas belong to the whole set; only paid credits identify a model.
"""

import math


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def observed_models(values):
    if (
        not isinstance(values, list)
        or not 1 <= len(values) <= 64
        or any(not isinstance(v, str) or not v or v != v.strip() or len(v) > 512 for v in values)
        or len(set(values)) != len(values)
    ):
        return []
    return sorted(values)


def loaded_models(raw, selected):
    """Offered models that are loaded now. Darkbloom loads the rest when requests
    arrive, and most Macs can't hold a large set at once, so the loaded subset is
    what serves; any change to it starts a new measurement segment."""
    warm = observed_models(raw.get('warm_models'))
    return [m for m in selected if m in warm]


def reporting_scope(account, raw, now):
    """Fresh local warm identity only; this does not prove serving output."""
    if not isinstance(account, str) or not account or not isinstance(raw, dict) or not finite(now):
        return None
    selected = observed_models(raw.get('advertised_models'))
    written = raw.get('written_at')
    key = raw.get('attestation_public_key')
    if (
        len(selected) < 3
        or not finite(written)
        or not -5 < now - written < 15
        or not isinstance(key, str)
        or not key
        or type(raw.get('pid')) is not int
        or raw['pid'] <= 0
        or not finite(raw.get('started_at'))
        or not 0 <= raw['started_at'] <= written
    ):
        return None
    trust = raw.get('trust')
    if not isinstance(trust, dict) or trust.get('status') != 'online':
        return None
    loaded = loaded_models(raw, selected)
    if not loaded:
        return None
    slots = raw.get('slots')
    if slots is not None:
        if not isinstance(slots, list) or any(not isinstance(slot, dict) for slot in slots):
            return None
        for model in loaded:
            matches = [slot for slot in slots if slot.get('model') == model]
            if (
                len(matches) != 1
                or not matches[0].get('kv_backend')
                or matches[0].get('load_error')
            ):
                return None
    stats = raw.get('stats')
    counters = (
        tuple(stats.get(k) for k in ('requests_served', 'tokens_generated'))
        if isinstance(stats, dict)
        else ()
    )
    if len(counters) != 2 or any(not finite(v) or v < 0 or int(v) != v for v in counters):
        return None
    return account, key, raw['pid'], raw['started_at'], tuple(selected), tuple(loaded)


class ReportingIdentity:
    """In-memory, exact 3+ model roster proof, independent of control identity."""

    def __init__(self):
        self.proof = None
        self.previous_proof = None

    def revoke(self):
        self.proof = None
        self.previous_proof = None

    def confirm(self, account, raw, now, provider, confirmed_at):
        scope = reporting_scope(account, raw, now)
        if (
            scope is None
            or not isinstance(provider, str)
            or not provider.strip()
            or not finite(confirmed_at)
            or not 0 <= now - confirmed_at < 180
        ):
            self.revoke()
            return
        previous = self.proof
        if previous and previous['scope'] == scope and previous['provider'] == provider:
            if confirmed_at < previous['at']:
                return
            self.previous_proof = previous
        else:
            self.previous_proof = None
        self.proof = {'scope': scope, 'provider': provider, 'at': confirmed_at}

    def match(self, account, raw, now):
        proof = self.proof
        if proof is None or not finite(now):
            self.revoke()
            return None
        # A collector sample may have captured its clock just before a newer
        # same-scope roster response. Use only its still-valid preceding proof;
        # never borrow the newer timestamp or revoke proof using an older sample.
        if now < proof['at']:
            previous = self.previous_proof
            if (
                previous
                and 0 <= now - previous['at'] < 180
                and reporting_scope(account, raw, now) == previous['scope']
            ):
                return previous['provider']
            return None
        if now - proof['at'] >= 180 or reporting_scope(account, raw, now) != proof['scope']:
            self.revoke()
            return None
        return proof['provider']


class ProviderReporting:
    def __init__(self):
        self.previous = None
        self.verified_at = None

    def reset(self):
        self.previous = None
        self.verified_at = None

    def observe(
        self, account, raw, now, identity_verified=False, pending=False, roster_provider=None
    ):
        selected = observed_models(raw.get('advertised_models'))
        result = {
            'counting': False,
            'status': 'paused',
            'models': selected,
            'scope': 'aggregate',
            'managedBy': 'darkbloom',
        }

        def pause(detail):
            self.reset()
            return {**result, 'detail': detail}

        written = raw.get('written_at')
        if len(selected) < 3 or not finite(written) or not -5 < now - written < 15:
            return pause('Statistics paused · waiting for fresh multi-model readings.')
        if (
            not account
            or not raw.get('attestation_public_key')
            or type(raw.get('pid')) is not int
            or raw['pid'] <= 0
            or not finite(raw.get('started_at'))
            or not 0 <= raw['started_at'] <= written
        ):
            return pause(
                'Statistics paused · matching this Mac and its selected models to the provider roster.'
            )
        if pending:
            return pause('Statistics paused · model switching, loading or pre-warming.')
        trust = raw.get('trust')
        if not isinstance(trust, dict) or trust.get('status') != 'online':
            return pause('Statistics paused · provider is not ready to serve.')
        loaded = loaded_models(raw, selected)
        if not loaded:
            return pause(
                f'Statistics paused · none of the {len(selected)} models your provider offers '
                'are loaded yet.'
            )
        # Older providers omit slots. If present, an unloaded/failed slot is
        # stronger evidence than an out-of-date warm_models list.
        slots = raw.get('slots')
        if slots is not None:
            if not isinstance(slots, list) or any(not isinstance(s, dict) for s in slots):
                return pause('Statistics paused · model slot readiness is unavailable.')
            for model in loaded:
                matches = [s for s in slots if s.get('model') == model]
                if (
                    len(matches) != 1
                    or not matches[0].get('kv_backend')
                    or matches[0].get('load_error')
                ):
                    return pause(
                        'Statistics paused · a selected model slot is unloaded or has a loading error.'
                    )
        stats = raw.get('stats')
        counters = (
            tuple(stats.get(k) for k in ('requests_served', 'tokens_generated'))
            if isinstance(stats, dict)
            else ()
        )
        if len(counters) != 2 or any(not finite(v) or v < 0 or int(v) != v for v in counters):
            return pause('Statistics paused · waiting for valid provider work counters.')
        if not identity_verified:
            return pause(
                'Statistics paused · matching this Mac and its selected models to the provider roster.'
            )
        key = (
            account,
            raw['attestation_public_key'],
            raw['pid'],
            raw['started_at'],
            tuple(selected),
            tuple(loaded),
            roster_provider,
        )
        previous = self.previous
        current = {'key': key, 'at': written, 'counters': counters}
        same = previous is not None and previous['key'] == key
        if same and written == previous['at'] and counters != previous['counters']:
            return pause('Statistics paused · waiting for a consistent provider sample.')
        continuous = (
            same
            and 0 <= written - previous['at'] <= 15
            and all(a >= b for a, b in zip(counters, previous['counters']))
        )
        if not continuous:
            self.verified_at = None
        elif (
            written > previous['at']
            and counters[1] > previous['counters'][1]
            and self.verified_at is None
        ):
            # Begin now, not at daemon startup or the first unverified sample.
            self.verified_at = written
        self.previous = current
        if self.verified_at is None:
            return {
                **result,
                'detail': 'Models loaded · waiting to observe fresh serving output from this model set.',
            }
        return {
            **result,
            'counting': True,
            'status': 'counting',
            'verifiedAt': self.verified_at,
            'detail': (
                f'Counting aggregate work while all {len(selected)} models are loaded and warm. '
                if len(loaded) == len(selected)
                else f'Counting aggregate work from the {len(loaded)} of {len(selected)} models '
                f'loaded now ({", ".join(loaded)}); a new segment starts when that set changes. '
            )
            + 'Serving output observed; per-model income comes from confirmed credits.',
            'loaded': loaded,
        }
