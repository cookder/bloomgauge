"""Read-only aggregate reporting for model sets Bloomkeeper does not control.

Never grants optimizer eligibility, runs a decode, or changes the provider.
Counter deltas belong to the whole set; only paid credits identify a model.
"""

import logging, math
import bloom_log  # noqa: F401  (quiet until the app configures logging)

log = logging.getLogger('bloom.reporting')


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# Darkbloom rewrites daemon-state.json about every 2 s while serving, so 15 s is the
# measured liveness window. During 0.9.10's startup preload (every start) it defers
# registration and refreshes the file only every 30 s
# (ProviderLoop+StartupPreload.swift `preloadLivenessRefreshInterval`).
FRESH_SECONDS = 15
PRELOAD_FRESH_SECONDS = 45


def preloading(raw):
    """Darkbloom is loading its startup models (`startup_preload_pending_models`, 0.9.10)."""
    pending = raw.get('startup_preload_pending_models') if isinstance(raw, dict) else None
    return isinstance(pending, list) and bool(pending)


def state_fresh(raw, now):
    """The provider wrote daemon-state recently enough to count as running."""
    written = raw.get('written_at') if isinstance(raw, dict) else None
    window = PRELOAD_FRESH_SECONDS if preloading(raw) else FRESH_SECONDS
    return finite(written) and finite(now) and -5 < now - written < window


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


def offered_not_downloaded(offered, local, listed_at, offered_since):
    """Offered models that `darkbloom models list --all` no longer shows on this Mac.

    `darkbloom models remove` deletes a model's files, but the running provider keeps
    offering it until Darkbloom restarts. Only a list read after this model set began
    being offered counts (a model downloaded later would look missing), and only when
    it shows at least one offered model (otherwise it may describe another folder)."""
    offered = observed_models(offered)
    ids = {m.get('id') for m in local if isinstance(m, dict)} if isinstance(local, list) else set()
    if (
        not offered
        or not finite(listed_at)
        or not finite(offered_since)
        or listed_at < offered_since
        or not ids & set(offered)
    ):
        return []
    return [m for m in offered if m not in ids]


def reporting_scope(account, raw, now):
    """This process, key and offered set for this account, fresh and online.

    Identity only; it does not prove serving output. Which offered models are loaded
    is not part of it: Darkbloom loads and unloads them as requests come and go, and
    the coordinator's roster row doesn't depend on them. The loaded set belongs to the
    statistics segment instead (ProviderReporting)."""
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
    stats = raw.get('stats')
    counters = (
        tuple(stats.get(k) for k in ('requests_served', 'tokens_generated'))
        if isinstance(stats, dict)
        else ()
    )
    if len(counters) != 2 or any(not finite(v) or v < 0 or int(v) != v for v in counters):
        return None
    return account, key, raw['pid'], raw['started_at'], tuple(selected)


class ReportingIdentity:
    """In-memory 3+ model roster proof, independent of control identity.

    Each proof keeps the offered models its roster row lists: the ones the network
    sends this Mac work for. match() leaves them in `routed` for the proof it used."""

    def __init__(self):
        self.proof = None
        self.previous_proof = None
        self.routed = None

    def revoke(self):
        self.proof = None
        self.previous_proof = None
        self.routed = None

    def confirm(self, account, raw, now, provider, confirmed_at, routed=None):
        scope = reporting_scope(account, raw, now)
        # Offered models the roster row lists; all of them when it isn't given.
        offered = scope[-1] if scope else ()
        routes = tuple(m for m in offered if routed is None or m in routed)
        if (
            scope is None
            or not routes
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
        self.proof = {'scope': scope, 'provider': provider, 'at': confirmed_at, 'routed': routes}

    def match(self, account, raw, now):
        self.routed = None
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
                self.routed = previous['routed']
                return previous['provider']
            return None
        if now - proof['at'] >= 180 or reporting_scope(account, raw, now) != proof['scope']:
            self.revoke()
            return None
        self.routed = proof['routed']
        return proof['provider']


class ProviderReporting:
    def __init__(self):
        self.previous = None
        self.verified_at = None
        self.loaded_seen = None  # for the churn log; survives statistics pauses

    def reset(self):
        self.previous = None
        self.verified_at = None

    def note_loaded(self, raw, selected, written):
        """Log every change to one provider process's loaded models, to measure churn."""
        process = (
            raw.get('attestation_public_key'),
            raw.get('pid'),
            raw.get('started_at'),
            tuple(selected),
        )
        loaded = tuple(loaded_models(raw, selected))
        seen = self.loaded_seen
        changes = 0
        if seen and seen['process'] == process:
            if loaded == seen['loaded'] or written <= seen['at']:
                return
            changes = seen['changes'] + 1
            log.info(
                'Darkbloom changed the loaded models (change %d in this provider process, '
                'previous set seen for %d s): %d of %d offered loaded%s',
                changes,
                round(written - seen['at']),
                len(loaded),
                len(selected),
                f' ({", ".join(loaded)})' if loaded else '',
            )
        self.loaded_seen = {'process': process, 'loaded': loaded, 'at': written, 'changes': changes}

    def observe(
        self,
        account,
        raw,
        now,
        identity_verified=False,
        pending=False,
        roster_provider=None,
        routed=None,
    ):
        """`routed`: offered models the matched roster row lists (None: all of them)."""
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
            # Counting keeps the 15 s window; 0.9.10's startup preload writes every 30 s.
            return pause(
                'Statistics paused · Darkbloom is starting and loading its models.'
                if len(selected) >= 3 and preloading(raw) and state_fresh(raw, now)
                else 'Statistics paused · waiting for fresh multi-model readings.'
            )
        self.note_loaded(raw, selected, written)
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
        # Only models the network sends this Mac work for count as serving.
        routes = [m for m in selected if routed is None or m in routed]
        loaded = [m for m in loaded_models(raw, selected) if m in routes]
        if not loaded:
            return pause(
                f'Statistics paused · none of the {len(selected)} models your provider offers '
                'are loaded yet.'
                if len(routes) == len(selected)
                else f'Statistics paused · none of the {len(routes)} models the network sends '
                'this Mac work for are loaded yet.'
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
            tuple(routes),
            roster_provider,
        )
        previous = self.previous
        # The loaded set is part of the segment, not of the proof: see below.
        current = {'key': key, 'loaded': tuple(loaded), 'at': written, 'counters': counters}
        same = previous is not None and previous['key'] == key
        if (
            same
            and written == previous['at']
            and (counters != previous['counters'] or current['loaded'] != previous['loaded'])
        ):
            return pause('Statistics paused · waiting for a consistent provider sample.')
        continuous = (
            same
            and 0 <= written - previous['at'] <= 15
            and all(a >= b for a, b in zip(counters, previous['counters']))
        )
        if not continuous:
            self.verified_at = None
        elif written > previous['at'] and (
            (self.verified_at is None and counters[1] > previous['counters'][1])
            or (self.verified_at is not None and current['loaded'] != previous['loaded'])
        ):
            # Begin now, not at daemon startup or the first unverified sample. When
            # Darkbloom loads or unloads a model, this process has already shown it
            # serves: keep counting, but start a new segment so numbers from different
            # loaded sets never mix.
            self.verified_at = written
        self.previous = current
        if self.verified_at is None:
            return {
                **result,
                'detail': 'Models loaded · waiting to observe fresh serving output from this model set.',
            }
        routed_note = (
            ''
            if len(routes) == len(selected)
            else f'The network sends this Mac work for {len(routes)} of the {len(selected)} '
            'models your provider offers. '
        )
        return {
            **result,
            'counting': True,
            'status': 'counting',
            'verifiedAt': self.verified_at,
            'detail': (
                f'Counting aggregate work while all {len(routes)} models are loaded and warm. '
                if len(loaded) == len(routes)
                else f'Counting aggregate work from the {len(loaded)} of {len(routes)} models '
                f'loaded now ({", ".join(loaded)}); a new segment starts when that set changes. '
            )
            + routed_note
            + 'Serving output observed; per-model income comes from confirmed credits.',
            'loaded': loaded,
        }
