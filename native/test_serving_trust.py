"""Serving authorization: hardware trust, or App Attest without Darkbloom MDM.

Row shapes are copied from the live network on Sep 28, 2026 (/v1/providers/attestation,
/v1/stats) and from Darkbloom 0.9.11 daemon-state.json."""

import unittest

import network_evidence
from optimizer import roster_identity
from serving_trust import daemon_authorized, daemon_verifying, roster_authorized, stats_authorized

KEY = 'se-key'
ATTEST_ONLY_ROW = {  # 162 providers looked like this on Sep 28
    'provider_id': 'p1', 'se_public_key': KEY, 'models': ['gemma-4-26b-qat-4bit'],
    'status': 'serving', 'trust_level': 'self_signed', 'app_attest_authorized': True,
    'mdm_verified': False, 'mda_verified': False,
    'verification': {'app_attest': {'state': 'verified'}, 'legacy': {'state': 'pending'}},
}
HARDWARE_ROW = dict(ATTEST_ONLY_ROW, trust_level='hardware', app_attest_authorized=False, mdm_verified=True)
UNAUTHORIZED_ROW = dict(ATTEST_ONLY_ROW, status='online', app_attest_authorized=False,
                        verification={'app_attest': {'state': 'pending'}, 'legacy': {'state': 'pending'}})


def daemon(level, path=None, reason=None, status='online'):
    trust = {'status': status, 'trust_level': level, 'reason': 'Provider authorization updated'}
    if path:
        trust['authorization'] = {'path': path, 'reason': reason, 'protocol': 1}
    return {'trust': trust}


class ServingTrustTests(unittest.TestCase):
    def test_daemon_state(self):
        self.assertTrue(daemon_authorized(daemon('hardware')))
        self.assertTrue(daemon_authorized(daemon('self_signed', 'app_attest', 'app_attest_verified')))
        self.assertTrue(daemon_authorized(daemon('self_signed', 'legacy', 'legacy_verification_active')))
        for raw in (
            daemon('self_signed', 'none', 'app_attest_qualification_required'),
            daemon('self_signed', 'app_attest', 'security_verification_failed'),
            daemon('none', 'self_route', 'owner_serving_authorized'),
            daemon('self_signed', 'legacy', 'app_attest_verified'),
            daemon('self_signed'),
            daemon('hardware', status='untrusted'),
            daemon('self_signed', 'app_attest', 'app_attest_verified', status='offline'),
            {'trust': 'online'}, {}, None,
        ):
            with self.subTest(raw=raw):
                self.assertFalse(daemon_authorized(raw))

    def test_a_new_session_is_online_but_not_cleared_while_the_network_verifies_it(self):
        # Every start: online, self_signed, no authorization yet (Darkbloom 0.9.11).
        self.assertTrue(daemon_verifying(daemon('self_signed', 'none', 'app_attest_qualification_required')))
        self.assertTrue(daemon_verifying(daemon('self_signed')))
        for raw in (
            daemon('hardware', 'none', 'app_attest_qualification_required'),  # Andrew's Mac
            daemon('self_signed', 'app_attest', 'app_attest_verified'),
            daemon('none', 'none', 'app_attest_qualification_required', status='untrusted'),
            daemon('self_signed', status='offline'),
            {'trust': 'online'}, {}, None,
        ):
            with self.subTest(raw=raw):
                self.assertFalse(daemon_verifying(raw))

    def test_the_roster_lists_a_session_it_is_still_verifying(self):
        # 56 providers on Sep 28: online, self_signed, verification pending. Identity matches
        # (a pick may warm up locally); serving authorization doesn't.
        raw = {'attestation_public_key': KEY, 'advertised_models': ['gemma-4-26b-qat-4bit']}
        proof = roster_identity(raw, [UNAUTHORIZED_ROW])
        self.assertTrue(proof['servingEligible'])
        self.assertFalse(proof['hardwareVerified'])

    def test_roster_rows(self):
        self.assertTrue(roster_authorized(ATTEST_ONLY_ROW))
        self.assertTrue(roster_authorized(HARDWARE_ROW))
        self.assertFalse(roster_authorized(UNAUTHORIZED_ROW))
        self.assertFalse(roster_authorized(dict(ATTEST_ONLY_ROW, status='untrusted')))
        self.assertFalse(roster_authorized(dict(ATTEST_ONLY_ROW, app_attest_authorized='true')))

    def test_roster_identity_counts_an_attest_only_mac_as_verified(self):
        raw = {'attestation_public_key': KEY, 'advertised_models': ['gemma-4-26b-qat-4bit']}
        self.assertTrue(roster_identity(raw, [ATTEST_ONLY_ROW])['hardwareVerified'])
        self.assertFalse(roster_identity(raw, [UNAUTHORIZED_ROW])['hardwareVerified'])

    def test_stats_rows_and_the_peer_benchmark_flag(self):
        self.assertTrue(stats_authorized(ATTEST_ONLY_ROW))
        self.assertTrue(stats_authorized(HARDWARE_ROW))
        self.assertFalse(stats_authorized(UNAUTHORIZED_ROW))
        self.assertIsNone(stats_authorized({'status': 'online'}))
        provider = dict(ATTEST_ONLY_ROW, id='p1', current_model='gemma-4-26b-qat-4bit',
                        requests_served=10, tokens_generated=100, chip_family='M5', chip_tier='Pro', memory_gb=48)
        self.assertIs(network_evidence.compact([provider])['p1'][6], True)


if __name__ == '__main__':
    unittest.main()
