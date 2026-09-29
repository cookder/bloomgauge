"""Whether Darkbloom's coordinator lets a Mac serve: its trust, as the coordinator decides it.

trust_level 'hardware' means Darkbloom MDM + MDA + Secure Enclave key (coordinator
registry/provider.go). A Mac can also be authorized through App Attest without MDM (macOS
27, or `darkbloom unenroll --keep-serving`): it stays 'self_signed' but carries an
app_attest authorization and serves normally (coordinator 0.9.11
appattest/service/authorization_status.go). On Sep 28, 162 of ~1,100 online providers
were like that, and Darkbloom says its MDM will be switched off soon, which makes it every
Mac. Checks that used to require 'hardware' use these instead. A self_signed Mac without
an authorization (legacy verification not active) is still not authorized.
"""

# The coordinator's (path, reason) pairs that mean "may serve the network"
# (authorization_status.go). 'self_route' / owner_serving_authorized is the owner routing work
# to their own Mac, allowed at any trust level: not serving authorization.
AUTHORIZED = {('app_attest', 'app_attest_verified'), ('legacy', 'legacy_verification_active')}


def daemon_authorized(raw):
    """daemon-state.json `trust`: online and hardware-trusted or authorized to serve.

    It ignores the ~30 s App Attest lease expiry, so never use it alone to admit a command:
    callers also require the live roster (roster_authorized)."""
    trust = (raw or {}).get('trust') if isinstance(raw, dict) else None
    if not isinstance(trust, dict) or trust.get('status') != 'online':
        return False
    if trust.get('trust_level') == 'hardware':
        return True
    authorization = trust.get('authorization')
    return (
        isinstance(authorization, dict)
        and (authorization.get('path'), authorization.get('reason')) in AUTHORIZED
    )


def daemon_verifying(raw):
    """daemon-state.json `trust`: online but not (yet) authorized to serve. After every start
    the coordinator lists the new session like that while it verifies the Mac (App Attest or
    MDM: self_signed, authorization app_attest_qualification_required; the live roster on
    Sep 28 had 56 such Macs, verification 'pending'). Darkbloom's local endpoint serves
    meanwhile (ProviderLoop+LocalEndpoint.swift has no trust check), so a pick can warm and
    verify the model locally, but the network sends it no work yet."""
    trust = (raw or {}).get('trust') if isinstance(raw, dict) else None
    return isinstance(trust, dict) and trust.get('status') == 'online' and not daemon_authorized(raw)


def roster_authorized(row):
    """A /v1/providers/attestation row: live and hardware-trusted or App Attest-authorized."""
    if not isinstance(row, dict) or row.get('status') not in ('online', 'serving'):
        return False
    return row.get('trust_level') == 'hardware' or row.get('app_attest_authorized') is True


def stats_authorized(provider):
    """A /v1/stats provider: True/False, None when it carries no trust level at all."""
    if not isinstance(provider, dict) or provider.get('trust_level') is None:
        return None
    if provider.get('trust_level') == 'hardware':
        return True
    verification = provider.get('verification')
    attest = verification.get('app_attest') if isinstance(verification, dict) else None
    return isinstance(attest, dict) and attest.get('state') == 'verified'
