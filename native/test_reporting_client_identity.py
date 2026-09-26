"""Regression for the reproduced Cloudflare block of the generic Python client."""

import unittest
from unittest.mock import Mock, patch
import support_reports as support
import usage_reporting as usage


class ClientIdentityTests(unittest.TestCase):
    def check_transport(self, module, method):
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"status":"sent","reportId":"synthetic"}'
        opener = Mock()
        body = b'{"synthetic":"unchanged"}'
        headers = {
            'Authorization': 'Bearer synthetic-test-only',
            'Content-Type': 'application/json',
        }
        original = dict(headers)
        captured = []

        def edge(request, timeout):
            agent = request.get_header('User-agent')
            self.assertTrue(
                agent and agent.startswith('Bloom'),
                'Generic Python identity would be blocked before the worker',
            )
            self.assertEqual(request.full_url, module.ENDPOINT)
            self.assertEqual(request.get_method(), method)
            self.assertEqual(request.data, body)
            self.assertEqual(request.get_header('Authorization'), headers['Authorization'])
            self.assertEqual(request.get_header('Content-type'), 'application/json')
            self.assertEqual(timeout, 8)
            captured.append(agent)
            return response

        opener.open.side_effect = edge
        with patch.object(module.urllib.request, 'build_opener', return_value=opener) as build:
            result = (
                module._transport(module.ENDPOINT, body, headers, 8)
                if module is support
                else module._transport(method, module.ENDPOINT, body, headers, 8)
            )
        self.assertEqual(headers, original)
        self.assertEqual(len(captured), 1)
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], module._NoRedirect)
        self.assertEqual(result[0] if module is support else result, 200)
        if module is usage:
            response.read.assert_not_called()

    def test_explicitly_confirmed_support_transport_identifies_bloom(self):
        self.check_transport(support, 'POST')

    def test_opted_in_usage_transport_identifies_bloom(self):
        self.check_transport(usage, 'POST')

    def test_opt_out_deletion_uses_the_same_transport_identity(self):
        self.check_transport(usage, 'DELETE')
